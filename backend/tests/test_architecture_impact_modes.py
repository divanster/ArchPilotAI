import copy
import uuid
from typing import Any

import pytest

from app.domain.graph import (
    EdgeKind,
    EdgeSpec,
    ImpactMode,
    NodeKind,
    NodeSpec,
    edge_id,
    impact,
)
from app.domain.revision import finding_instruction
from app.domain.semantic_lint import LintConfig, LintRule, lint_proposal
from tests.test_architecture_lint import base, load

PID = uuid.UUID(int=1)
T = ImpactMode.TRANSITIVE


def u(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def edge(src: int, dst: int) -> EdgeSpec:
    return EdgeSpec(
        edge_id(PID, u(src), u(dst), EdgeKind.JUSTIFIED_BY),
        u(src),
        u(dst),
        EdgeKind.JUSTIFIED_BY,
        f"f[{src}]",
    )


def ids(result: Any) -> list[int]:
    return [h.node_id.int for h in result.hits]


# 1 <- 2 <- 3 <- 4 (each depends on the next lower one)
CHAIN = [edge(2, 1), edge(3, 2), edge(4, 3)]


def test_direct_is_the_default_and_stops_at_depth_one() -> None:
    result = impact(u(1), CHAIN)
    assert ids(result) == [2]
    assert not result.truncated  # direct mode does not ask about deeper nodes
    assert result.hubs == []


def test_transitive_follows_the_chain_up_to_the_cap() -> None:
    result = impact(u(1), CHAIN, mode=T)
    assert ids(result) == [2, 3, 4]
    assert [h.depth for h in result.hits] == [1, 2, 3]
    assert not any(h.via_hub for h in result.hits)
    capped = impact(u(1), CHAIN, mode=T, max_depth=2)
    assert ids(capped) == [2, 3] and capped.truncated


def shared_hub() -> list[EdgeSpec]:
    # Node 2 depends on 1 and is shared by 6 others (3..8), each with a further dependent.
    edges = [edge(2, 1)]
    edges += [edge(n, 2) for n in range(3, 9)]
    edges += [edge(n + 10, n) for n in range(3, 9)]
    return edges


def test_transitive_does_not_expand_beyond_a_hub_and_reports_it() -> None:
    result = impact(u(1), shared_hub(), mode=T, hub_threshold=5)
    assert ids(result) == [2]  # 2 touches 7 edges, so its dependents are not pulled in
    assert [(h.node_id.int, h.degree) for h in result.hubs] == [(2, 7)]


def test_threshold_is_configurable() -> None:
    result = impact(u(1), shared_hub(), mode=T, hub_threshold=7)
    assert 2 not in {h.node_id.int for h in result.hubs}
    assert {3, 4, 5, 6, 7, 8} <= set(ids(result))
    assert impact(u(1), shared_hub(), mode=T, hub_threshold=6).hubs[0].degree == 7


def test_direct_mode_still_lists_a_hub_as_a_hit_without_expanding_it() -> None:
    result = impact(u(1), shared_hub())
    assert ids(result) == [2] and result.hubs == []


def test_a_hub_with_nothing_further_to_expand_is_not_reported() -> None:
    edges = [edge(n, 1) for n in range(2, 9)]  # root itself is the hub; leaves have degree 1
    result = impact(u(1), edges, mode=T, hub_threshold=5)
    assert len(result.hits) == 7 and result.hubs == []
    assert all(h.via_hub for h in result.hits)  # the path started at a hub (the queried node)


def test_cycle_safety_in_both_modes() -> None:
    cycle = [edge(2, 1), edge(3, 2), edge(1, 3)]
    for mode in ImpactMode:
        result = impact(u(1), cycle, mode=mode)
        assert len(ids(result)) == len(set(ids(result)))
        assert 1 not in ids(result)
    assert sorted(ids(impact(u(1), cycle, mode=T))) == [2, 3]


def test_result_cap_applies_in_both_modes() -> None:
    fan = [edge(n, 1) for n in range(2, 12)]
    for mode in ImpactMode:
        result = impact(u(1), fan, mode=mode, limit=3, hub_threshold=100)
        assert len(result.hits) == 3 and result.truncated


# ---- API -------------------------------------------------------------------------------------


def test_api_defaults_to_direct_and_returns_the_new_fields(client: Any) -> None:
    from tests.test_architecture_graph import generated, graph_url, rebuild, snapshot

    pid, proposal_id, _ = generated(client)
    rebuild(client, pid, proposal_id)
    snap = snapshot(client, pid, proposal_id)
    node = next(n for n in snap["nodes"] if n["kind"] == "requirement")
    direct = client.get(graph_url(pid, proposal_id, f"/impact?node_id={node['id']}")).json()
    assert direct["mode"] == "direct" and direct["max_depth"] == 1
    assert direct["hubs_not_expanded"] == []
    assert direct["items"] and {i["depth"] for i in direct["items"]} == {1}
    first = direct["items"][0]
    assert {"node", "depth", "via_kind", "via_evidence", "via_hub"} <= set(first)
    assert first["node"]["kind"] and first["node"]["label"]

    transitive = client.get(
        graph_url(pid, proposal_id, f"/impact?node_id={node['id']}&mode=transitive")
    ).json()
    assert transitive["mode"] == "transitive" and transitive["max_depth"] == 3
    assert transitive["hub_threshold"] == 5
    assert len(transitive["items"]) >= len(direct["items"])
    bad = client.get(graph_url(pid, proposal_id, f"/impact?node_id={node['id']}&mode=wide"))
    assert bad.status_code == 422


# ---- meta open question lint -----------------------------------------------------------------


def meta_findings(question: str, config: LintConfig | None = None) -> list[str | None]:
    data = base()
    data["open_questions"] = [
        {"question": "Which identity provider does the client use?", "requirement_ids": []},
        {"question": question, "requirement_ids": []},
    ]
    content = load(data)
    before = copy.deepcopy(content.model_dump(mode="json"))
    report = lint_proposal(data["title"], data["summary"], content, {}, config)
    assert content.model_dump(mode="json") == before  # advisory: content is never changed
    return [f.path for f in report.findings if f.rule is LintRule.META_OPEN_QUESTION]


@pytest.mark.parametrize(
    "question",
    [
        "Why were passages not cited in this proposal?",
        "Why were no passages cited?",
        "Were the supplied passages used by the model?",
        "Did the model cite any source documents?",
        "Why does this proposal have no citations?",
    ],
)
def test_meta_questions_are_flagged_with_their_path(question: str) -> None:
    assert meta_findings(question) == ["open_questions[1]"]


@pytest.mark.parametrize(
    "question",
    [
        "Which AI model should the client use for search?",
        "Which document section defines the retention period?",
        "Does the client have a preferred cloud region?",
        "How many users will upload documents each day?",
        "Which source system holds the customer master data?",
        "Is a proposal template required by the client's procurement team?",
    ],
)
def test_ordinary_client_questions_are_not_flagged(question: str) -> None:
    assert meta_findings(question) == []


def test_patterns_are_configurable() -> None:
    config = LintConfig(meta_question_patterns=(r"\bwidget\b",))
    assert meta_findings("Where is the widget?", config) == ["open_questions[1]"]
    assert meta_findings("Why were passages not cited?", config) == []


def test_meta_finding_has_a_revise_instruction_and_a_fixed_message() -> None:
    data = base()
    data["open_questions"] = [{"question": "Why were passages not cited?", "requirement_ids": []}]
    report = lint_proposal(data["title"], data["summary"], load(data), {})
    finding = next(f for f in report.findings if f.rule is LintRule.META_OPEN_QUESTION)
    assert "Why were" not in finding.message
    assert "open_questions[0]" in finding_instruction(finding.model_dump(mode="json"))


def test_node_spec_is_importable_for_completeness() -> None:
    assert NodeSpec(u(1), NodeKind.RISK, "r", "risks[0]").label == "r"
