"""Post-generation semantic lint (Phase 7D). Fake provider only; findings are advisory."""

import copy
import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.domain.architecture import ArchitectureContent, GeneratedArchitecture, content_to_json
from app.domain.semantic_lint import (
    LintConfig,
    ProductTerm,
    RequirementText,
    find_terms,
    lint_proposal,
)
from app.models.architecture import ArchitectureProposal
from tests.test_architecture_api import (
    alias_payload,
    arch_url,
    confirmed_project,
    generate,
    serve,
    valid_payload,
)
from tests.test_architecture_coverage import legacy_content, store_legacy

R1, R2 = uuid.uuid4(), uuid.uuid4()
IDS = [str(R1), str(R2)]


def base() -> dict[str, Any]:
    return copy.deepcopy(valid_payload(IDS))


def load(data: dict[str, Any]) -> ArchitectureContent:
    cls = GeneratedArchitecture if "title" in data else ArchitectureContent
    return cls.model_validate(data)


def run(
    data: dict[str, Any],
    reqs: dict[uuid.UUID, RequirementText] | None = None,
    config: LintConfig | None = None,
) -> list[tuple[str, str | None]]:
    content = load(data)
    report = lint_proposal(data["title"], data["summary"], content, reqs or {}, config)
    return [(f.rule.value, f.path) for f in report.findings]


def required_item(tech: str, cited: uuid.UUID = R1, **extra: Any) -> dict[str, Any]:
    item = base()["recommended_stack"][1]
    item.update(
        origin="required_by_client",
        supporting_requirement_ids=[str(cited)],
        technology=tech,
        **extra,
    )
    return dict(item)


def with_stack(item: dict[str, Any]) -> dict[str, Any]:
    data = base()
    data["recommended_stack"] = [item]
    return data


REQS = {
    R1: RequirementText("Secrets", "Store secrets in Azure Key Vault.", "use Key Vault"),
    R2: RequirementText("Runtime", "The service runs on Node.js."),
}


# ---- 1. origin ---------------------------------------------------------------------------------


def test_default_fake_output_has_no_findings() -> None:
    assert run(base()) == []


def test_origin_overclaim_is_flagged_for_unmentioned_product() -> None:
    data = with_stack(required_item("Azure Blob Storage"))
    assert run(data, REQS) == [("origin_overclaim", "recommended_stack[0]")]


def test_origin_overclaim_names_the_term_from_the_list() -> None:
    data = with_stack(required_item("Redis", decision="Use Redis and Express as the cache layer"))
    content = load(data)
    report = lint_proposal("t", "s", content, REQS)
    assert report.findings[0].terms == ["Express", "Redis"]


def test_product_named_in_a_cited_requirement_is_not_flagged() -> None:
    assert run(with_stack(required_item("Azure Key Vault")), REQS) == []


def test_alias_spelling_matches_the_same_product() -> None:
    # The requirement says "Key Vault"; the item says "Azure Key Vault".
    reqs = {R1: RequirementText("Secrets", "Keep keys in Key Vault.")}
    assert run(with_stack(required_item("Azure Key Vault")), reqs) == []
    assert run(with_stack(required_item("Nodejs")), {R1: RequirementText("r", "Node.js")}) == []


def test_only_cited_requirements_count() -> None:
    # Node.js is in R2, but the item cites R1 only.
    assert run(with_stack(required_item("Node.js", cited=R1)), REQS) == [
        ("origin_overclaim", "recommended_stack[0]")
    ]
    assert run(with_stack(required_item("Node.js", cited=R2)), REQS) == []


@pytest.mark.parametrize("origin", ["recommended_by_ai", "assumed", "requires_clarification"])
def test_other_origins_are_not_checked(origin: str) -> None:
    item = required_item("Redis")
    item["origin"] = origin
    assert run(with_stack(item), REQS) == []


# ---- 1b. preference overclaim ------------------------------------------------------------------


