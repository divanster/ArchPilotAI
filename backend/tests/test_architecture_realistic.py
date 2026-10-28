"""Mocked, realistically sized proposals for a 24-requirement project (no provider involved)."""

import json
import uuid
from typing import Any

import pytest

from app.core.config import Settings
from app.domain.architecture import (
    RESPONSE_CHARS_PER_TOKEN,
    FabricatedReferenceError,
    FailureCategory,
    GeneratedArchitecture,
    InvalidArchitectureOutputError,
    parse_generated,
    verify_references,
)

CONFIRMED = [uuid.uuid4() for _ in range(24)]
SENTENCE = "A realistic sentence describing the design choice and the reasoning behind it. "
CONCISE = "One sentence of about eighty characters that states the choice and its reason. "
REPEAT = 3  # sentences per text field (verbose profile)
REFS = 3  # requirement ids per item (verbose profile)
BUDGET_TOKENS = Settings.model_fields["architecture_max_output_tokens"].default
APPROX_CHARS_PER_TOKEN = 3.5


def ids(start: int, n: int = 3) -> list[str]:
    return [str(CONFIRMED[(start + k) % len(CONFIRMED)]) for k in range(n)]


def decision(i: int, origin: str = "recommended_by_ai") -> dict[str, Any]:
    return {
        "name": f"Decision {i}",
        "decision": SENTENCE * REPEAT,
        "rationale": SENTENCE * REPEAT,
        "origin": origin,
        "supporting_requirement_ids": ids(i, REFS),
        "alternatives_considered": [SENTENCE] * REPEAT,
        "assumptions": [SENTENCE],
        "risks": [SENTENCE],
    }


def realistic_proposal(per_list: int = 7, components: int = 10) -> dict[str, Any]:
    nodes = [{"id": f"n{i}", "name": f"Node {i}", "type": "service"} for i in range(components)]
    edges = [
        {"source": f"n{i}", "target": f"n{i + 1}", "relationship": "calls"}
        for i in range(components - 1)
    ]
    data = _realistic_body(per_list, components, nodes, edges)
    decisions = [
        d
        for key in (
            "recommended_stack",
            "integrations",
            "data_architecture",
            "security_architecture",
            "non_functional_design",
        )
        for d in data[key]
    ]
    for n, d in enumerate(decisions):
        d["id"] = f"d{n}"
    data["coverage_matrix"] = [
        {
            "requirement_id": str(rid),
            "related_ids": ["n0"],
            "verification": "Proposed acceptance test: reviewer walks the flow.",
            "status": "addressed" if n % 2 else "partial",
            "gap": None if n % 2 else "Needs client confirmation.",
        }
        for n, rid in enumerate(CONFIRMED)
    ]
    return data


def _realistic_body(
    per_list: int, components: int, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "title": "Atlas architecture",
        "summary": SENTENCE * 4,
        "solution_overview": SENTENCE * 8,
        "recommended_stack": [
            {**decision(i), "layer": f"Layer {i}", "technology": f"Tech {i}"}
            for i in range(per_list)
        ],
        "components": [
            {
                "id": f"n{i}",
                "name": f"Component {i}",
                "type": "service",
                "responsibility": SENTENCE * REPEAT,
                "technology": None if i % 2 else "Python",
                "requirement_ids": ids(i, REFS),
            }
            for i in range(components)
        ],
        "integrations": [decision(i, "requires_clarification") for i in range(per_list)],
        "data_architecture": [decision(i) for i in range(per_list)],
        "security_architecture": [decision(i, "required_by_client") for i in range(per_list)],
        "non_functional_design": [decision(i) for i in range(per_list)],
        "risks": [
            {
                "description": SENTENCE,
                "severity": "medium",
                "mitigation": SENTENCE,
                "requirement_ids": ids(i, REFS),
            }
            for i in range(per_list)
        ],
        "assumptions": [
            {"statement": SENTENCE, "requirement_ids": ids(i, 1)} for i in range(per_list)
        ],
        "open_questions": [
            {
                "question": SENTENCE,
                "why_it_matters": None if i % 2 else SENTENCE,
                "requirement_ids": ids(i, 1),
            }
            for i in range(per_list)
        ],
        "alternatives": [
            {
                "title": f"Alternative {i}",
                "description": SENTENCE,
                "trade_offs": SENTENCE,
                "requirement_ids": ids(i, 1),
            }
            for i in range(per_list)
        ],
        "implementation_phases": [
            {
                "name": f"Phase {i}",
                "objectives": [SENTENCE] * REPEAT,
                "requirement_ids": ids(i * 4, REFS),
            }
            for i in range(4)
        ],
        "diagram": {"nodes": nodes, "edges": edges},
    }


