"""Per-deployment completion budget, per-call usage, budget exhaustion and the run's deployment.

Mocked transport and the fake provider only; no network.
"""

import json
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.deps import provider_for_deployment
from app.core.config import DeploymentSetting, Settings, get_settings
from app.llm.contracts import LLMRequest, LLMResponse, TokenUsage
from app.llm.exceptions import LLMInvalidResponseError, LLMReasoningBudgetExhaustedError
from app.llm.factory import create_provider, deployment_settings, verified_deployments
from app.llm.fake_provider import FakeLLMProvider
from app.llm.workbench_provider import WorkbenchConfig, WorkbenchProvider
from tests.test_agent import (
    action,
    clean_agent_state,  # noqa: F401  (autouse fixture of the agent tests)
    configure,
    finish,
    run_of,
    start,
    write,
)
from tests.test_agent import world as world  # noqa: F401
from tests.test_patches import World
from tests.test_requirements_api import use

REASONING = "gpt-5-2025-08-07-dzs-sdc"
OTHER = "gpt-5-5-2026-04-24-dzs-sdc"
GPT41 = "gpt-4-1-2025-04-14-dzs-sdc"
REQ = LLMRequest(prompt="p", system_prompt="s", temperature=0.0, max_output_tokens=4000)


def settings(deployment: str, **extra: Any) -> Settings:
    return Settings(
        _env_file=None,
        require_postgres=False,
        llm_provider="kpmg_workbench",
        workbench_api_key="test-only-key-not-real",
        workbench_endpoint="https://workbench.example.test",
        workbench_deployment=deployment,
        workbench_api_version="2024-02-01",
        workbench_region="r",
        workbench_charge_code="c",
        **extra,
    )


def body_sent(provider: Any) -> dict[str, Any]:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "m",
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {},
            },
        )

    provider._transport = httpx.MockTransport(handler)
    provider.generate(REQ)
    return seen[0]


# ---- 1. budget and effort per deployment -------------------------------------------------------


def test_reasoning_deployments_get_the_configured_budget_and_low_effort_by_default() -> None:
    body = body_sent(create_provider(settings(REASONING)))
    assert body["max_completion_tokens"] == 25_000
    assert body["reasoning_effort"] == "low"
    assert "max_tokens" not in body and "temperature" not in body


def test_gpt41_keeps_its_request_limit_and_gets_no_effort() -> None:
    body = body_sent(create_provider(settings(GPT41)))
    assert body["max_tokens"] == 4000
    assert "max_completion_tokens" not in body and "reasoning_effort" not in body
    assert deployment_settings(settings(GPT41), GPT41)[0] is None


def test_budget_and_effort_can_be_set_globally_and_per_deployment() -> None:
    cfg = settings(
        REASONING,
        workbench_max_completion_tokens_reasoning=12_000,
        workbench_deployments={
            OTHER: DeploymentSetting(
                max_completion_tokens=40_000, reasoning_effort="high", verified=True
            )
        },
    )
    assert deployment_settings(cfg, REASONING) == (12_000, "low")
    assert deployment_settings(cfg, OTHER) == (40_000, "high")
    assert verified_deployments(cfg) == [OTHER]
    assert body_sent(create_provider(cfg))["max_completion_tokens"] == 12_000


def test_deployment_settings_come_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCHPILOT_WORKBENCH_MAX_COMPLETION_TOKENS_REASONING", "30000")
    monkeypatch.setenv("ARCHPILOT_WORKBENCH_DEPLOYMENTS", json.dumps({OTHER: {"verified": True}}))
    cfg = Settings(_env_file=None, require_postgres=False)
    assert cfg.workbench_max_completion_tokens_reasoning == 30_000
    assert verified_deployments(cfg) == [OTHER]


# ---- 2. what each call used --------------------------------------------------------------------


def test_reasoning_tokens_and_finish_reason_are_read_from_the_reply() -> None:
    reply = {
        "model": "actual-model",
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 13,
            "completion_tokens": 90,
            "total_tokens": 103,
            "completion_tokens_details": {"reasoning_tokens": 64},
        },
    }
    provider = WorkbenchProvider(
        WorkbenchConfig(
            endpoint="https://workbench.example.test",
            deployment=REASONING,
            api_version="v",
            region="r",
            charge_code="c",
            api_key="k",
            max_retries=0,
        ),
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=reply)),
    )
    got = provider.generate(REQ)
    assert got.usage.reasoning_tokens == 64 and got.usage.output_tokens == 90
    assert got.finish_reason == "stop" and got.model == "actual-model"


class MeteredProvider(FakeLLMProvider):
    """The fake provider with reasoning tokens and a finish reason on every call."""

    def generate(self, request: LLMRequest) -> LLMResponse:
        got = super().generate(request)
        usage = TokenUsage(
            input_tokens=100, output_tokens=50, total_tokens=150, reasoning_tokens=30
        )
        return got.model_copy(update={"usage": usage, "finish_reason": "stop", "latency_ms": 7})


