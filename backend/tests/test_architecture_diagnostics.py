"""Safe diagnostics for rejected architecture output (mocked provider, no network)."""

import copy
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.domain.architecture import (
    RESERVED_KEYS,
    ArchitectureContent,
    ArchitectureError,
    DecisionOrigin,
    FabricatedReferenceError,
    FailureCategory,
    GeneratedArchitecture,
    InvalidArchitectureOutputError,
    NodeType,
    Severity,
    parse_generated,
    verify_references,
)
from app.llm.architecture_prompt import SYSTEM_PROMPT
from app.llm.contracts import LLMRequest, LLMResponse, TokenUsage
from app.models.architecture import ArchitectureProposal
from app.repositories.architecture import ArchitectureRepository
from app.repositories.document import DocumentRepository
from app.repositories.project import ProjectRepository
from app.repositories.requirement import RequirementRepository
from app.services.architecture_generation import ArchitectureGenerationService
from tests.test_architecture_api import confirmed_project, valid_payload
from tests.test_llm import OK_BODY, provider_for

SECRET = "SECRET-MODEL-CONTENT-9f3a"
IDS = [str(uuid.uuid4()), str(uuid.uuid4())]


def payload() -> dict[str, Any]:
    return copy.deepcopy(valid_payload(IDS))


def fail(raw: str | dict[str, Any], **kw: Any) -> InvalidArchitectureOutputError:
    text = raw if isinstance(raw, str) else json.dumps(raw)
    with pytest.raises(InvalidArchitectureOutputError) as caught:
        parse_generated(text, **kw)
    return caught.value


def test_valid_output_still_parses() -> None:
    assert isinstance(parse_generated(json.dumps(payload())), GeneratedArchitecture)


# ---- one test per failure category -----------------------------------------------------------


def test_json_parse_failure_category() -> None:
    err = fail(f"```json\n{SECRET}")
    assert err.code == "invalid_output"
    assert err.category == FailureCategory.JSON_PARSE
    assert err.diagnostics["reason"] == "invalid_json"
    assert err.diagnostics["starts_with_code_fence"] is True
    assert err.diagnostics["raw_chars"] == len(f"```json\n{SECRET}")
    assert isinstance(err.diagnostics["error_offset"], int)


def test_truncated_output_is_an_output_size_failure() -> None:
    err = fail('{"title": "cut off', finish_reason="length")
    assert err.category == FailureCategory.OUTPUT_SIZE
    assert err.diagnostics["reason"] == "truncated_by_token_limit"
    assert err.diagnostics["finish_reason"] == "length"


def test_oversized_response_is_an_output_size_failure() -> None:
    err = fail(json.dumps(payload()), max_chars=50)
    assert err.category == FailureCategory.OUTPUT_SIZE
    assert err.diagnostics["reason"] == "response_too_large"


def test_complete_json_with_length_finish_reason_is_not_truncation() -> None:
    err = fail('{"title": "x"}', finish_reason="length")
    assert err.category == FailureCategory.SCHEMA


