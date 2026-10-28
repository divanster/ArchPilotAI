"""Retrieval-grounded architecture generation (Phase 7C): keyword passages, aliases, citations.

Fake LLM provider only; no network. Runs on SQLite and PostgreSQL through the shared fixtures.
"""

import json
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.domain.architecture import (
    ArchitectureContent,
    FabricatedReferenceError,
    FailureCategory,
    InvalidArchitectureOutputError,
    PassageCatalog,
    PassageSource,
    RetrievalInfo,
    parse_generated,
)
from app.llm.architecture_prompt import SYSTEM_PROMPT
from app.llm.fake_provider import FakeLLMProvider
from app.models.architecture import ArchitectureProposal
from app.models.document import Document
from app.repositories.architecture import ArchitectureRepository
from app.repositories.chunk import ChunkRepository
from app.repositories.document import DocumentRepository
from app.repositories.project import ProjectRepository
from app.repositories.requirement import RequirementRepository
from app.services.architecture_context import (
    ArchitectureContextRetriever,
    RequirementQuery,
    passage_cost,
    query_terms,
)
from app.services.architecture_generation import ArchitectureGenerationService
from tests.test_architecture_api import (
    alias_payload,
    arch_url,
    confirmed_project,
    count,
    generate,
    serve,
)
from tests.test_requirements_api import make_doc, make_project, use

FACT_A = "The system shall list projects for each Zephyrquill tenant."
DOC_A = f"Zephyrquill catalogue notes.\n\n{FACT_A}\n\nUnrelated: office parking rules."
DOC_B = "Brontovane appendix.\n\nThe system must list projects and respond within 2 seconds."


def index(client: TestClient, pid: str, did: str) -> None:
    response = client.post(f"/api/projects/{pid}/documents/{did}/index", json={})
    assert response.status_code == 200, response.text


def prompt_context(provider: FakeLLMProvider) -> dict[str, Any]:
    prompt = provider.requests[-1].prompt
    context: dict[str, Any] = json.loads(prompt.split("\n", 2)[2].rsplit("\nEND_CONTEXT_", 1)[0])
    return context


def project_with_index(client: TestClient) -> tuple[str, list[str], str]:
    pid, ids = confirmed_project(client)
    did = make_doc(client, pid, DOC_A, "zephyr.txt")
    index(client, pid, did)
    return pid, ids, did


def cited_payload(ids: list[str], aliases: list[str]) -> dict[str, Any]:
    data = alias_payload(ids[:2])
    data["data_architecture"][0]["source_passage_ids"] = aliases
    data["assumptions"][0]["source_passage_ids"] = aliases
    return data


def make_service(
    session: Session, provider: Any, *, budget: int = 4000, **kwargs: Any
) -> ArchitectureGenerationService:
    return ArchitectureGenerationService(
        provider,
        ProjectRepository(session),
        DocumentRepository(session),
        RequirementRepository(session),
        ArchitectureRepository(session),
        max_requirements=60,
        max_context_chars=24_000,
        max_unconfirmed_chars=budget,
        max_output_tokens=8000,
        chunks=ChunkRepository(session),
        **kwargs,
    )


# ---- passages, aliases and citations ----------------------------------------------------------


def test_indexed_passages_get_aliases_and_are_saved_as_cited_sources(
    client: TestClient, engine: Engine
) -> None:
    pid, _, did = project_with_index(client)
    provider = FakeLLMProvider()
    use(provider)
    response = generate(client, pid)
    assert response.status_code == 201, response.text
    body = response.json()

    context = prompt_context(provider)
    passages = context["unconfirmed_context"]
    assert [p["id"] for p in passages] == ["P001"]
    assert passages[0]["status"] == "unconfirmed" and passages[0]["document"] == "zephyr.txt"
    assert "Zephyrquill" in passages[0]["excerpt"]

    content = ArchitectureContent.model_validate(body["content"])
    [source] = content.source_passages
    assert str(source.document_id) == did and source.filename == "zephyr.txt"
    with Session(engine) as session:
        text = session.get(Document, uuid.UUID(did)).extracted_text  # type: ignore[union-attr]
    assert text[source.source_start : source.source_end] == passages[0]["excerpt"]
    # The fake cites P001 on a decision and an assumption; both were resolved to the chunk id.
    assert content.data_architecture[0].source_passage_ids == [source.chunk_id]
    assert content.assumptions[0].source_passage_ids == [source.chunk_id]
    assert content.retrieval is not None and content.retrieval.status == "used"
    stored = json.dumps(body["content"])
    assert "P001" not in stored
    assert str(source.chunk_id) not in provider.requests[0].prompt


