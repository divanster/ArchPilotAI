"""Request-scoped requirement aliases (R001, R002, ...): offline, no real provider."""

import copy
import json
import logging
import random
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.domain.architecture import (
    FabricatedReferenceError,
    InvalidCoverageError,
    build_alias_map,
    content_to_json,
    parse_generated,
    resolve_aliases,
    verify_references,
)
from app.llm.contracts import LLMRequest, LLMResponse
from app.models.architecture import ArchitectureProposal
from app.repositories.architecture import ArchitectureRepository
from app.repositories.document import DocumentRepository
from app.repositories.project import ProjectRepository
from app.repositories.requirement import RequirementRepository
from app.services.architecture_generation import ArchitectureGenerationService
from tests.test_architecture_api import (
    alias_payload,
    arch_url,
    confirmed_project,
    count,
    generate,
    serve,
)

IDS = [str(uuid.uuid4()) for _ in range(3)]
UUIDS = [uuid.UUID(i) for i in IDS]
ALIASES = build_alias_map(UUIDS)
SECRET = "SECRET-ALIAS-VALUE-7c1e"


def data() -> dict[str, Any]:
    return copy.deepcopy(alias_payload(IDS))


def parse(payload: dict[str, Any]) -> Any:
    return parse_generated(json.dumps(payload), aliases=ALIASES)


def rejected(payload: dict[str, Any]) -> FabricatedReferenceError:
    with pytest.raises(FabricatedReferenceError) as caught:
        parse(payload)
    return caught.value


# ---- the alias map ----------------------------------------------------------------------------


def test_aliases_are_sequential_and_ordered_by_uuid() -> None:
    assert list(ALIASES) == ["R001", "R002", "R003"]
    assert list(ALIASES.values()) == sorted(UUIDS, key=str)


def test_alias_map_is_stable_whatever_the_input_order() -> None:
    shuffled = UUIDS[:]
    for seed in range(5):
        random.Random(seed).shuffle(shuffled)
        assert build_alias_map(shuffled) == ALIASES
    assert build_alias_map(UUIDS + UUIDS) == ALIASES  # duplicates cannot shift aliases


def test_alias_width_grows_past_999_requirements() -> None:
    big = build_alias_map(uuid.uuid4() for _ in range(1000))
    assert "R0001" in big and "R1000" in big and len(big) == 1000


def test_empty_snapshot_has_no_aliases() -> None:
    assert build_alias_map([]) == {}


# ---- resolution -------------------------------------------------------------------------------


def test_valid_aliases_resolve_to_the_original_uuids_everywhere() -> None:
    content = parse(data())
    assert {str(e.requirement_id) for e in content.coverage_matrix} == set(IDS)
    cited = {
        str(r) for d in content.security_architecture for r in d.supporting_requirement_ids
    } | {str(r) for c in content.components for r in c.requirement_ids}
    assert cited and cited <= set(IDS)
    verify_references(content, set(UUIDS))  # existing validators run on UUIDs


def test_resolution_does_not_mutate_its_input() -> None:
    original = data()
    snapshot = copy.deepcopy(original)
    resolve_aliases(original, ALIASES)
    assert original == snapshot


def test_stored_content_holds_uuids_not_aliases() -> None:
    stored = json.dumps(content_to_json(parse(data())))
    assert all(i in stored for i in IDS)
    assert not any(alias in stored for alias in ALIASES)


def test_every_requirement_reference_field_is_resolved() -> None:
    payload = data()
    first = "R001"
    for group in (
        "recommended_stack",
        "integrations",
        "data_architecture",
        "security_architecture",
        "non_functional_design",
    ):
        for decision in payload[group]:
            decision["supporting_requirement_ids"] = [first]
    for section in (
        "components",
        "risks",
        "assumptions",
        "open_questions",
        "alternatives",
        "implementation_phases",
    ):
        for item in payload[section]:
            item["requirement_ids"] = [first]
    resolved = resolve_aliases(payload, ALIASES)
    expected = str(ALIASES[first])
    assert expected in json.dumps(resolved) and first not in json.dumps(
        {k: v for k, v in resolved.items() if k != "coverage_matrix"}
    ).replace("R001-", "")
    for entry in resolved["coverage_matrix"]:
        assert entry["requirement_id"] in IDS


