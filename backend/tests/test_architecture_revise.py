"""Revise with findings (Phase 7E). Fake provider only; the source draft is never touched."""

import json
import uuid

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.llm.fake_provider import FakeLLMProvider
from app.models.architecture import ArchitectureProposal
from tests.test_architecture_api import (
    alias_payload,
    arch_url,
    confirmed_project,
    count,
    generate,
    req_url,
    serve,
)
from tests.test_architecture_lint import flagged_payload
from tests.test_requirements_api import make_project, use


def revise(client: TestClient, pid: str, proposal_id: str) -> Response:
    response: Response = client.post(arch_url(pid, f"/{proposal_id}/revise"))
    return response


def flagged_draft(client: TestClient) -> tuple[str, list[str], dict]:  # type: ignore[type-arg]
    pid, ids = confirmed_project(client)
    serve(flagged_payload(ids))
    body = generate(client, pid).json()
    assert body["lint_findings"]["findings"]
    return pid, ids, body


def record(engine: Engine, proposal_id: str) -> ArchitectureProposal:
    with Session(engine) as session:
        row = session.get(ArchitectureProposal, uuid.UUID(proposal_id))
        assert row is not None
        session.expunge(row)
        return row


def test_revise_creates_a_new_version_and_leaves_the_source_alone(
    client: TestClient, engine: Engine
) -> None:
    pid, ids, source = flagged_draft(client)
    before = record(engine, source["id"])
    serve(alias_payload(ids))  # the "revised" answer: clean, no findings
    response = revise(client, pid, source["id"])
    assert response.status_code == 201, response.text
    new = response.json()
    assert new["id"] != source["id"]
    assert new["version"] == source["version"] + 1
    assert new["status"] == "draft"
    assert new["prompt_version"] == "architecture-revise-v1"
    assert new["revised_from_id"] == source["id"]
    assert new["revised_from_version"] == source["version"]
    assert new["revised_from_findings"] == len(source["lint_findings"]["findings"])
    assert count(engine) == 2

    after = record(engine, source["id"])
    assert after.status == "draft"
    assert after.content == before.content
    assert after.lint_findings == before.lint_findings
    assert after.title == before.title and after.summary == before.summary
    assert after.revised_from_id is None
    assert after.prompt_version == before.prompt_version


def test_lint_is_rerun_on_the_new_version(client: TestClient) -> None:
    pid, ids, source = flagged_draft(client)
    serve(alias_payload(ids))
    new = revise(client, pid, source["id"]).json()
    assert new["lint_findings"] is not None
    assert new["lint_findings"]["findings"] == []
    assert new["revised_from_findings"] > 0

    # A revision that fixes nothing is linted again and keeps its findings.
    serve(flagged_payload(ids))
    again = revise(client, pid, source["id"]).json()
    assert {f["rule"] for f in again["lint_findings"]["findings"]} == {
        f["rule"] for f in source["lint_findings"]["findings"]
    }


def test_revision_prompt_uses_aliases_findings_and_the_generation_budget(
    client: TestClient,
) -> None:
    pid, ids, source = flagged_draft(client)
    provider = serve(alias_payload(ids))
    assert revise(client, pid, source["id"]).status_code == 201
    assert len(provider.requests) == 1  # one revision per request, no loops
    request = provider.requests[0]
    assert "architecture-revise-v1" in (request.system_prompt or "")
    assert request.max_output_tokens == 8000
    prompt = request.prompt
    assert '"findings"' in prompt and '"draft"' in prompt
    assert "origin_overclaim" in prompt and "R001" in prompt
    assert all(i not in prompt for i in ids)  # the model never sees requirement UUIDs


def test_default_fake_revision_round_trips_the_stored_draft(
    client: TestClient, engine: Engine
) -> None:
    pid, ids, source = flagged_draft(client)
    use(FakeLLMProvider())  # returns the draft it was given
    response = revise(client, pid, source["id"])
    assert response.status_code == 201, response.text
    new = response.json()
    assert new["content"]["recommended_stack"] == source["content"]["recommended_stack"]
    assert new["content"]["coverage_matrix"] == source["content"]["coverage_matrix"]
    assert new["title"] == source["title"]
    assert new["lint_findings"]["findings"] == source["lint_findings"]["findings"]


def test_approved_and_rejected_drafts_cannot_be_revised(client: TestClient, engine: Engine) -> None:
    for status in ("approved", "rejected"):
        pid, ids, source = flagged_draft(client)
        assert (
            client.patch(arch_url(pid, f"/{source['id']}"), json={"status": status}).status_code
            == 200
        )
        provider = serve(alias_payload(ids))
        response = revise(client, pid, source["id"])
        assert response.status_code == 409, response.text
        assert provider.requests == []
        assert len(client.get(arch_url(pid)).json()) == 1


def test_a_draft_without_findings_cannot_be_revised(client: TestClient, engine: Engine) -> None:
    pid, ids = confirmed_project(client)
    serve(alias_payload(ids))
    clean = generate(client, pid).json()
    assert clean["lint_findings"]["findings"] == []
    assert revise(client, pid, clean["id"]).status_code == 409
    assert count(engine) == 1


def test_an_unlinted_old_draft_cannot_be_revised(client: TestClient, engine: Engine) -> None:
    from tests.test_architecture_coverage import store_legacy

    pid, ids = confirmed_project(client)
    legacy_id = store_legacy(engine, pid, ids[:2])
    assert revise(client, pid, legacy_id).status_code == 409


def test_revise_is_isolated_per_project(client: TestClient, engine: Engine) -> None:
    pid_a, ids, source = flagged_draft(client)
    pid_b = make_project(client, "B")
    provider = serve(alias_payload(ids))
    assert revise(client, pid_b, source["id"]).status_code == 404
    assert revise(client, str(uuid.uuid4()), source["id"]).status_code == 404
    assert revise(client, pid_a, str(uuid.uuid4())).status_code == 404
    assert provider.requests == []
    assert count(engine) == 1
    assert client.get(arch_url(pid_b)).json() == []


def test_stale_citations_block_the_revision(client: TestClient, engine: Engine) -> None:
    pid, ids, source = flagged_draft(client)
    assert client.patch(req_url(pid, ids[0]), json={"title": "Changed"}).status_code == 200
    provider = serve(alias_payload(ids))
    assert revise(client, pid, source["id"]).status_code == 409
    assert provider.requests == []
    assert count(engine) == 1


def test_invalid_revision_output_saves_nothing(client: TestClient, engine: Engine) -> None:
    pid, ids, source = flagged_draft(client)
    serve(json.dumps({"title": "x"}))
    assert revise(client, pid, source["id"]).status_code == 422
    assert count(engine) == 1
    assert record(engine, source["id"]).status == "draft"
