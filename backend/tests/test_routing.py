"""Model routing, escalation and per-run budgets. Fake providers only; no network."""

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.domain import routing as r
from app.llm.contracts import LLMRequest, LLMResponse
from app.llm.exceptions import LLMReasoningBudgetExhaustedError
from app.llm.fake_provider import FakeLLMProvider
from tests.test_agent import (
    action,
    clean_agent_state,  # noqa: F401  (autouse fixture of the agent tests)
    finish,
    run_of,
    start,
    write,
)
from tests.test_agent import world as world  # noqa: F401
from tests.test_patches import World
from tests.test_requirements_api import use

DEFAULT = "fake-model-1"
LIGHT, HEAVY = "light-m", "heavy-m"
BAD = '{"foo": 1}'


def policy(**extra: Any) -> r.RoutingPolicy:
    values: dict[str, Any] = {
        "enabled": True,
        "light": LIGHT,
        "heavy": HEAVY,
        "verified": frozenset({LIGHT, HEAVY}),
    }
    values.update(extra)
    return r.RoutingPolicy(**values)


class Exhausted(FakeLLMProvider):
    def generate(self, request: LLMRequest) -> LLMResponse:
        raise LLMReasoningBudgetExhaustedError(status_code=200)


class Fleet:
    """The fake providers a run can be routed to, and how often each was called."""

    def __init__(self, **scripts: list[str] | FakeLLMProvider) -> None:
        self.providers: dict[str, FakeLLMProvider] = {}
        for name, script in scripts.items():
            self.providers[name.replace("_", "-")] = (
                script
                if isinstance(script, FakeLLMProvider)
                else FakeLLMProvider(agent_script=list(script), model=name.replace("_", "-"))
            )
        self.resolved: list[str] = []

    def resolve(self, name: str) -> FakeLLMProvider:
        self.resolved.append(name)
        return self.providers[name]


def route_runs(monkeypatch: pytest.MonkeyPatch, routing: r.RoutingPolicy, fleet: Fleet) -> None:
    monkeypatch.setattr("app.api.deps.routing_policy", lambda _settings: routing)
    monkeypatch.setattr(
        "app.api.deps.provider_for_deployment", lambda _p, name: fleet.resolve(name)
    )


def calls(run: dict[str, Any]) -> list[dict[str, Any]]:
    return [s["call"] for s in run["steps"]]


# ---- 1. policy selection ------------------------------------------------------------------------


@pytest.mark.parametrize("action_type", [a for a, t in r.ACTION_TIERS.items() if t == r.LIGHT])
def test_light_actions_go_to_the_light_deployment(action_type: str) -> None:
    got = r.route_for(policy(), action_type, DEFAULT)
    assert (got.deployment, got.tier, got.reason) == (LIGHT, r.LIGHT, r.POLICY)


@pytest.mark.parametrize("action_type", [a for a, t in r.ACTION_TIERS.items() if t == r.HEAVY])
def test_heavy_actions_go_to_the_heavy_deployment(action_type: str) -> None:
    got = r.route_for(policy(), action_type, DEFAULT)
    assert (got.deployment, got.tier, got.reason) == (HEAVY, r.HEAVY, r.POLICY)


def test_the_spec_assigns_each_action_type_to_its_tier() -> None:
    assert {r.FILE_SELECTION, r.SHORT_EDIT, r.FORMATTING, r.SIMPLE_TOOL_CALL} == {
        a for a, t in r.ACTION_TIERS.items() if t == r.LIGHT
    }
    assert {r.CHECKLIST_PROPOSAL, r.CI_DIAGNOSIS, r.STATIC_DIAGNOSIS, r.REPAIR, r.SELF_REVIEW} == {
        a for a, t in r.ACTION_TIERS.items() if t == r.HEAVY
    }


def test_an_unknown_action_type_is_refused() -> None:
    with pytest.raises(r.RoutingError):
        r.route_for(policy(), "make_coffee", DEFAULT)


def test_a_blank_tier_uses_the_runs_own_deployment() -> None:
    got = r.route_for(policy(light="", verified=frozenset({HEAVY})), r.FILE_SELECTION, DEFAULT)
    assert got.deployment == DEFAULT


def test_an_unverified_deployment_is_never_routed_to() -> None:
    bad = policy(verified=frozenset({LIGHT}))
    assert bad.problems() == [f"{HEAVY} is not marked as verified"]
    with pytest.raises(r.UnverifiedDeploymentError):
        r.route_for(bad, r.REPAIR, DEFAULT)
    assert r.route_for(bad, r.FILE_SELECTION, DEFAULT).deployment == LIGHT