# ---- unknown references -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("R999", "unknown_alias"),
        ("r001", "unknown_alias"),  # no case folding
        (" R001 ", "unknown_alias"),  # no trimming
        ("R1", "unknown_alias"),  # no zero-padding repair
        ("R001,R002", "unknown_alias"),
        ("api", "unknown_alias"),  # a component id is not a requirement alias
        (SECRET, "unknown_alias"),
        (str(uuid.uuid4()), "uuid_instead_of_alias"),
        (IDS[0], "uuid_instead_of_alias"),  # even a real UUID of this project
        (1, "not_a_string"),
        (None, "not_a_string"),
    ],
)
def test_unknown_references_are_rejected_without_repair(value: Any, reason: str) -> None:
    payload = data()
    payload["risks"][0]["requirement_ids"] = [value]
    err = rejected(payload)
    assert err.code == "fabricated_reference"
    assert err.diagnostics["locations"] == [
        {"path": "risks[0].requirement_ids[0]", "reason": reason}
    ]
    assert err.diagnostics["invalid_references"] == 1
    assert "R999" not in json.dumps(err.diagnostics) and SECRET not in json.dumps(err.diagnostics)
    assert str(err) and SECRET not in str(err)


@pytest.mark.parametrize(
    ("edit", "path"),
    [
        (
            lambda p: p["coverage_matrix"][1].update(requirement_id="R404"),
            "coverage_matrix[1].requirement_id",
        ),
        (
            lambda p: p["security_architecture"][0].update(
                supporting_requirement_ids=["R001", "R404"]
            ),
            "security_architecture[0].supporting_requirement_ids[1]",
        ),
        (
            lambda p: p["components"][0].update(requirement_ids=["R404"]),
            "components[0].requirement_ids[0]",
        ),
        (
            lambda p: p["implementation_phases"][0].update(requirement_ids=["R404"]),
            "implementation_phases[0].requirement_ids[0]",
        ),
        (
            lambda p: p["assumptions"][0].update(requirement_ids=["R404"]),
            "assumptions[0].requirement_ids[0]",
        ),
    ],
)
def test_diagnostics_name_the_schema_location(edit: Any, path: str) -> None:
    payload = data()
    edit(payload)
    err = rejected(payload)
    assert [loc["path"] for loc in err.diagnostics["locations"]] == [path]
    assert err.diagnostics["aliases_available"] == len(ALIASES)


def test_many_invalid_references_are_counted_and_capped() -> None:
    payload = data()
    for entry in payload["coverage_matrix"]:
        entry["requirement_id"] = "R404"
    for decision in payload["security_architecture"]:
        decision["supporting_requirement_ids"] = ["R404"] * 50
    err = rejected(payload)
    diag = err.diagnostics
    assert diag["invalid_references"] > len(diag["locations"]) > 0
    assert diag["locations_omitted"] == diag["invalid_references"] - len(diag["locations"])


def test_model_chosen_keys_never_reach_the_diagnostics() -> None:
    payload = data()
    payload[SECRET] = [{"requirement_ids": ["R404"]}]  # unknown top-level key is not walked
    payload["risks"][0]["requirement_ids"] = ["R404"]
    err = rejected(payload)
    assert SECRET not in json.dumps(err.diagnostics)


def test_malformed_shapes_fall_through_to_schema_validation() -> None:
    from app.domain.architecture import InvalidArchitectureOutputError

    payload = data()
    payload["risks"][0]["requirement_ids"] = "R001"  # not a list
    with pytest.raises(InvalidArchitectureOutputError):
        parse(payload)


def test_without_an_alias_map_uuids_are_still_accepted() -> None:
    # Existing callers and stored drafts: UUID references, no request map.
    from tests.test_architecture_api import valid_payload

    content = parse_generated(json.dumps(valid_payload(IDS)))
    verify_references(content, set(UUIDS))


# ---- coverage with aliases --------------------------------------------------------------------


def test_complete_alias_coverage_passes() -> None:
    verify_references(parse(data()), set(UUIDS))


def test_missing_alias_coverage_is_rejected() -> None:
    payload = data()
    payload["coverage_matrix"].pop()
    with pytest.raises(InvalidCoverageError) as caught:
        verify_references(parse(payload), set(UUIDS))
    assert caught.value.diagnostics["missing_requirements"] == 1


def test_duplicate_alias_coverage_is_rejected() -> None:
    payload = data()
    payload["coverage_matrix"].append(copy.deepcopy(payload["coverage_matrix"][0]))
    with pytest.raises(InvalidCoverageError) as caught:
        verify_references(parse(payload), set(UUIDS))
    assert caught.value.diagnostics["duplicate_requirements"] == 1


# ---- separate namespaces ----------------------------------------------------------------------


def test_requirement_aliases_are_not_valid_component_or_decision_references() -> None:
    payload = data()
    payload["coverage_matrix"][0]["related_ids"] = ["R001"]
    with pytest.raises(InvalidCoverageError) as caught:
        verify_references(parse(payload), set(UUIDS))
    assert caught.value.diagnostics["unknown_related_ids"] == 1


