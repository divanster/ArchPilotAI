"""Revise with findings (Phase 7E). Fake provider only; the source draft is never touched."""

import json
import uuid

from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.domain.revision import finding_instruction, revision_outcome
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
    assert new["prompt_version"] == "architecture-revise-v2"
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
    assert "architecture-revise-v2" in (request.system_prompt or "")
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


# ---- Phase 7F: targeted instructions and outcome -----------------------------------------------


def finding(rule: str, path: str | None = None, terms: list[str] | None = None) -> dict:  # type: ignore[type-arg]
    return {"rule": rule, "path": path, "message": "m", "terms": terms or []}


def test_origin_and_preference_instructions_name_the_exact_path_and_product() -> None:
    for rule in ("origin_overclaim", "preference_overclaim"):
        text = finding_instruction(finding(rule, "recommended_stack[1]", ["Azure Key Vault"]))
        assert "recommended_stack[1]" in text and "Azure Key Vault" in text
        assert "recommended_by_ai" in text
        assert "separate new item" in text  # the second allowed fix
    assert "required" in finding_instruction(finding("origin_overclaim", "integrations[0]"))
    assert "preferred" in finding_instruction(finding("preference_overclaim", "integrations[0]"))


def test_wording_instruction_targets_only_the_flagged_field() -> None:
    text = finding_instruction(finding("completed_implementation_wording", "solution_overview"))
    assert "solution_overview" in text and "only the field" in text and "proposal language" in text


def test_coverage_instruction_requires_a_gap_for_partial_entries() -> None:
    text = finding_instruction(finding("coverage_all_addressed"))
    assert "partial or unresolved" in text and "stated gap" in text
    assert "concrete mechanism" in text and "open question" in text


def test_citation_instruction_forbids_citing_only_to_silence_the_finding() -> None:
    text = finding_instruction(finding("passages_not_cited"))
    assert "P-aliases" in text and "open question" in text
    assert "only to silence" in text


def test_every_lint_rule_has_a_specific_instruction() -> None:
    from app.domain.semantic_lint import LintRule

    generic = finding_instruction(finding("something_else"))
    for rule in LintRule:
        assert finding_instruction(finding(rule.value, "x", ["t"])) != generic


def test_prompt_carries_one_targeted_instruction_per_finding(client: TestClient) -> None:
    pid, ids, source = flagged_draft(client)
    provider = serve(alias_payload(ids))
    assert revise(client, pid, source["id"]).status_code == 201
    request = provider.requests[0]
    context = json.loads(request.prompt.split("\n", 2)[2].rsplit("\nEND_CONTEXT_", 1)[0])
    by_rule = {f["rule"]: f for f in context["findings"]}
    assert set(by_rule) == {
        "origin_overclaim",
        "completed_implementation_wording",
        "coverage_all_addressed",
    }
    origin = by_rule["origin_overclaim"]
    assert "recommended_stack[1]" in origin["instruction"]
    assert "Azure Key Vault" in origin["instruction"]
    assert "stated gap" in by_rule["coverage_all_addressed"]["instruction"]
    assert "solution_overview" in by_rule["completed_implementation_wording"]["instruction"]
    assert "Follow each instruction" in (request.system_prompt or "")


def test_outcome_reports_resolved_and_still_present_per_rule() -> None:
    before = {
        "findings": [
            finding("origin_overclaim", "recommended_stack[1]"),
            finding("origin_overclaim", "integrations[0]"),
            finding("coverage_all_addressed"),
            finding("completed_implementation_wording", "solution_overview"),
        ]
    }
    after = {
        "findings": [
            finding("origin_overclaim", "integrations[0]"),
            finding("coverage_all_addressed"),
            finding("passages_not_cited"),
        ]
    }
    out = revision_outcome(before, after)
    assert out is not None
    assert out["rules"]["origin_overclaim"] == {"before": 2, "resolved": 1, "still_present": 1}
    assert out["rules"]["coverage_all_addressed"] == {
        "before": 1,
        "resolved": 0,
        "still_present": 1,
    }
    assert out["rules"]["completed_implementation_wording"]["resolved"] == 1
    assert out["rules"]["passages_not_cited"] == {
        "before": 0,
        "resolved": 0,
        "still_present": 0,
        "introduced": 1,
    }
    assert (out["resolved"], out["still_present"], out["introduced"]) == (2, 2, 1)
    assert revision_outcome(None, after) is None
    assert revision_outcome(before, None) is None


def test_stored_outcome_when_everything_is_resolved(client: TestClient, engine: Engine) -> None:
    pid, ids, source = flagged_draft(client)
    serve(alias_payload(ids))
    new = revise(client, pid, source["id"]).json()
    outcome = new["revised_outcome"]
    assert outcome["still_present"] == 0 and outcome["introduced"] == 0
    assert outcome["resolved"] == len(source["lint_findings"]["findings"])
    assert outcome["rules"]["origin_overclaim"]["resolved"] == 1
    assert record(engine, new["id"]).revised_outcome == outcome
    assert client.get(arch_url(pid, f"/{new['id']}")).json()["revised_outcome"] == outcome


def test_stored_outcome_when_nothing_is_fixed_or_partly_fixed(client: TestClient) -> None:
    pid, ids, source = flagged_draft(client)
    serve(flagged_payload(ids))  # a model that changed nothing
    same = revise(client, pid, source["id"]).json()["revised_outcome"]
    assert same["resolved"] == 0
    assert same["still_present"] == len(source["lint_findings"]["findings"])

    partly = flagged_payload(ids)
    partly["recommended_stack"][1]["origin"] = "recommended_by_ai"
    serve(partly)
    out = revise(client, pid, source["id"]).json()["revised_outcome"]
    assert out["rules"]["origin_overclaim"]["resolved"] == 1
    assert out["rules"]["coverage_all_addressed"]["still_present"] == 1
    assert out["rules"]["completed_implementation_wording"]["still_present"] == 1


def test_outcome_leaves_source_and_ordinary_drafts_untouched(
    client: TestClient, engine: Engine
) -> None:
    pid, ids, source = flagged_draft(client)
    before = record(engine, source["id"])
    assert source["revised_outcome"] is None
    serve(alias_payload(ids))
    assert revise(client, pid, source["id"]).status_code == 201
    after = record(engine, source["id"])
    assert after.revised_outcome is None
    assert (after.content, after.lint_findings, after.status) == (
        before.content,
        before.lint_findings,
        before.status,
    )
