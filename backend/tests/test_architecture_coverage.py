"""Requirement coverage matrix, backward compatibility and prompt wording (fake provider only)."""

import copy
import json
import re
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.domain.architecture import (
    PROMPT_VERSION,
    ArchitectureContent,
    DecisionOrigin,
    FabricatedReferenceError,
    FailureCategory,
    GeneratedArchitecture,
    InvalidArchitectureOutputError,
    InvalidCoverageError,
    parse_generated,
    verify_references,
)
from app.llm.architecture_prompt import SYSTEM_PROMPT
from app.models.architecture import ArchitectureProposal
from tests.test_architecture_api import (
    alias_payload,
    arch_url,
    confirmed_project,
    count,
    generate,
    serve,
    valid_payload,
)

IDS = [str(uuid.uuid4()) for _ in range(3)]
CONFIRMED = {uuid.UUID(i) for i in IDS}


def payload(ids: list[str] = IDS) -> dict[str, Any]:
    return copy.deepcopy(valid_payload(ids))


def parsed(data: dict[str, Any]) -> GeneratedArchitecture:
    return parse_generated(json.dumps(data))


# ---- coverage references ----------------------------------------------------------------------


def test_complete_matrix_is_accepted_and_covers_each_requirement_once() -> None:
    content = parsed(payload())
    verify_references(content, CONFIRMED)
    assert sorted(str(e.requirement_id) for e in content.coverage_matrix) == sorted(IDS)


def test_missing_requirement_is_rejected() -> None:
    data = payload()
    data["coverage_matrix"].pop()
    with pytest.raises(InvalidCoverageError) as caught:
        verify_references(parsed(data), CONFIRMED)
    assert caught.value.category == FailureCategory.COVERAGE
    assert caught.value.diagnostics["missing_requirements"] == 1
    assert IDS[2] not in json.dumps(caught.value.diagnostics)


def test_duplicate_requirement_is_rejected() -> None:
    data = payload()
    data["coverage_matrix"].append(copy.deepcopy(data["coverage_matrix"][0]))
    with pytest.raises(InvalidCoverageError) as caught:
        verify_references(parsed(data), CONFIRMED)
    assert caught.value.diagnostics["duplicate_requirements"] == 1


def test_fabricated_requirement_id_is_rejected() -> None:
    data = payload()
    data["coverage_matrix"][0]["requirement_id"] = str(uuid.uuid4())
    with pytest.raises(FabricatedReferenceError):
        verify_references(parsed(data), CONFIRMED)


def test_matrix_may_not_point_at_unknown_components_or_decisions() -> None:
    data = payload()
    data["coverage_matrix"][0]["related_ids"] = ["no-such-component"]
    with pytest.raises(InvalidCoverageError) as caught:
        verify_references(parsed(data), CONFIRMED)
    assert caught.value.diagnostics["unknown_related_ids"] == 1


def test_matrix_may_reference_decisions_as_well_as_components() -> None:
    data = payload()
    data["coverage_matrix"][0]["related_ids"] = ["d-access", "api"]
    verify_references(parsed(data), CONFIRMED)


def test_the_matrix_is_required_for_new_proposals() -> None:
    data = payload()
    del data["coverage_matrix"]
    with pytest.raises(InvalidArchitectureOutputError):
        parsed(data)
    data["coverage_matrix"] = []
    with pytest.raises(InvalidArchitectureOutputError):
        parsed(data)


@pytest.mark.parametrize(
    "change",
    [
        lambda e: e.update(status="partial", gap=None),
        lambda e: e.update(status="unresolved", gap=None),
        lambda e: e.update(status="addressed", related_ids=[]),
        lambda e: e.update(status="verified"),
        lambda e: e.update(extra="x"),
    ],
)
def test_inconsistent_entries_are_rejected(change: Any) -> None:
    data = payload()
    change(data["coverage_matrix"][0])
    with pytest.raises(InvalidArchitectureOutputError):
        parsed(data)


def test_unresolved_entry_needs_a_gap_but_no_component() -> None:
    data = payload()
    data["coverage_matrix"][0].update(status="unresolved", related_ids=[], gap="Client decision")
    verify_references(parsed(data), CONFIRMED)