def test_a_component_named_like_an_alias_is_left_alone() -> None:
    payload = data()
    old = payload["components"][0]["id"]
    payload["components"][0]["id"] = "R002"
    for node in payload["diagram"]["nodes"]:
        node["id"] = "R002" if node["id"] == old else node["id"]
    for edge in payload["diagram"]["edges"]:
        edge["source"] = "R002" if edge["source"] == old else edge["source"]
        edge["target"] = "R002" if edge["target"] == old else edge["target"]
    for entry in payload["coverage_matrix"]:
        entry["related_ids"] = ["R002" if r == old else r for r in entry["related_ids"]]
    payload["coverage_matrix"][0]["related_ids"] = ["R002"]
    content = parse(payload)
    assert content.components[0].id == "R002"  # not rewritten to a UUID
    assert any("R002" in e.related_ids for e in content.coverage_matrix)
    verify_references(content, set(UUIDS))


# ---- generation service -----------------------------------------------------------------------


def service(session: Session, provider: Any) -> ArchitectureGenerationService:
    return ArchitectureGenerationService(
        provider,
        ProjectRepository(session),
        DocumentRepository(session),
        RequirementRepository(session),
        ArchitectureRepository(session),
        max_requirements=60,
        max_context_chars=24000,
        max_unconfirmed_chars=0,
        max_output_tokens=8000,
    )


class Canned:
    def __init__(self, text: str) -> None:
        self.text = text

    def generate(self, request: LLMRequest) -> LLMResponse:
        return LLMResponse(text=self.text, provider="p", model="m", latency_ms=1)


def test_prompt_context_uses_stable_aliases_in_order(client: TestClient, engine: Engine) -> None:
    pid, ids = confirmed_project(client)
    with Session(engine) as session:
        first = service(session, Canned(""))._build_context(uuid.UUID(pid))
        second = service(session, Canned(""))._build_context(uuid.UUID(pid))
    context, aliases, _ = first
    assert [r["id"] for r in context["confirmed_requirements"]] == ["R001", "R002"]
    assert first == second
    assert set(aliases.values()) == {uuid.UUID(i) for i in ids[:2]}
    assert not any(i in json.dumps(context) for i in ids)


def test_alias_maps_are_per_request_and_per_project(client: TestClient, engine: Engine) -> None:
    pid_a, ids_a = confirmed_project(client)
    pid_b, ids_b = confirmed_project(client)
    with Session(engine) as session:
        _, map_a, _ = service(session, Canned(""))._build_context(uuid.UUID(pid_a))
        _, map_b, _ = service(session, Canned(""))._build_context(uuid.UUID(pid_b))
    assert list(map_a) == list(map_b) == ["R001", "R002"]
    assert set(map_a.values()).isdisjoint(map_b.values())
    assert set(map_b.values()) == {uuid.UUID(i) for i in ids_b[:2]}


def test_a_proposal_for_one_project_resolves_only_through_its_own_map(
    client: TestClient, engine: Engine
) -> None:
    pid_a, ids_a = confirmed_project(client)
    pid_b, ids_b = confirmed_project(client)
    serve(alias_payload(ids_a[:2]))
    assert generate(client, pid_a).status_code == 201
    assert generate(client, pid_b).status_code == 201  # same aliases, B's own requirements
    summary_b = client.get(arch_url(pid_b)).json()[0]
    body_b = client.get(arch_url(pid_b, f"/{summary_b['id']}")).json()
    cited = {e["requirement_id"] for e in body_b["content"]["coverage_matrix"]}
    assert cited == set(ids_b[:2]) and cited.isdisjoint(ids_a)


def test_saved_proposal_references_are_uuids_in_the_database(
    client: TestClient, engine: Engine
) -> None:
    pid, ids = confirmed_project(client)
    serve(alias_payload(ids[:2]))
    assert generate(client, pid).status_code == 201
    with Session(engine) as session:
        row = session.scalars(select(ArchitectureProposal)).one()
        stored = json.dumps(row.content)
        assert row.prompt_version == "architecture-v6"
    assert all(i in stored for i in ids[:2])
    assert not any(f'"R00{n}"' in stored for n in range(1, 10))


def test_failed_alias_validation_saves_nothing_and_logs_no_values(
    client: TestClient, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    pid, ids = confirmed_project(client)
    payload = alias_payload(ids[:2])
    payload["risks"][0]["requirement_ids"] = [SECRET]
    serve(payload)
    with caplog.at_level(logging.INFO):
        response = generate(client, pid)
    assert response.status_code == 422
    assert count(engine) == 0
    logged = "\n".join(
        r.getMessage() for r in caplog.records if r.name == "app.services.architecture_generation"
    )
    assert "invalid_references" in logged and "risks[0].requirement_ids[0]" in logged
    assert SECRET not in logged and SECRET not in response.text
    assert not any(i in logged for i in ids)