def v3_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the architecture-v4 prompt asks for: one sentence per field, at most 2 refs."""
    monkeypatch.setattr(f"{__name__}.SENTENCE", CONCISE)
    monkeypatch.setattr(f"{__name__}.REPEAT", 1)
    monkeypatch.setattr(f"{__name__}.REFS", 2)


@pytest.mark.parametrize("per_list", [3, 4])
def test_a_v3_style_24_requirement_proposal_is_valid_and_fits_the_budget(
    monkeypatch: pytest.MonkeyPatch, per_list: int
) -> None:
    v3_profile(monkeypatch)
    raw = json.dumps(realistic_proposal(per_list=per_list, components=8), separators=(",", ":"))
    max_chars = BUDGET_TOKENS * RESPONSE_CHARS_PER_TOKEN
    parsed = parse_generated(raw, finish_reason="stop", max_chars=max_chars)
    assert isinstance(parsed, GeneratedArchitecture)
    verify_references(parsed, set(CONFIRMED))
    assert len(parsed.coverage_matrix) == 24  # the matrix is part of what must fit
    estimated_tokens = len(raw) / APPROX_CHARS_PER_TOKEN
    assert estimated_tokens < BUDGET_TOKENS, estimated_tokens
    if per_list <= 3:  # headroom for tokenisation error
        assert estimated_tokens < BUDGET_TOKENS * 0.85, estimated_tokens


def test_verbose_proposals_overflow_the_budget_which_v3_avoids() -> None:
    """Characterisation: 7 items with full sentences and 3 refs is valid but too large."""
    raw = json.dumps(realistic_proposal(per_list=7))
    parse_generated(raw, finish_reason="stop")
    assert len(raw) / APPROX_CHARS_PER_TOKEN > BUDGET_TOKENS
    # The character guard (8 chars/token) is deliberately looser than real tokenisation,
    # so only the provider's finish_reason reveals this overflow.
    assert len(raw) < BUDGET_TOKENS * RESPONSE_CHARS_PER_TOKEN


def test_the_same_proposal_would_not_have_fit_the_old_4000_token_budget() -> None:
    raw = json.dumps(realistic_proposal(), separators=(",", ":"))
    assert len(raw) / APPROX_CHARS_PER_TOKEN > 4000


def test_budget_default_is_8000() -> None:
    assert BUDGET_TOKENS == 8000


def test_truncating_a_realistic_proposal_is_reported_as_truncation() -> None:
    raw = json.dumps(realistic_proposal())
    with pytest.raises(InvalidArchitectureOutputError) as caught:
        parse_generated(raw[: int(len(raw) * 0.6)], finish_reason="length")
    assert caught.value.category == FailureCategory.OUTPUT_SIZE
    assert caught.value.diagnostics["reason"] == "truncated_by_token_limit"


def test_one_fabricated_id_among_24_confirmed_is_rejected_with_counts_only() -> None:
    data = realistic_proposal()
    data["risks"][0]["requirement_ids"].append(str(uuid.uuid4()))
    parsed = parse_generated(json.dumps(data))
    with pytest.raises(FabricatedReferenceError) as caught:
        verify_references(parsed, set(CONFIRMED))
    assert caught.value.category == FailureCategory.REFERENCE
    assert caught.value.diagnostics["not_confirmed_distinct"] == 1
    assert caught.value.diagnostics["confirmed_available"] == 24


def test_a_proposal_citing_a_non_confirmed_known_id_is_rejected() -> None:
    parsed = parse_generated(json.dumps(realistic_proposal()))
    with pytest.raises(FabricatedReferenceError):
        verify_references(parsed, set(CONFIRMED[:5]))


@pytest.mark.parametrize(
    ("edit", "code"),
    [
        (
            lambda d: d["diagram"]["edges"].append(
                {"source": "n0", "target": "ghost", "relationship": "calls"}
            ),
            "unknown_edge_node",
        ),
        (
            lambda d: d["diagram"]["edges"].append(
                {"source": "n0", "target": "n0", "relationship": "calls"}
            ),
            "self_loop_edge",
        ),
        (lambda d: d["diagram"]["edges"].append(d["diagram"]["edges"][0]), "duplicate_edge"),
    ],
)
def test_graph_defects_in_a_realistic_proposal(edit: Any, code: str) -> None:
    data = realistic_proposal()
    edit(data)
    with pytest.raises(InvalidArchitectureOutputError) as caught:
        parse_generated(json.dumps(data))
    assert caught.value.category == FailureCategory.GRAPH
    assert {e["type"] for e in caught.value.diagnostics["errors"]} == {code}


def test_maximum_component_count_from_the_prompt_is_accepted() -> None:
    parsed = parse_generated(json.dumps(realistic_proposal(components=10)))
    assert len(parsed.components) == 10