def test_decision_and_component_ids_must_be_unique_and_present() -> None:
    data = payload()
    data["security_architecture"][0]["id"] = "api"  # clashes with a component id
    with pytest.raises(InvalidArchitectureOutputError):
        parsed(data)
    data = payload()
    del data["security_architecture"][0]["id"]
    with pytest.raises(InvalidArchitectureOutputError):
        parsed(data)


def test_preference_origin_needs_evidence() -> None:
    data = payload()
    data["data_architecture"][0].update(origin="preferred_by_client", supporting_requirement_ids=[])
    with pytest.raises(InvalidArchitectureOutputError):
        parsed(data)
    data["data_architecture"][0]["supporting_requirement_ids"] = [IDS[0]]
    assert parsed(data).data_architecture[0].origin is DecisionOrigin.PREFERRED_BY_CLIENT


# ---- generation endpoint ----------------------------------------------------------------------


def test_generation_stores_a_complete_matrix(client: TestClient, engine: Engine) -> None:
    pid, ids = confirmed_project(client)
    response = generate(client, pid)
    assert response.status_code == 201, response.text
    matrix = response.json()["content"]["coverage_matrix"]
    assert sorted(e["requirement_id"] for e in matrix) == sorted(ids[:2])
    assert count(engine) == 1


@pytest.mark.parametrize("defect", ["duplicate", "fabricated", "unconfirmed"])
def test_defective_matrix_is_rejected_and_nothing_is_saved(
    client: TestClient, engine: Engine, defect: str
) -> None:
    pid, ids = confirmed_project(client, confirm=1)
    data = alias_payload([ids[0]])
    matrix = data["coverage_matrix"]
    if defect == "duplicate":
        matrix.append(copy.deepcopy(matrix[0]))
    elif defect == "fabricated":
        matrix[0]["requirement_id"] = str(uuid.uuid4())
    else:
        matrix.append({**matrix[0], "requirement_id": "R002"})  # real but only proposed
    serve(data)
    response = generate(client, pid)
    assert response.status_code == 422
    assert count(engine) == 0


def test_generation_rejects_a_matrix_that_omits_a_confirmed_requirement(
    client: TestClient, engine: Engine
) -> None:
    pid, ids = confirmed_project(client)
    data = alias_payload(ids[:2])
    data["coverage_matrix"].pop()
    serve(data)
    response = generate(client, pid)
    assert response.status_code == 422
    assert "exactly once" in response.json()["detail"]
    assert count(engine) == 0


# ---- backward compatibility -------------------------------------------------------------------


def legacy_content(ids: list[str]) -> dict[str, Any]:
    """A proposal as stored by prompt architecture-v3: no matrix, no decision ids."""
    data = payload(ids)
    del data["coverage_matrix"]
    for key in (
        "recommended_stack",
        "integrations",
        "data_architecture",
        "security_architecture",
        "non_functional_design",
    ):
        for decision in data[key]:
            decision.pop("id")
    del data["title"], data["summary"]
    return data


def store_legacy(engine: Engine, pid: str, ids: list[str]) -> str:
    with Session(engine) as session:
        proposal = ArchitectureProposal(
            project_id=uuid.UUID(pid),
            title="Legacy draft",
            summary="Created before the coverage matrix existed.",
            status="draft",
            version=1,
            content=legacy_content(ids),
            provider="fake",
            model="fake-model-1",
            prompt_version="architecture-v3",
        )
        session.add(proposal)
        session.commit()
        return str(proposal.id)


def test_legacy_content_loads_without_a_matrix() -> None:
    content = ArchitectureContent.model_validate(legacy_content(IDS))
    assert content.coverage_matrix is None
    verify_references(content, CONFIRMED)  # nothing to check, and no failure


def test_legacy_proposal_is_readable_and_reports_no_coverage(
    client: TestClient, engine: Engine
) -> None:
    pid, ids = confirmed_project(client)
    legacy_id = store_legacy(engine, pid, ids[:2])

    listed = client.get(arch_url(pid)).json()
    assert [p["id"] for p in listed] == [legacy_id]
    body = client.get(arch_url(pid, f"/{legacy_id}")).json()
    assert body["prompt_version"] == "architecture-v3"
    assert body["content"]["coverage_matrix"] is None
    assert body["stale_requirement_ids"] == []