def preferred_item(tech: str, cited: uuid.UUID = R1, **extra: Any) -> dict[str, Any]:
    item = required_item(tech, cited, **extra)
    item["origin"] = "preferred_by_client"
    return item


AZURE_PREF = {R1: RequirementText("Platform", "We would prefer to host on Azure.")}


def test_preference_overclaim_is_flagged_for_services_under_a_platform_preference() -> None:
    for tech in ("Azure Key Vault", "Azure App Service", "Azure Blob Storage"):
        assert run(with_stack(preferred_item(tech)), AZURE_PREF) == [
            ("preference_overclaim", "recommended_stack[0]")
        ], tech


def test_preference_overclaim_reports_the_terms_and_a_distinct_rule() -> None:
    data = with_stack(preferred_item("Azure Key Vault"))
    finding = lint_proposal("t", "s", load(data), AZURE_PREF).findings[0]
    assert finding.rule.value == "preference_overclaim"
    assert finding.terms == ["Azure Key Vault"]
    assert "preference" in finding.message


def test_preferred_service_named_by_the_requirement_is_not_flagged() -> None:
    reqs = {R1: RequirementText("Secrets", "Prefer Azure Key Vault for secrets.")}
    assert run(with_stack(preferred_item("Azure Key Vault")), reqs) == []


def test_a_bare_platform_name_is_not_a_service() -> None:
    item = preferred_item("Azure", decision="Host on the Azure platform")
    assert run(with_stack(item), AZURE_PREF) == []


def test_preference_check_ignores_uncited_and_unconfirmed_requirements() -> None:
    assert run(with_stack(preferred_item("Azure Blob Storage")), {}) == []
    # Cited R1 is not among the confirmed requirements; only R2 is.
    assert run(with_stack(preferred_item("Azure Blob Storage", cited=R1)), {R2: REQS[R2]}) == []


def test_only_cited_requirements_count_for_preferences() -> None:
    reqs = {R1: AZURE_PREF[R1], R2: RequirementText("Storage", "Use Azure Blob Storage.")}
    assert run(with_stack(preferred_item("Azure Blob Storage", cited=R1)), reqs) == [
        ("preference_overclaim", "recommended_stack[0]")
    ]
    assert run(with_stack(preferred_item("Azure Blob Storage", cited=R2)), reqs) == []


def test_required_items_keep_the_origin_rule() -> None:
    assert run(with_stack(required_item("Azure Blob Storage")), AZURE_PREF) == [
        ("origin_overclaim", "recommended_stack[0]")
    ]


def test_generic_wording_is_not_a_product() -> None:
    item = required_item("Relational database", decision="Express the data as relational tables")
    assert run(with_stack(item), REQS) == []


def test_case_sensitive_terms_ignore_ordinary_words() -> None:
    assert find_terms("express delivery in react to events", LintConfig()) == set()
    assert find_terms("Express.js with React", LintConfig()) == {"Express", "React"}
    assert find_terms("Express", LintConfig()) == {"Express"}  # a bare technology label
    assert find_terms("Built with Express to serve APIs", LintConfig()) == {"Express"}


def test_term_inside_a_longer_word_is_not_a_match() -> None:
    assert find_terms("Redistribution, Dockerfile-less, Postgresql-like", LintConfig()) == {
        "PostgreSQL"
    }
    assert find_terms("preact and nodejsx", LintConfig()) == set()


def test_unverifiable_citation_is_not_flagged() -> None:
    # Cited requirement is unknown (e.g. no longer confirmed): nothing to compare with.
    assert run(with_stack(required_item("Redis")), {}) == []


def test_extra_terms_are_configurable() -> None:
    item = required_item("Acme Vault")
    assert run(with_stack(item), REQS) == []
    config = LintConfig().with_extra_terms(["Acme Vault", "  ", "redis"])
    assert run(with_stack(item), REQS, config) == [("origin_overclaim", "recommended_stack[0]")]
    assert len(config.product_terms) == len(LintConfig().product_terms) + 1
    custom = LintConfig(product_terms=(ProductTerm("Acme Vault"),))
    assert run(with_stack(required_item("Redis")), REQS, custom) == []


