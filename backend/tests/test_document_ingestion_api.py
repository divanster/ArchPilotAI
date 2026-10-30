"""End-to-end ingestion of PDF and DOCX uploads: storage, preview text, requirements, BM25."""

import uuid

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.api.deps import get_extraction_limits, get_llm_provider
from app.domain.document import DOCX_MEDIA_TYPE, PDF_MEDIA_TYPE, ExtractionLimits
from app.llm.fake_provider import FakeLLMProvider
from app.main import app
from app.models import DocumentChunk
from tests.document_samples import make_docx, make_encrypted_pdf, make_pdf, make_textless_pdf

Q1 = "The system shall list projects (active and archived)."
Q2 = "The system must respond within 2 seconds."
Q3 = "The zephyrquartz exporter shall produce CSV files."
PDF_PAGES = [
    ["Synthetic specification.", f"- {Q1}", "Background notes."],
    [f"- {Q2}", f"- {Q3}"],
]


def _project(client: TestClient) -> str:
    response = client.post("/api/projects", json={"name": "Ingestion"})
    assert response.status_code == 201
    return str(response.json()["id"])


def _upload(client: TestClient, pid: str, name: str, data: bytes, content_type: str) -> Response:
    response: Response = client.post(
        f"/api/projects/{pid}/documents", files={"file": (name, data, content_type)}
    )
    return response


def _samples() -> list[tuple[str, bytes, str]]:
    return [
        ("Spec v1 (final).pdf", make_pdf(PDF_PAGES), PDF_MEDIA_TYPE),
        (
            "Spec v1 (final).docx",
            make_docx(
                ["Synthetic specification.", f"- {Q1}"],
                table=[["ID", "Text"], ["NFR-001", Q2]],
                after=[f"- {Q3}"],
            ),
            DOCX_MEDIA_TYPE,
        ),
    ]


@pytest.mark.parametrize(("name", "data", "media_type"), _samples(), ids=["pdf", "docx"])
def test_upload_keeps_filename_and_stores_extracted_text(
    client: TestClient, name: str, data: bytes, media_type: str
) -> None:
    pid = _project(client)
    created = _upload(client, pid, name, data, media_type)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["filename"] == name
    assert body["media_type"] == media_type
    assert body["size_bytes"] == len(data)

    detail = client.get(f"/api/projects/{pid}/documents/{body['id']}").json()
    for quote in (Q1, Q2, Q3):
        assert detail["text"].count(quote) == 1
    assert "%PDF" not in detail["text"] and "PK" not in detail["text"][:2]

    duplicate = _upload(client, pid, f"copy-{name}", data, "application/octet-stream")
    assert duplicate.status_code == 409


@pytest.mark.parametrize(("name", "data", "media_type"), _samples(), ids=["pdf", "docx"])
def test_requirement_quotes_match_extracted_text_exactly(
    client: TestClient, name: str, data: bytes, media_type: str
) -> None:
    app.dependency_overrides[get_llm_provider] = lambda: FakeLLMProvider()
    pid = _project(client)
    did = _upload(client, pid, name, data, media_type).json()["id"]
    text = client.get(f"/api/projects/{pid}/documents/{did}").json()["text"]

    response = client.post(f"/api/projects/{pid}/documents/{did}/extract-requirements")
    assert response.status_code == 201, response.text
    requirements = response.json()["requirements"]
    quotes = {r["source_quote"] for r in requirements}
    assert {Q1, Q3} <= quotes
    assert any(Q2 in q for q in quotes)
    for r in requirements:
        assert text[r["source_start"] : r["source_end"]] == r["source_quote"]


def test_pdf_chunks_and_bm25_search_use_extracted_text(client: TestClient, engine: Engine) -> None:
    pid = _project(client)
    name, data, media_type = _samples()[0]
    did = _upload(client, pid, name, data, media_type).json()["id"]
    text = client.get(f"/api/projects/{pid}/documents/{did}").json()["text"]

    indexed = client.post(f"/api/projects/{pid}/documents/{did}/index", json={})
    assert indexed.status_code == 200, indexed.text
    with Session(engine) as session:
        chunks = session.scalars(
            select(DocumentChunk).where(DocumentChunk.document_id == uuid.UUID(did))
        ).all()
    assert chunks
    for chunk in chunks:
        assert text[chunk.source_start : chunk.source_end] == chunk.text

    hits = client.post(f"/api/projects/{pid}/search", json={"query": "zephyrquartz"}).json()
    assert [h["filename"] for h in hits["results"]] == [name]
    hit = hits["results"][0]
    assert text[hit["source_start"] : hit["source_end"]] == hit["text"]
    assert Q3 in hit["text"]


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        (make_textless_pdf(), "probably scanned or image-only"),
        (make_encrypted_pdf([Q1]), "encrypted or password-protected"),
        (make_encrypted_pdf([Q1], user_password=""), "encrypted or password-protected"),
        (b"%PDF-1.7\nnot really a pdf", "could not be read"),
    ],
    ids=["scanned", "encrypted", "owner-only-encryption", "damaged"],
)
def test_unreadable_pdfs_are_422_with_a_clear_reason(
    client: TestClient, data: bytes, fragment: str
) -> None:
    pid = _project(client)
    response = _upload(client, pid, "input.pdf", data, "application/pdf")
    assert response.status_code == 422
    assert fragment in response.json()["detail"]
    assert client.get(f"/api/projects/{pid}/documents").json() == []


def test_empty_docx_is_422(client: TestClient) -> None:
    pid = _project(client)
    response = _upload(client, pid, "empty.docx", make_docx([]), DOCX_MEDIA_TYPE)
    assert response.status_code == 422
    assert "no text" in response.json()["detail"]


def test_page_and_character_limits_come_from_settings(client: TestClient) -> None:
    pid = _project(client)
    app.dependency_overrides[get_extraction_limits] = lambda: ExtractionLimits(
        max_pages=1, max_chars=40
    )
    pages = _upload(client, pid, "long.pdf", make_pdf(PDF_PAGES), PDF_MEDIA_TYPE)
    assert pages.status_code == 413
    assert "2 pages; the limit is 1 pages" in pages.json()["detail"]

    chars = _upload(client, pid, "wide.docx", make_docx([Q1]), DOCX_MEDIA_TYPE)
    assert chars.status_code == 413
    assert "40 character limit" in chars.json()["detail"]

    fits = _upload(client, pid, "short.pdf", make_pdf([["Short page."]]), PDF_MEDIA_TYPE)
    assert fits.status_code == 201
    assert len(client.get(f"/api/projects/{pid}/documents").json()) == 1