def test_legacy_proposal_keeps_review_actions(client: TestClient, engine: Engine) -> None:
    pid, ids = confirmed_project(client)
    legacy_id = store_legacy(engine, pid, ids[:2])
    url = arch_url(pid, f"/{legacy_id}")
    edited = client.patch(url, json={"title": "Reviewed"})
    assert edited.status_code == 200
    assert edited.json()["content"]["coverage_matrix"] is None
    assert client.patch(url, json={"status": "rejected"}).json()["status"] == "rejected"


def test_new_generation_next_to_a_legacy_draft_leaves_it_untouched(
    client: TestClient, engine: Engine
) -> None:
    pid, ids = confirmed_project(client)
    legacy_id = store_legacy(engine, pid, ids[:2])
    new = generate(client, pid)
    assert new.status_code == 201 and new.json()["version"] == 2
    legacy = client.get(arch_url(pid, f"/{legacy_id}")).json()
    assert legacy["status"] == "draft" and legacy["title"] == "Legacy draft"
    assert legacy["content"]["coverage_matrix"] is None


# ---- prompt and proposal wording --------------------------------------------------------------


def flat(text: str) -> str:
    return " ".join(text.split())


def test_prompt_version_is_bumped() -> None:
    assert PROMPT_VERSION == "architecture-v6"
    assert f"ARCHPILOT_PROMPT_VERSION={PROMPT_VERSION}" in SYSTEM_PROMPT


def test_prompt_separates_requirements_preferences_recommendations_and_open_points() -> None:
    text = flat(SYSTEM_PROMPT)
    for phrase in (
        "Mandatory client requirements",
        "Client preferences",
        "AI implementation recommendations",
        "Assumptions and unresolved questions",
        "origin required_by_client",
        "origin preferred_by_client",
        "origin recommended_by_ai",
        "A valid requirement reference does not prove that a selected technology was required",
        "never silently resolve an open question",
    ):
        assert phrase in text, phrase


def test_prompt_uses_the_atlas_examples() -> None:
    text = flat(SYSTEM_PROMPT)
    for phrase in (
        "React and TypeScript",
        "REST API is required but Node.js or Express is not",
        "PostgreSQL or Azure is a preference, not a mandatory selection",
        "naming a specific vault product is your recommendation",
    ):
        assert phrase in text, phrase


def test_prompt_demands_proposal_language_and_no_forced_stack() -> None:
    text = flat(SYSTEM_PROMPT)
    for phrase in (
        '"proposed", "designed to" and "target"',
        "Never claim that security is already implemented",
        "performance or availability is proven or guaranteed",
        "Do not default to any framework only because this tool happens to use it",
        "explain the alternatives",
    ):
        assert phrase in text, phrase


def test_prompt_asks_for_design_completeness_and_the_matrix() -> None:
    text = flat(SYSTEM_PROMPT)
    for phrase in (
        "document parsing as a separate component from document storage",
        "keyword search mechanism",
        "AI source citation and validation",
        "approval states and rejection reasons",
        "deployment topology",
        "performance, concurrency and backup-restore testing",
        "Every confirmed requirement appears exactly once",
        "Do not repeat requirement descriptions",
        "not proof of coverage",
    ):
        assert phrase in text, phrase
    assert "coverage_matrix" in text


BANNED = re.compile(
    r"already (implemented|secure|met)|is (fully )?(secure|implemented)|"
    r"(proven|guaranteed?|guarantees)|fully covered|\bFastAPI\b",
    re.IGNORECASE,
)


def strings(node: Any) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in strings(v)]
    return []


def test_fake_proposal_follows_the_wording_rules_and_forces_no_framework() -> None:
    data = payload()
    offending = [s for s in strings(data) if BANNED.search(s)]
    assert not offending, offending
    stack = {item["layer"]: item for item in data["recommended_stack"]}
    assert stack["Backend"]["origin"] == "requires_clarification"
    required = [
        d
        for k in ("recommended_stack", "security_architecture")
        for d in data[k]
        if d["origin"] == "required_by_client"
    ]
    assert required == [], "the fake model must not claim client mandates it cannot know"