def test_origin_applies_to_every_decision_group() -> None:
    data = base()
    for group in ("integrations", "data_architecture", "security_architecture"):
        item = required_item("Auth0", id=f"x-{group}", decision="Sign in through Auth0")
        item.pop("layer"), item.pop("technology")
        data[group] = [item]
    found = run(data, REQS)
    assert sorted(p or "" for _, p in found) == [
        "data_architecture[0]",
        "integrations[0]",
        "security_architecture[0]",
    ]


# ---- 2. wording --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "phrase", ["is implemented", "are implemented", "ensures", "guarantees", "proven"]
)
def test_completed_implementation_wording_is_flagged(phrase: str) -> None:
    data = base()
    data["solution_overview"] = f"Access control {phrase} for all users."
    assert run(data) == [("completed_implementation_wording", "solution_overview")]


def test_wording_is_flagged_in_title_summary_and_nested_text() -> None:
    data = base()
    data["title"] = "Guarantees uptime"
    data["summary"] = "Encryption IS IMPLEMENTED."
    data["components"][0]["responsibility"] = "Ensures the UI stays fast."
    paths = {p for _, p in run(data)}
    assert paths == {"title", "summary", "components[0].responsibility"}


@pytest.mark.parametrize(
    "text",
    [
        "The design should be implemented in phase two.",
        "Controls will be implemented later.",
        "This is not yet proven in production.",
        "It was never proven at this scale.",
        "The approach is unproven.",
        "Please ensure backups run; the guaranteed-delivery queue is optional.",
        "Implementation of the plan is described below.",
    ],
)
def test_planning_language_is_not_flagged(text: str) -> None:
    data = base()
    data["solution_overview"] = text
    assert run(data) == []


def test_wording_phrases_are_configurable() -> None:
    data = base()
    data["solution_overview"] = "Fully certified design."
    config = LintConfig(wording_phrases=("fully certified",))
    assert run(data, config=config) == [("completed_implementation_wording", "solution_overview")]


def test_one_finding_per_text_even_with_several_phrases() -> None:
    data = base()
    data["solution_overview"] = "It ensures and guarantees results; they are implemented."
    content = load(data)
    report = lint_proposal("t", "s", content, {})
    assert len(report.findings) == 1
    assert report.findings[0].terms == ["are implemented", "ensures", "guarantees"]


# ---- 3. coverage -------------------------------------------------------------------------------


def all_addressed() -> dict[str, Any]:
    data = base()
    for entry in data["coverage_matrix"]:
        entry.update(status="addressed", gap=None)
    return data


def test_all_addressed_is_flagged() -> None:
    assert run(all_addressed()) == [("coverage_all_addressed", None)]


@pytest.mark.parametrize("status", ["partial", "unresolved"])
def test_one_open_entry_clears_the_all_addressed_flag(status: str) -> None:
    data = all_addressed()
    data["coverage_matrix"][0].update(status=status, gap="Open point.")
    if status == "unresolved":
        data["coverage_matrix"][0]["related_ids"] = []
    assert run(data) == []


def test_legacy_content_without_matrix_is_not_flagged() -> None:
    content = ArchitectureContent.model_validate(legacy_content(IDS))
    assert lint_proposal("t", "s", content, REQS).findings == []


SEARCH = {
    R1: RequirementText("Document search", "Users can search all documents."),
    R2: RequirementText("Upload", "Users upload files up to 50 MB."),
}


def test_backend_requirement_mapped_only_to_the_frontend_is_flagged() -> None:
    data = base()
    data["coverage_matrix"][0]["related_ids"] = ["web"]
    data["coverage_matrix"][1]["related_ids"] = ["web"]
    found = run(data, SEARCH)
    assert found == [
        ("frontend_only_backend_requirement", "coverage_matrix[0]"),
        ("frontend_only_backend_requirement", "coverage_matrix[1]"),
    ]