def test_requirement_and_passage_aliases_are_separate_namespaces(client: TestClient) -> None:
    pid, ids, _ = project_with_index(client)
    provider = FakeLLMProvider()
    use(provider)
    assert generate(client, pid).status_code == 201
    context = prompt_context(provider)
    requirement_aliases = {r["id"] for r in context["confirmed_requirements"]}
    passage_aliases = {p["id"] for p in context["unconfirmed_context"]}
    assert requirement_aliases == {"R001", "R002"} and passage_aliases == {"P001"}
    assert requirement_aliases.isdisjoint(passage_aliases)


@pytest.mark.parametrize(
    "bad",
    [["P009"], ["p001"], [" P001"], ["R001"], [str(uuid.uuid4())], ["P01"], [""], [7]],
)
def test_unknown_passage_alias_is_rejected_without_saving(
    client: TestClient, engine: Engine, bad: list[Any]
) -> None:
    pid, ids, _ = project_with_index(client)
    serve(cited_payload(ids, bad))
    response = generate(client, pid)
    assert response.status_code == 422
    assert count(engine) == 0
    for value in bad:
        assert str(value).strip() == "" or str(value) not in response.text


def test_passage_alias_cannot_be_used_as_a_requirement_reference(
    client: TestClient, engine: Engine
) -> None:
    pid, ids, _ = project_with_index(client)
    data = alias_payload(ids[:2])
    data["data_architecture"][0]["supporting_requirement_ids"] = ["P001"]
    serve(data)
    assert generate(client, pid).status_code == 422
    assert count(engine) == 0


def test_citing_a_passage_when_none_were_offered_is_rejected(
    client: TestClient, engine: Engine
) -> None:
    pid, ids = confirmed_project(client)  # nothing indexed
    serve(cited_payload(ids, ["P001"]))
    assert generate(client, pid).status_code == 422
    assert count(engine) == 0


def test_model_cannot_write_reserved_provenance_fields(client: TestClient, engine: Engine) -> None:
    pid, ids, _ = project_with_index(client)
    data = alias_payload(ids[:2])
    data["source_passages"] = []
    serve(data)
    assert generate(client, pid).status_code == 422
    data = alias_payload(ids[:2])
    data["retrieval"] = {"status": "used"}
    serve(data)
    assert generate(client, pid).status_code == 422
    assert count(engine) == 0


def test_phases_and_coverage_do_not_take_passage_ids(client: TestClient) -> None:
    pid, ids, _ = project_with_index(client)
    data = alias_payload(ids[:2])
    data["implementation_phases"][0]["source_passage_ids"] = ["P001"]
    serve(data)
    assert generate(client, pid).status_code == 422
    data = alias_payload(ids[:2])
    data["coverage_matrix"][0]["source_passage_ids"] = ["P001"]
    serve(data)
    assert generate(client, pid).status_code == 422


def test_failed_passage_reference_reports_only_schema_locations(
    caplog: pytest.LogCaptureFixture,
) -> None:
    source = PassageSource(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename="a.txt",
        source_start=0,
        source_end=5,
    )
    info = RetrievalInfo(
        status="used", passages_provided=1, unindexed_documents=0, budget_chars=100, truncated=False
    )
    catalog = PassageCatalog({"P001": source}, info)
    secret = "SECRET-PASSAGE-ALIAS"
    data = alias_payload([str(uuid.uuid4())])
    data["risks"][0]["source_passage_ids"] = ["P001", secret]
    with pytest.raises(FabricatedReferenceError) as caught:
        parse_generated(json.dumps(data), passages=catalog)
    error = caught.value
    assert error.category is FailureCategory.REFERENCE
    assert error.diagnostics["namespace"] == "passage"
    assert error.diagnostics["locations"] == [
        {"path": "risks[0].source_passage_ids[1]", "reason": "unknown_alias"}
    ]
    assert secret not in str(error) and secret not in json.dumps(error.diagnostics)


# ---- context retrieval ------------------------------------------------------------------------


def test_unindexed_documents_generate_without_excerpts_and_are_reported(
    client: TestClient, engine: Engine
) -> None:
    pid, _ = confirmed_project(client)
    provider = FakeLLMProvider()
    use(provider)
    response = generate(client, pid)
    assert response.status_code == 201
    assert prompt_context(provider)["unconfirmed_context"] == []
    retrieval = response.json()["content"]["retrieval"]
    assert retrieval["status"] == "not_indexed" and retrieval["passages_provided"] == 0
    assert retrieval["unindexed_documents"] == 1
    # Generation never indexes anything on its own.
    with Session(engine) as session:
        assert ChunkRepository(session).scan(uuid.UUID(pid), None, 10) == []