def test_a_run_cannot_start_with_an_unverified_routing_deployment(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    world: World,  # noqa: F811
) -> None:
    fleet = Fleet()
    route_runs(monkeypatch, policy(verified=frozenset({LIGHT})), fleet)
    use(FakeLLMProvider(agent_script=[finish()]))
    assert start(client, world).status_code == 422
    assert fleet.resolved == []


def test_routing_off_is_the_default_and_calls_the_runs_deployment(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    world: World,  # noqa: F811
) -> None:
    fleet = Fleet()
    route_runs(monkeypatch, policy(enabled=False), fleet)
    default = FakeLLMProvider(agent_script=[write("src/m.py"), finish()])
    use(default)
    run = run_of(client, world, start(client, world))
    assert run["status"] == "finished"
    assert fleet.resolved == []
    assert len(default.requests) == 2
    for call in calls(run):
        assert call["deployment"] == DEFAULT
        assert call["tier"] is None and call["routing_reason"] == r.USER_SELECTED
    assert run["usage"]["escalations"] == []


def test_the_default_settings_leave_routing_off() -> None:
    from app.core.config import Settings
    from app.llm.factory import routing_policy

    cfg = Settings(_env_file=None, require_postgres=False)
    assert not routing_policy(cfg).enabled and routing_policy(cfg).problems() == []


def test_routed_calls_follow_the_policy_and_every_call_is_recorded(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    world: World,  # noqa: F811
) -> None:
    fleet = Fleet(light_m=[action("list_files"), write("src/m.py"), finish()])
    route_runs(monkeypatch, policy(), fleet)
    use(FakeLLMProvider(model=DEFAULT))
    run = run_of(client, world, start(client, world))
    got = calls(run)
    assert len(got) == run["steps_taken"] >= 3
    for call in got:
        assert call["deployment"] and call["routing_reason"] in {r.POLICY, r.ESCALATION}
        assert call["prompt_tokens"] and call["completion_tokens"]
    assert got[0]["deployment"] == LIGHT and got[0]["routing_detail"] == r.FILE_SELECTION
    assert run["usage"]["calls"] == len(got)
    assert sum(d["calls"] for d in run["usage"]["by_deployment"]) == len(got)


# ---- 2. escalation ------------------------------------------------------------------------------


def test_two_consecutive_invalid_actions_escalate() -> None:
    state = r.RoutingState(policy(), DEFAULT)
    state.observe("invalid", "invalid_shape", {}, changed=False)
    assert state.route().tier == r.LIGHT
    state.observe("invalid", "invalid_args", {}, changed=False)
    got = state.route()
    assert (got.deployment, got.reason, got.detail) == (HEAVY, r.ESCALATION, r.INVALID_ACTIONS)


def test_invalid_actions_must_be_consecutive() -> None:
    state = r.RoutingState(policy(), DEFAULT)
    state.observe("invalid", "invalid_shape", {}, changed=False)
    state.observe("list_files", None, {}, changed=False)
    state.observe("invalid", "invalid_shape", {}, changed=False)
    assert state.route().tier == r.LIGHT


FINDING = {"findings": [{"tool": "ruff", "file": "a.py", "code": "F401", "scope": "changed"}]}
CI_FAILURE = {"new": [{"name": "test_x", "file": "t.py", "category": "assertion"}]}


@pytest.mark.parametrize("result", [FINDING, CI_FAILURE])
def test_the_same_finding_or_ci_failure_twice_escalates(result: dict[str, Any]) -> None:
    state = r.RoutingState(policy(), DEFAULT)
    state.observe("run_checks", None, result, changed=True)
    assert state.escalation is None
    state.observe("run_checks", None, result, changed=True)
    got = state.route()
    assert (got.deployment, got.reason, got.detail) == (HEAVY, r.ESCALATION, r.REPEATED_FAILURE)


def test_different_or_non_actionable_findings_do_not_escalate() -> None:
    state = r.RoutingState(policy(), DEFAULT)
    other = {"findings": [{"tool": "ruff", "file": "a.py", "code": "F841", "scope": "changed"}]}
    elsewhere = {
        "findings": [{"tool": "ruff", "file": "b.py", "code": "E501", "scope": "elsewhere"}]
    }
    for result in (FINDING, other, elsewhere, elsewhere):
        state.observe("run_checks", None, result, changed=True)
    assert state.escalation is None


def test_the_line_of_a_finding_does_not_make_it_a_new_one() -> None:
    state = r.RoutingState(policy(), DEFAULT)
    for line in (3, 9):
        found = {"findings": [{**FINDING["findings"][0], "line": line}]}
        state.observe("run_checks", None, found, changed=True)
    assert state.escalation == r.REPEATED_FAILURE