def test_backend_component_or_decision_clears_the_frontend_rule() -> None:
    data = base()
    data["coverage_matrix"][0]["related_ids"] = ["web", "api"]
    data["coverage_matrix"][1]["related_ids"] = ["web", "d-backend"]
    assert run(data, SEARCH) == []


def test_frontend_only_requirement_without_backend_implication_is_fine() -> None:
    data = base()
    data["coverage_matrix"][0]["related_ids"] = ["web"]
    reqs = {R1: RequirementText("Theme", "Users can pick a dark theme.")}
    assert run(data, reqs) == []
    # "research" does not start with "search"
    reqs = {R1: RequirementText("Research", "A research dashboard in the browser.")}
    assert run(data, reqs) == []


def test_backend_keywords_are_configurable() -> None:
    data = base()
    data["coverage_matrix"][0]["related_ids"] = ["web"]
    reqs = {R1: RequirementText("Export", "Users export reports.")}
    assert run(data, reqs) == []
    config = LintConfig(backend_keywords=("export",))
    assert run(data, reqs, config) == [("frontend_only_backend_requirement", "coverage_matrix[0]")]


# ---- 4. citations ------------------------------------------------------------------------------


def with_retrieval(data: dict[str, Any], provided: int) -> dict[str, Any]:
    data["retrieval"] = {
        "status": "used",
        "passages_provided": provided,
        "unindexed_documents": 0,
        "budget_chars": 4000,
        "truncated": False,
    }
    return data


def test_supplied_but_uncited_passages_are_reported() -> None:
    assert run(with_retrieval(base(), 3)) == [("passages_not_cited", None)]


def test_no_passages_supplied_is_not_reported() -> None:
    assert run(with_retrieval(base(), 0)) == []
    assert run(base()) == []  # stored before retrieval existed


def test_cited_passage_clears_the_citation_report() -> None:
    chunk = uuid.uuid4()
    data = with_retrieval(base(), 1)
    data["source_passages"] = [
        {
            "chunk_id": str(chunk),
            "document_id": str(uuid.uuid4()),
            "filename": "a.txt",
            "source_start": 0,
            "source_end": 5,
        }
    ]
    data["risks"][0]["source_passage_ids"] = [str(chunk)]
    assert run(data) == []


# ---- storage, API and old drafts ---------------------------------------------------------------


def stored(engine: Engine, pid: str) -> ArchitectureProposal:
    with Session(engine) as session:
        proposal = session.query(ArchitectureProposal).filter_by(project_id=uuid.UUID(pid)).one()
        session.expunge(proposal)
        return proposal


def flagged_payload(ids: list[str]) -> dict[str, Any]:
    data = copy.deepcopy(alias_payload(ids))
    data["solution_overview"] = "Access control is implemented and ensures privacy."
    data["recommended_stack"][1].update(
        origin="required_by_client",
        supporting_requirement_ids=["R001"],
        technology="Azure Key Vault",
    )
    for entry in data["coverage_matrix"]:
        entry.update(status="addressed", gap=None)
    return data


def test_generated_proposal_stores_findings_without_touching_content(
    client: TestClient, engine: Engine
) -> None:
    pid, ids = confirmed_project(client)
    payload = flagged_payload(ids)
    serve(payload)
    response = generate(client, pid)
    assert response.status_code == 201, response.text
    body = response.json()
    rules = {f["rule"] for f in body["lint_findings"]["findings"]}
    assert rules == {
        "origin_overclaim",
        "completed_implementation_wording",
        "coverage_all_addressed",
    }
    assert body["status"] == "draft"
    # The wording the model used is stored verbatim.
    assert body["content"]["solution_overview"] == payload["solution_overview"]
    record = stored(engine, pid)
    assert record.lint_findings is not None
    assert record.lint_findings["findings"] == body["lint_findings"]["findings"]
    assert record.content["solution_overview"] == payload["solution_overview"]
    assert record.content["recommended_stack"][1]["technology"] == "Azure Key Vault"
    assert (
        client.get(arch_url(pid, f"/{record.id}")).json()["lint_findings"] == body["lint_findings"]
    )