def test_partially_indexed_project_reports_unindexed_documents(client: TestClient) -> None:
    pid, _, _ = project_with_index(client)
    make_doc(client, pid, DOC_B, "later.txt")  # uploaded but not indexed
    provider = FakeLLMProvider()
    use(provider)
    retrieval = generate(client, pid).json()["content"]["retrieval"]
    assert retrieval["status"] == "used" and retrieval["unindexed_documents"] == 2
    names = {p["document"] for p in prompt_context(provider)["unconfirmed_context"]}
    assert names == {"zephyr.txt"}


def test_indexed_but_unmatched_documents_are_reported(client: TestClient) -> None:
    pid, _ = confirmed_project(client)
    did = make_doc(client, pid, "Quokka habitat survey 1999 xyzzy plugh", "off.txt")
    index(client, pid, did)
    provider = FakeLLMProvider()
    use(provider)
    retrieval = generate(client, pid).json()["content"]["retrieval"]
    assert retrieval["status"] == "no_matches"
    assert prompt_context(provider)["unconfirmed_context"] == []


def test_zero_budget_disables_document_context(client: TestClient, engine: Engine) -> None:
    pid, _, _ = project_with_index(client)
    provider = FakeLLMProvider()
    with Session(engine) as session:
        proposal = make_service(session, provider, budget=0).generate(uuid.UUID(pid))
        content = ArchitectureContent.model_validate(proposal.content)
    assert content.retrieval is not None and content.retrieval.status == "disabled"
    assert prompt_context(provider)["unconfirmed_context"] == []


def test_character_budget_caps_the_passages(client: TestClient, engine: Engine) -> None:
    pid, _ = confirmed_project(client)
    paragraphs = [
        f"The system shall list projects, variant number {n} {'x' * 800}" for n in range(8)
    ]
    did = make_doc(client, pid, "\n\n".join(paragraphs), "many.txt")
    index(client, pid, did)
    provider = FakeLLMProvider()
    with Session(engine) as session:
        proposal = make_service(session, provider, budget=2000).generate(uuid.UUID(pid))
        content = ArchitectureContent.model_validate(proposal.content)
    passages = prompt_context(provider)["unconfirmed_context"]
    assert 0 < len(passages) < 8
    assert sum(passage_cost("many.txt", p["excerpt"]) for p in passages) <= 2000
    assert content.retrieval is not None and content.retrieval.truncated is True
    assert content.retrieval.passages_provided == len(passages)


def test_passage_count_is_capped(client: TestClient, engine: Engine) -> None:
    pid, _ = confirmed_project(client)
    paragraphs = [f"The system shall list projects, variant {n} {'y' * 400}" for n in range(10)]
    index(client, pid, make_doc(client, pid, "\n\n".join(paragraphs), "n.txt"))
    provider = FakeLLMProvider()
    with Session(engine) as session:
        make_service(session, provider, max_passages=2).generate(uuid.UUID(pid))
    assert len(prompt_context(provider)["unconfirmed_context"]) == 2


def test_context_is_deterministic_and_ordered_by_document_position(
    client: TestClient, engine: Engine
) -> None:
    pid, _, _ = project_with_index(client)
    index(client, pid, make_doc(client, pid, DOC_B, "aaa-first.txt"))
    with Session(engine) as session:
        first = make_service(session, FakeLLMProvider())._build_context(uuid.UUID(pid))
        second = make_service(session, FakeLLMProvider())._build_context(uuid.UUID(pid))
    assert first[0] == second[0] and first[2] == second[2]
    passages = first[0]["unconfirmed_context"]
    assert [p["id"] for p in passages] == ["P001", "P002"]
    assert [p["document"] for p in passages] == ["aaa-first.txt", "zephyr.txt"]


def test_retrieval_is_isolated_per_project(client: TestClient, engine: Engine) -> None:
    pid_a, _ = confirmed_project(client)
    pid_b, _ = confirmed_project(client)
    index(client, pid_b, make_doc(client, pid_b, "Other tenant: " + FACT_A, "secret-b.txt"))
    provider = FakeLLMProvider()
    use(provider)
    response = generate(client, pid_a)
    assert response.status_code == 201
    assert "secret-b.txt" not in provider.requests[0].prompt
    assert "Zephyrquill" not in provider.requests[0].prompt
    assert response.json()["content"]["source_passages"] == []
    # And the other direction: B's own document is offered to B.
    provider_b = FakeLLMProvider()
    use(provider_b)
    assert generate(client, pid_b).status_code == 201
    assert "secret-b.txt" in provider_b.requests[0].prompt


def test_retriever_without_chunk_repository_is_disabled() -> None:
    retriever = ArchitectureContextRetriever(None, scan_limit=10, per_requirement=3, max_passages=5)
    result = retriever.retrieve(uuid.uuid4(), [], has_documents=True, budget_chars=1000)
    assert result.passages == [] and result.info.status == "disabled"