def test_diagnosis_and_repair_steps_are_heavy_and_edits_are_light() -> None:
    state = r.RoutingState(policy(), DEFAULT)
    assert state.route().tier == r.LIGHT
    state.observe("run_checks", None, {"findings": [FINDING["findings"][0]]}, changed=True)
    assert state.route().detail == r.STATIC_DIAGNOSIS and state.route().tier == r.HEAVY
    state.observe("write_file", "tool_failed", {}, changed=True)
    assert state.route().detail == r.REPAIR
    state.observe("write_file", None, {}, changed=True)
    assert state.route().detail == r.SHORT_EDIT and state.route().tier == r.LIGHT


def test_there_is_no_de_escalation_within_a_run() -> None:
    state = r.RoutingState(policy(), DEFAULT)
    state.observe("invalid", "invalid_shape", {}, changed=False)
    state.observe("invalid", "invalid_shape", {}, changed=False)
    for name in ("list_files", "write_file", "list_files"):
        state.observe(name, None, {}, changed=False)
        got = state.route()
        assert (got.deployment, got.tier, got.reason) == (HEAVY, r.HEAVY, r.ESCALATION)
    assert not state.escalate(r.REPEATED_FAILURE)  # already there; the first reason stays
    assert state.route().detail == r.INVALID_ACTIONS


def test_escalation_does_nothing_when_routing_is_off() -> None:
    state = r.RoutingState(policy(enabled=False), DEFAULT)
    for _ in range(3):
        state.observe("invalid", "invalid_shape", {}, changed=False)
    assert state.route() == r.Route(DEFAULT, None, r.USER_SELECTED, "")


def test_a_run_escalates_after_two_invalid_actions_and_stays_heavy(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    world: World,  # noqa: F811
) -> None:
    fleet = Fleet(
        light_m=[BAD, BAD, action("list_files")],
        heavy_m=[action("list_files"), write("src/m.py"), action("list_files"), finish()],
    )
    route_runs(monkeypatch, policy(), fleet)
    use(FakeLLMProvider(model=DEFAULT))
    run = run_of(client, world, start(client, world))
    got = calls(run)
    assert [c["deployment"] for c in got[:2]] == [LIGHT, LIGHT]
    heavy = got[2:]
    assert heavy and all(c["deployment"] == HEAVY for c in heavy)
    assert all(c["routing_reason"] == r.ESCALATION for c in heavy)
    assert LIGHT not in {c["deployment"] for c in heavy}
    assert run["usage"]["escalations"] == [
        {"step_no": 3, "reason": r.INVALID_ACTIONS, "deployment": HEAVY}
    ]
    assert len(fleet.providers[LIGHT].requests) == 2


def test_a_light_model_that_runs_out_of_reasoning_budget_escalates(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    world: World,  # noqa: F811
) -> None:
    fleet = Fleet(
        light_m=Exhausted(model=LIGHT),
        heavy_m=[write("src/m.py"), finish()],
    )
    route_runs(monkeypatch, policy(), fleet)
    use(FakeLLMProvider(model=DEFAULT))
    run = run_of(client, world, start(client, world))
    assert run["status"] == "finished"
    assert all(c["deployment"] == HEAVY for c in calls(run))
    assert calls(run)[0]["routing_reason"] == r.ESCALATION
    assert run["usage"]["escalations"][0]["reason"] == r.REASONING_EXHAUSTED


def test_an_exhausted_heavy_model_still_fails_the_run_as_before(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    world: World,  # noqa: F811
) -> None:
    fleet = Fleet(light_m=Exhausted(model=LIGHT), heavy_m=Exhausted(model=HEAVY))
    route_runs(monkeypatch, policy(), fleet)
    use(FakeLLMProvider(model=DEFAULT))
    run = run_of(client, world, start(client, world))
    assert (run["status"], run["stop_reason"]) == ("failed", "reasoning_budget_exhausted")


# ---- 3. budgets ---------------------------------------------------------------------------------


def test_each_budget_stop_reason_is_distinct_and_known_to_the_ci_loop() -> None:
    from app.domain.ci_loop import TERMINAL_REASONS

    assert len({r.TOKEN_LIMIT, r.HEAVY_CALL_LIMIT, r.COST_LIMIT}) == 3
    assert {r.HEAVY_CALL_LIMIT, r.COST_LIMIT} <= set(TERMINAL_REASONS)