def test_clean_proposal_stores_an_empty_report(client: TestClient) -> None:
    pid, ids = confirmed_project(client)
    serve(alias_payload(ids))
    body = generate(client, pid).json()
    assert body["lint_findings"]["findings"] == []


def test_findings_are_advisory_and_do_not_block_approval(client: TestClient) -> None:
    pid, ids = confirmed_project(client)
    serve(flagged_payload(ids))
    body = generate(client, pid).json()
    assert body["lint_findings"]["findings"]
    approved = client.patch(arch_url(pid, f"/{body['id']}"), json={"status": "approved"})
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    assert approved.json()["lint_findings"] == body["lint_findings"]


def test_old_drafts_have_no_findings_and_keep_working(client: TestClient, engine: Engine) -> None:
    pid, ids = confirmed_project(client)
    legacy_id = store_legacy(engine, pid, ids[:2])
    url = arch_url(pid, f"/{legacy_id}")
    assert stored(engine, pid).lint_findings is None
    body = client.get(url).json()
    assert body["lint_findings"] is None
    assert client.get(arch_url(pid)).status_code == 200
    assert client.patch(url, json={"status": "approved"}).json()["lint_findings"] is None


def test_editing_text_refreshes_findings_but_never_changes_content(
    client: TestClient, engine: Engine
) -> None:
    pid, ids = confirmed_project(client)
    serve(flagged_payload(ids))
    body = generate(client, pid).json()
    before = copy.deepcopy(stored(engine, pid).content)
    url = arch_url(pid, f"/{body['id']}")
    edited = client.patch(url, json={"summary": "The design guarantees nothing yet."})
    assert edited.status_code == 200
    paths = {f["path"] for f in edited.json()["lint_findings"]["findings"]}
    assert "summary" in paths
    assert stored(engine, pid).content == before
    fixed = client.patch(url, json={"summary": "A reviewed summary."})
    assert "summary" not in {f["path"] for f in fixed.json()["lint_findings"]["findings"]}
    assert stored(engine, pid).content == before


def test_content_edit_relints_and_keeps_provenance(client: TestClient, engine: Engine) -> None:
    pid, ids = confirmed_project(client)
    serve(alias_payload(ids))
    body = generate(client, pid).json()
    content = body["content"]
    content["solution_overview"] = "Everything is proven."
    edited = client.patch(arch_url(pid, f"/{body['id']}"), json={"content": content})
    assert edited.status_code == 200, edited.text
    assert [f["rule"] for f in edited.json()["lint_findings"]["findings"]] == [
        "completed_implementation_wording"
    ]


def test_lint_never_modifies_its_input() -> None:
    data = flagged_payload_direct()
    content = load(data)
    snapshot = json.dumps(content_to_json(content), sort_keys=True)
    lint_proposal("Title is implemented", "ensures", content, REQS)
    assert json.dumps(content_to_json(content), sort_keys=True) == snapshot


def flagged_payload_direct() -> dict[str, Any]:
    data = base()
    data["solution_overview"] = "Guaranteed? It guarantees."
    for entry in data["coverage_matrix"]:
        entry.update(status="addressed", gap=None)
    return data


def test_a_lint_failure_never_loses_the_proposal(
    client: TestClient, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.services.architecture_lint as module

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("lint bug")

    monkeypatch.setattr(module, "lint_proposal", boom)
    pid, ids = confirmed_project(client)
    serve(alias_payload(ids))
    response = generate(client, pid)
    assert response.status_code == 201
    assert response.json()["lint_findings"] is None
    assert stored(engine, pid).lint_findings is None