def test_query_terms_are_deterministic_short_word_free_and_capped() -> None:
    words = " ".join(f"word{n:02d}" for n in range(40))
    terms = query_terms("An ID is a UUID", f"Is it ok? {words} word00")
    assert terms == ["uuid", *[f"word{n:02d}" for n in range(19)]]
    assert terms == query_terms("An ID is a UUID", f"Is it ok? {words} word00")
    assert query_terms("a b", "of to") == []


# ---- prompt -----------------------------------------------------------------------------------


def test_prompt_treats_passages_as_evidence_not_requirements() -> None:
    assert "architecture-v6" in SYSTEM_PROMPT
    assert "EVIDENCE, never\ndecisions and never client requirements" in SYSTEM_PROMPT
    assert "source_passage_ids" in SYSTEM_PROMPT and "P001" in SYSTEM_PROMPT
    assert "assumption, a wish or an open question stays" in SYSTEM_PROMPT
    # The earlier requirement / preference / recommendation rules are unchanged.
    for phrase in ("required_by_client", "preferred_by_client", "recommended_by_ai", "R001"):
        assert phrase in SYSTEM_PROMPT


# ---- editing and backward compatibility -------------------------------------------------------


def generated_with_citation(client: TestClient) -> tuple[str, dict[str, Any]]:
    pid, _, _ = project_with_index(client)
    use(FakeLLMProvider())
    body = generate(client, pid).json()
    assert body["content"]["source_passages"]
    return pid, body


def test_review_edits_keep_recorded_sources(client: TestClient) -> None:
    pid, body = generated_with_citation(client)
    content = body["content"]
    original = json.loads(json.dumps(content["source_passages"]))
    content["source_passages"][0]["filename"] = "tampered.txt"  # provenance is not editable
    content["retrieval"]["status"] = "disabled"
    content["risks"][0]["description"] = "Edited by the reviewer."
    response = client.patch(arch_url(pid, f"/{body['id']}"), json={"content": content})
    assert response.status_code == 200, response.text
    saved = response.json()["content"]
    assert saved["source_passages"] == original
    assert saved["retrieval"]["status"] == "used"
    assert saved["risks"][0]["description"] == "Edited by the reviewer."


def test_review_edit_cannot_cite_a_forged_source(client: TestClient) -> None:
    pid, body = generated_with_citation(client)
    content = body["content"]
    forged = {
        "chunk_id": str(uuid.uuid4()),
        "document_id": str(uuid.uuid4()),
        "filename": "forged.txt",
        "source_start": 0,
        "source_end": 3,
    }
    content["source_passages"] = [*content["source_passages"], forged]
    content["risks"][0]["source_passage_ids"] = [forged["chunk_id"]]
    response = client.patch(arch_url(pid, f"/{body['id']}"), json={"content": content})
    assert response.status_code == 422
    stored = client.get(arch_url(pid, f"/{body['id']}")).json()["content"]
    assert "forged.txt" not in json.dumps(stored)


def test_proposals_without_citations_display_unchanged(client: TestClient, engine: Engine) -> None:
    pid, ids = confirmed_project(client)
    use(FakeLLMProvider())
    body = generate(client, pid).json()
    with Session(engine) as session:
        row = session.get(ArchitectureProposal, uuid.UUID(body["id"]))
        assert row is not None
        legacy = json.loads(json.dumps(row.content))
        for key in ("source_passages", "retrieval"):
            legacy.pop(key, None)
        row.content = legacy
        session.commit()
    again = client.get(arch_url(pid, f"/{body['id']}"))
    assert again.status_code == 200
    content = again.json()["content"]
    assert content["source_passages"] == [] and content["retrieval"] is None
    assert content["coverage_matrix"]  # untouched
    # Existing generation-time documents of other versions are not rewritten by reading.
    with Session(engine) as session:
        row = session.get(ArchitectureProposal, uuid.UUID(body["id"]))
        assert row is not None and "source_passages" not in row.content


def test_project_without_documents_reports_no_documents(client: TestClient, engine: Engine) -> None:
    pid = make_project(client)
    with Session(engine) as session:
        retriever = ArchitectureContextRetriever(
            ChunkRepository(session), scan_limit=10, per_requirement=3, max_passages=5
        )
        result = retriever.retrieve(
            uuid.UUID(pid),
            [RequirementQuery("R001", "list projects", "the system shall list projects")],
            has_documents=False,
            budget_chars=1000,
        )
    assert result.info.status == "no_documents" and result.passages == []


def test_invalid_content_citing_unrecorded_chunk_fails_schema() -> None:
    data = alias_payload([str(uuid.uuid4())])
    data["risks"][0]["source_passage_ids"] = [str(uuid.uuid4())]
    with pytest.raises(InvalidArchitectureOutputError):
        parse_generated(json.dumps({**data, "title": "t", "summary": "s"}))