def test_truncation_midway_through_a_full_proposal_is_not_repaired() -> None:
    raw = json.dumps(payload())
    err = fail(raw[: len(raw) // 2], finish_reason="length")
    assert err.category == FailureCategory.OUTPUT_SIZE
    assert err.diagnostics["reason"] == "truncated_by_token_limit"
    assert err.diagnostics["raw_chars"] == len(raw) // 2


def test_truncated_json_without_length_reason_stays_a_parse_failure() -> None:
    raw = json.dumps(payload())
    err = fail(raw[: len(raw) // 2], finish_reason="stop")
    assert err.category == FailureCategory.JSON_PARSE


def test_extra_fields_are_rejected_without_echoing_their_names() -> None:
    data = payload()
    data["zz_model_invented_key"] = 1
    data["components"][0]["zz_nested_invented_key"] = 1
    err = fail(data)
    assert err.category == FailureCategory.SCHEMA
    errors = err.diagnostics["errors"]
    assert {e["type"] for e in errors} == {"extra_forbidden"}
    assert {e["path"] for e in errors} == {"?", "components[0].?"}
    assert "zz_" not in json.dumps(err.diagnostics)


def test_every_missing_required_field_is_reported_by_schema_path() -> None:
    data = payload()
    for key in ("solution_overview", "diagram", "implementation_phases"):
        data.pop(key)
    del data["components"][0]["responsibility"]
    err = fail(data)
    assert err.category == FailureCategory.SCHEMA
    found = {(e["path"], e["type"]) for e in err.diagnostics["errors"]}
    assert found == {
        ("solution_overview", "missing"),
        ("diagram", "missing"),
        ("implementation_phases", "missing"),
        ("components[0].responsibility", "missing"),
    }


@pytest.mark.parametrize(
    ("change", "path", "kind"),
    [
        (lambda p: p.pop("solution_overview"), "solution_overview", "missing"),
        (lambda p: p.update(title=""), "title", "string_too_short"),
        (lambda p: p.update(title="x" * 201), "title", "string_too_long"),
        (lambda p: p.update(components="nope"), "components", "list_type"),
        (lambda p: p["components"][0].update(type="script"), "components[0].type", "enum"),
        (lambda p: p["risks"].append({"description": "d"}), None, "missing"),
        (
            lambda p: p["security_architecture"][0].update(supporting_requirement_ids=["x"]),
            "security_architecture[0].supporting_requirement_ids[0]",
            "uuid_parsing",
        ),
        (
            lambda p: p["security_architecture"][0].update(supporting_requirement_ids=[]),
            "security_architecture[0]",
            "evidence_required",
        ),
        (lambda p: p["components"].append(p["components"][0]), "<root>", "duplicate_component_id"),
    ],
)
def test_schema_failures_report_field_paths_and_error_types(
    change: Callable[[dict[str, Any]], Any], path: str | None, kind: str
) -> None:
    data = payload()
    change(data)
    err = fail(data)
    assert err.category == FailureCategory.SCHEMA
    errors = err.diagnostics["errors"]
    assert err.diagnostics["error_count"] >= 1
    assert any(e["type"] == kind and (path is None or e["path"] == path) for e in errors), errors


@pytest.mark.parametrize(
    ("change", "kind"),
    [
        (
            lambda p: p["diagram"]["edges"].append(
                {"source": "web", "target": "ghost", "relationship": "calls"}
            ),
            "unknown_edge_node",
        ),
        (
            lambda p: p["diagram"]["edges"].append(
                {"source": "web", "target": "web", "relationship": "loops"}
            ),
            "self_loop_edge",
        ),
        (lambda p: p["diagram"]["edges"].append(p["diagram"]["edges"][0]), "duplicate_edge"),
        (lambda p: p["diagram"]["nodes"].append(p["diagram"]["nodes"][0]), "duplicate_node_id"),
    ],
)
def test_graph_failures_use_fixed_codes(change: Callable[[dict[str, Any]], Any], kind: str) -> None:
    data = payload()
    change(data)
    err = fail(data)
    assert err.category == FailureCategory.GRAPH
    assert err.diagnostics["errors"] == [{"path": "diagram", "type": kind, "count": 1}]


def test_reference_failure_reports_counts_only() -> None:
    data = payload()
    content = GeneratedArchitecture.model_validate(data)
    with pytest.raises(FabricatedReferenceError) as caught:
        verify_references(content, {uuid.UUID(IDS[0])})
    err = caught.value
    assert err.code == "fabricated_reference"
    assert err.category == FailureCategory.REFERENCE
    assert set(err.diagnostics) == {
        "cited_distinct",
        "not_confirmed_distinct",
        "confirmed_available",
    }
    assert err.diagnostics["not_confirmed_distinct"] >= 1
    assert IDS[1] not in json.dumps(err.diagnostics)


# ---- redaction --------------------------------------------------------------------------------


def test_unknown_and_model_chosen_names_are_sanitised() -> None:
    data = payload()
    data[f"{SECRET}_section"] = []
    data["components"][0][f"{SECRET}_field"] = SECRET
    data["security_architecture"][0]["supporting_requirement_ids"] = [SECRET]
    data["risks"] = {SECRET: SECRET}
    err = fail(data)
    rendered = json.dumps(err.diagnostics) + str(err) + repr(err)
    assert SECRET not in rendered
    paths = {e["path"] for e in err.diagnostics["errors"]}
    assert "?" in paths and "components[0].?" in paths
    assert err.__cause__ is None  # the ValidationError, which embeds the input, is not chained


@pytest.mark.parametrize(
    "raw",
    [
        f"{SECRET} not json",
        f'{{"title": "{SECRET}"',
        json.dumps({"title": SECRET}),
        json.dumps([SECRET]),
        json.dumps({"title": "x" * 300, "summary": SECRET}),
    ],
)
def test_no_failure_exposes_model_content(raw: str) -> None:
    err = fail(raw, finish_reason="length")
    assert SECRET not in json.dumps(err.diagnostics) + str(err) + repr(err.args)
    assert err.__cause__ is None and err.__suppress_context__


# ---- service: provider metadata and logging ---------------------------------------------------


class Canned:
    provider_name = "canned"
    model_name = "canned-1"

    def __init__(self, response: LLMResponse) -> None:
        self.response = response

    def generate(self, request: LLMRequest) -> LLMResponse:
        return self.response


def service(session: Session, provider: Canned, **kw: int) -> ArchitectureGenerationService:
    return ArchitectureGenerationService(
        provider,
        ProjectRepository(session),
        DocumentRepository(session),
        RequirementRepository(session),
        ArchitectureRepository(session),
        max_requirements=60,
        max_context_chars=24000,
        max_unconfirmed_chars=0,
        max_output_tokens=kw.get("max_output_tokens", 4000),
    )


def proposals(engine: Engine) -> int:
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(ArchitectureProposal)) or 0


def run(
    engine: Engine, pid: str, response: LLMResponse, **kw: int
) -> tuple[ArchitectureError, Any]:
    with Session(engine) as session, pytest.raises(ArchitectureError) as caught:
        service(session, Canned(response), **kw).generate(uuid.UUID(pid))
    return caught.value, None


def test_provider_metadata_survives_validation_failure(
    client: TestClient, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    pid, _ = confirmed_project(client)
    response = LLMResponse(
        text=f'{{"title": "{SECRET}"',
        provider="kpmg_workbench",
        model="gpt-4.1-2025-04-14",
        latency_ms=1234,
        usage=TokenUsage(input_tokens=900, output_tokens=4000, total_tokens=4900),
        finish_reason="length",
    )
    with caplog.at_level(logging.INFO):
        err, _ = run(engine, pid, response)
    assert err.category == FailureCategory.OUTPUT_SIZE
    assert err.metadata is not None
    assert err.metadata.as_dict() == {
        "provider": "kpmg_workbench",
        "model": "gpt-4.1-2025-04-14",
        "latency_ms": 1234,
        "input_tokens": 900,
        "output_tokens": 4000,
        "finish_reason": "length",
    }
    assert proposals(engine) == 0
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "category=output_size" in logged and "gpt-4.1-2025-04-14" in logged
    assert SECRET not in logged


def test_unavailable_metadata_is_null_not_fabricated(client: TestClient, engine: Engine) -> None:
    pid, _ = confirmed_project(client)
    response = LLMResponse(text="not json", provider="p", model="m", latency_ms=0)
    err, _ = run(engine, pid, response)
    assert err.metadata is not None
    assert err.metadata.input_tokens is None
    assert err.metadata.output_tokens is None
    assert err.metadata.finish_reason is None


def test_unsafe_finish_reason_is_dropped(client: TestClient, engine: Engine) -> None:
    pid, _ = confirmed_project(client)
    response = LLMResponse(
        text="x", provider="p", model="m", latency_ms=1, finish_reason=f"Stop: {SECRET}"
    )
    err, _ = run(engine, pid, response)
    assert err.metadata is not None and err.metadata.finish_reason is None


def test_oversized_response_is_refused_by_the_service(client: TestClient, engine: Engine) -> None:
    pid, ids = confirmed_project(client)
    text = json.dumps(valid_payload(ids))
    response = LLMResponse(text=text, provider="p", model="m", latency_ms=1)
    err, _ = run(engine, pid, response, max_output_tokens=2)
    assert err.category == FailureCategory.OUTPUT_SIZE
    assert proposals(engine) == 0


def test_reference_failure_keeps_metadata_and_logs_counts(
    client: TestClient, engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    pid, _ = confirmed_project(client)
    response = LLMResponse(
        text=json.dumps(payload()),
        provider="p",
        model="m",
        latency_ms=5,
        usage=TokenUsage(input_tokens=1, output_tokens=2),
        finish_reason="stop",
    )
    with caplog.at_level(logging.WARNING):
        err, _ = run(engine, pid, response)
    assert isinstance(err, FabricatedReferenceError)
    assert err.metadata is not None and err.metadata.finish_reason == "stop"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "category=reference" in logged and "invalid_references" in logged
    assert IDS[0] not in logged and IDS[1] not in logged


def test_http_response_for_invalid_output_stays_generic(client: TestClient) -> None:
    from tests.test_architecture_api import generate, serve

    pid, _ = confirmed_project(client)
    serve(json.dumps({"title": SECRET, SECRET: 1}))
    response = generate(client, pid)
    assert response.status_code == 422
    assert SECRET not in response.text
    assert "valid architecture proposal" in response.json()["detail"]


# ---- Workbench finish_reason ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reported", "expected"),
    [("stop", "stop"), ("length", "length"), (None, None), (7, None), ("Bad Value!", None)],
)
def test_workbench_provider_reports_finish_reason(reported: object, expected: str | None) -> None:
    body = copy.deepcopy(OK_BODY)
    body["choices"][0]["finish_reason"] = reported  # type: ignore[index]
    provider, _ = provider_for(lambda request: httpx.Response(200, json=body))
    result = provider.generate(LLMRequest(prompt="p"))
    assert result.finish_reason == expected


# ---- prompt versus schema ---------------------------------------------------------------------


def schema_field_names() -> set[str]:
    names: set[str] = set()
    seen: set[str] = set()

    def walk(schema: dict[str, Any], defs: dict[str, Any]) -> None:
        for name, prop in schema.get("properties", {}).items():
            names.add(name)
            if name in RESERVED_KEYS:  # written by the service, never by the model
                continue
            for ref in _refs(prop):
                key = ref.rsplit("/", 1)[-1]
                if key not in seen:
                    seen.add(key)
                    walk(defs[key], defs)

    full = GeneratedArchitecture.model_json_schema()
    walk(full, full.get("$defs", {}))
    return names


def _refs(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_refs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_refs(item))
    return found


def test_prompt_names_every_schema_field() -> None:
    missing = {n for n in schema_field_names() if n not in SYSTEM_PROMPT}
    assert not missing, missing


def test_prompt_enum_values_match_the_schema() -> None:
    for enum in (DecisionOrigin, NodeType, Severity):
        for member in enum:
            assert member.value in SYSTEM_PROMPT, member
    prompt_types = SYSTEM_PROMPT.split("node type one of ")[1].split(";")[0].split("|")
    assert {t.strip() for t in prompt_types} == {m.value for m in NodeType}


def test_prompt_states_the_enforced_output_constraints() -> None:
    flat = " ".join(SYSTEM_PROMPT.split())
    for phrase in (
        "Strict JSON only",
        "no Markdown, no code fences",
        "do not add any additional fields",
        "Every required field must be present",
        "Strings must be non-empty",
        "null is allowed only for the optional fields",
        "at least one component",
        "at least one objective",
        "one diagram node",
        "at most 8 components",
        "3 to 4 items",
        "about one sentence per text field",
        "at most 2 entries in each of alternatives_considered",
        "at most 2 requirement ids per item",
        "never omit a supporting requirement id",
        "Component ids must be unique",
        "diagram node ids must be unique",
        "No duplicate edges",
        "no self-loops",
        'the exact aliases (for example "R001")',
    ):
        assert phrase in flat, phrase


def test_prompt_numeric_limits_match_the_schema() -> None:
    flat = " ".join(SYSTEM_PROMPT.split())
    schema = GeneratedArchitecture.model_json_schema()
    props = schema["properties"]
    limits = {
        "recommended_stack": 20,
        "integrations": 20,
        "data_architecture": 15,
        "security_architecture": 15,
        "non_functional_design": 15,
        "alternatives": 15,
        "risks": 20,
        "implementation_phases": 12,
    }
    for key, limit in limits.items():
        assert props[key]["maxItems"] == limit, key
    assert props["components"]["maxItems"] >= 10
    assert schema["$defs"]["Diagram"]["properties"]["nodes"]["maxItems"] == 40
    assert schema["$defs"]["Diagram"]["properties"]["edges"]["maxItems"] == 80
    for text in (
        "at most 200",
        "at most 1500",
        "at most 5000",
        "at most 64",
        "must never be exceeded: 20 recommended_stack, 20 integrations, 15 in each",
        "12 phases, 40 diagram nodes and 80 edges",
        "at most 50 per list",
    ):
        assert text in flat, text


def test_prompt_optional_fields_are_the_schema_optional_fields() -> None:
    defs = GeneratedArchitecture.model_json_schema()["$defs"]
    nullable = {
        (name, field)
        for name, d in defs.items()
        for field, spec in d.get("properties", {}).items()
        if any(opt.get("type") == "null" for opt in spec.get("anyOf", []))
    }
    assert nullable == {
        ("Component", "technology"),
        ("OpenQuestion", "why_it_matters"),
        ("CoverageEntry", "gap"),
        ("Decision", "id"),  # optional only so older stored proposals still load
        ("StackItem", "id"),
    }


def test_architecture_output_budget_default_is_8000_tokens() -> None:
    from app.core.config import Settings

    field = Settings.model_fields["architecture_max_output_tokens"]
    assert field.default == 8000
    assert any(getattr(m, "le", None) == 8000 for m in field.metadata)


def test_required_top_level_keys_are_all_requested() -> None:
    required = set(GeneratedArchitecture.model_json_schema()["required"])
    keys_line = SYSTEM_PROMPT.split("with these keys:")[1].split('A "decision"')[0]
    requested = {
        k.strip().rstrip(".") for k in keys_line.replace("\n", " ").split(",") if k.strip()
    }
    assert required <= requested


def test_content_model_matches_generated_model_minus_title_and_summary() -> None:
    generated = set(GeneratedArchitecture.model_fields)
    assert generated - set(ArchitectureContent.model_fields) == {"title", "summary"}