def test_budget_stop_checks_each_limit() -> None:
    light, heavy = r.Route(LIGHT, r.LIGHT, r.POLICY, ""), r.Route(HEAVY, r.HEAVY, r.POLICY, "")
    state = r.RoutingState(
        policy(token_limit=100, heavy_call_limit=1, cost_limit=1.0, prices={HEAVY: (1.0, 1.0)}),
        DEFAULT,
    )
    assert state.budget_stop(light) is None
    state.record(heavy, 10, 10)
    assert state.budget_stop(light) is None  # a light call is not a heavy call
    assert state.budget_stop(heavy) == r.HEAVY_CALL_LIMIT
    state.record(light, 40, 40)
    assert state.budget_stop(light) == r.TOKEN_LIMIT
    cost_only = r.RoutingState(policy(cost_limit=0.03, prices={LIGHT: (1.0, 1.0)}), DEFAULT)
    cost_only.record(light, 10, 20)
    assert cost_only.budget_stop(light) == r.COST_LIMIT


def test_no_limits_means_no_budget_stop() -> None:
    state = r.RoutingState(policy(), DEFAULT)
    state.record(r.Route(HEAVY, r.HEAVY, r.POLICY, ""), 10**6, 10**6)
    assert state.budget_stop(r.Route(HEAVY, r.HEAVY, r.POLICY, "")) is None


def test_a_run_stops_on_the_token_limit(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    world: World,  # noqa: F811
) -> None:
    fleet = Fleet(light_m=[action("list_files"), write("src/m.py"), finish()])
    route_runs(monkeypatch, policy(token_limit=1), fleet)
    use(FakeLLMProvider(model=DEFAULT))
    run = run_of(client, world, start(client, world))
    assert (run["status"], run["stop_reason"]) == ("stopped", r.TOKEN_LIMIT)
    assert len(calls(run)) == 1


def test_a_run_stops_on_the_heavy_call_limit(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    world: World,  # noqa: F811
) -> None:
    fleet = Fleet(
        light_m=[BAD, BAD],
        heavy_m=[action("list_files"), action("list_files"), finish()],
    )
    route_runs(monkeypatch, policy(heavy_call_limit=1), fleet)
    use(FakeLLMProvider(model=DEFAULT))
    run = run_of(client, world, start(client, world))
    assert (run["status"], run["stop_reason"]) == ("stopped", r.HEAVY_CALL_LIMIT)
    assert [c["deployment"] for c in calls(run)] == [LIGHT, LIGHT, HEAVY]
    assert len(fleet.providers[HEAVY].requests) == 1


def test_a_run_stops_on_the_cost_limit(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    world: World,  # noqa: F811
) -> None:
    fleet = Fleet(light_m=[action("list_files"), write("src/m.py"), finish()])
    route_runs(monkeypatch, policy(cost_limit=0.001, prices={LIGHT: (1.0, 1.0)}), fleet)
    use(FakeLLMProvider(model=DEFAULT))
    run = run_of(client, world, start(client, world))
    assert (run["status"], run["stop_reason"]) == ("stopped", r.COST_LIMIT)
    assert len(calls(run)) == 1 and calls(run)[0]["cost"] > 0
    assert run["usage"]["cost"] == pytest.approx(calls(run)[0]["cost"])


# ---- 4. project isolation -----------------------------------------------------------------------


def test_budgets_are_counted_per_run_not_across_runs(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    world: World,  # noqa: F811
) -> None:
    first = Fleet(light_m=[BAD, BAD], heavy_m=[action("list_files"), action("list_files")])
    route_runs(monkeypatch, policy(heavy_call_limit=1), first)
    use(FakeLLMProvider(model=DEFAULT))
    one = run_of(client, world, start(client, world))
    second = Fleet(light_m=[BAD, BAD], heavy_m=[action("list_files"), action("list_files")])
    route_runs(monkeypatch, policy(heavy_call_limit=1), second)
    two = run_of(client, world, start(client, world))
    for run in (one, two):
        assert run["stop_reason"] == r.HEAVY_CALL_LIMIT
        assert [c["deployment"] for c in calls(run)] == [LIGHT, LIGHT, HEAVY]
    assert one["id"] != two["id"]


def test_another_project_cannot_read_a_runs_routing_record(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    world: World,  # noqa: F811
) -> None:
    route_runs(monkeypatch, policy(), Fleet(light_m=[write("src/m.py"), finish()]))
    use(FakeLLMProvider(model=DEFAULT))
    run = run_of(client, world, start(client, world))
    other = uuid.uuid4()
    assert client.get(f"/api/projects/{other}/agent-runs/{run['id']}").status_code == 404


def test_the_routing_settings_are_readable(client: TestClient) -> None:
    body = client.get("/api/agent/routing").json()
    assert body["enabled"] is False and body["problems"] == []
    assert r.REPAIR in body["heavy_actions"] and r.FILE_SELECTION in body["light_actions"]