def test_every_step_records_its_call_and_the_run_shows_totals(
    client: TestClient, world: World
) -> None:
    use(MeteredProvider(agent_script=[action("list_files"), write("src/m.py"), finish()]))
    run = run_of(client, world, start(client, world))
    assert [s["call"] for s in run["steps"]] == [
        {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "reasoning_tokens": 30,
            "finish_reason": "stop",
            "latency_ms": 7,
        }
    ] * 3
    assert run["usage"] == {
        "calls": 3,
        "prompt_tokens": 300,
        "completion_tokens": 150,
        "reasoning_tokens": 90,
        "latency_ms": 21,
        "finish_reasons": {"stop": 3},
    }


def test_a_provider_that_reports_no_reasoning_tokens_gives_none_and_zero_totals(
    client: TestClient, world: World
) -> None:
    use(FakeLLMProvider(agent_script=[action("list_files"), write("src/m.py"), finish()]))
    run = run_of(client, world, start(client, world))
    assert run["steps"][0]["call"]["reasoning_tokens"] is None
    assert run["usage"]["reasoning_tokens"] == 0 and run["usage"]["calls"] == 3


# ---- 3. budget exhausted -----------------------------------------------------------------------


def _reply(content: str, finish_reason: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "m",
            "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 25000},
        },
    )


def _provider(response: httpx.Response) -> WorkbenchProvider:
    return WorkbenchProvider(
        WorkbenchConfig(
            endpoint="https://workbench.example.test",
            deployment=REASONING,
            api_version="v",
            region="r",
            charge_code="c",
            api_key="k",
            max_retries=0,
        ),
        transport=httpx.MockTransport(lambda _r: response),
    )


def test_empty_reply_with_length_is_reasoning_budget_exhausted() -> None:
    with pytest.raises(LLMReasoningBudgetExhaustedError) as info:
        _provider(_reply("", "length")).generate(REQ)
    assert "reasoning budget exhausted" in info.value.public_message.lower()
    assert isinstance(info.value, LLMInvalidResponseError)  # callers that catch it still do
    assert info.value.status_code == 200


def test_other_empty_replies_stay_a_generic_invalid_response() -> None:
    with pytest.raises(LLMInvalidResponseError) as info:
        _provider(_reply("", "stop")).generate(REQ)
    assert not isinstance(info.value, LLMReasoningBudgetExhaustedError)
    assert _provider(_reply("partial", "length")).generate(REQ).finish_reason == "length"


def test_a_run_whose_model_exhausts_its_budget_says_so(client: TestClient, world: World) -> None:
    class Exhausted(FakeLLMProvider):
        def generate(self, request: LLMRequest) -> LLMResponse:
            raise LLMReasoningBudgetExhaustedError(status_code=200)

    use(Exhausted())
    run = run_of(client, world, start(client, world))
    assert (run["status"], run["stop_reason"]) == ("failed", "reasoning_budget_exhausted")
    assert "reasoning budget exhausted" in run["summary"].lower()


# ---- 4. the deployment of a run ----------------------------------------------------------------


def with_deployments(monkeypatch: pytest.MonkeyPatch) -> None:
    patched = get_settings().model_copy(
        update={
            "workbench_deployment": GPT41,
            "workbench_deployments": {
                OTHER: DeploymentSetting(verified=True),
                REASONING: DeploymentSetting(verified=False),
            },
        }
    )
    monkeypatch.setattr("app.api.deps.get_settings", lambda: patched)


def test_the_ui_gets_the_default_and_only_verified_deployments(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_deployments(monkeypatch)
    assert client.get("/api/agent/deployments").json() == {"default": GPT41, "verified": [OTHER]}


def test_a_run_can_use_a_verified_deployment_and_the_default_is_unchanged(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    configure(monkeypatch)
    with_deployments(monkeypatch)
    script = [write("src/m.py"), finish()]
    use(FakeLLMProvider(agent_script=list(script), model=GPT41))
    chosen = run_of(client, world, start(client, world, deployment=OTHER))
    assert chosen["deployment"] == OTHER and chosen["status"] == "finished"

    use(FakeLLMProvider(agent_script=list(script), model=GPT41))
    default = run_of(client, world, start(client, world))
    assert default["deployment"] == GPT41


def test_an_unverified_deployment_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, world: World
) -> None:
    configure(monkeypatch)
    with_deployments(monkeypatch)
    use(FakeLLMProvider(agent_script=[finish()]))
    for name in (REASONING, "something-else"):
        assert start(client, world, deployment=name).status_code == 422


def test_the_chosen_deployment_is_the_one_called_with_its_own_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = settings(
        GPT41,
        workbench_deployments={
            OTHER: DeploymentSetting(max_completion_tokens=33_000, verified=True)
        },
    )
    monkeypatch.setattr("app.api.deps.get_settings", lambda: cfg)
    default = create_provider(cfg)
    assert isinstance(default, WorkbenchProvider)
    chosen = provider_for_deployment(default, OTHER)
    assert provider_for_deployment(default, None) is default
    assert provider_for_deployment(default, GPT41) is default
    assert isinstance(chosen, WorkbenchProvider) and chosen.model_name == OTHER
    assert body_sent(chosen)["max_completion_tokens"] == 33_000
    assert default.model_name == GPT41
