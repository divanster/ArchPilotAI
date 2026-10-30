import hashlib
import importlib.util
import zipfile
from io import BytesIO

import pytest

from app.domain import document_extraction
from app.domain.document import (
    DOCX_MEDIA_TYPE,
    PDF_MEDIA_TYPE,
    DocumentTooLargeError,
    ExtractionLimits,
    InvalidDocumentError,
    UnsupportedDocumentTypeError,
    parse_upload,
)
from app.domain.document_extraction import extract_docx_text, extract_pdf_text
from tests.document_samples import (
    make_docx,
    make_encrypted_pdf,
    make_pdf,
    make_textless_pdf,
)

LIMIT = 1_048_576
LIMITS = ExtractionLimits(max_pages=5, max_chars=10_000)


def test_pdf_text_is_extracted_page_by_page() -> None:
    data = make_pdf(
        [
            ["Synthetic spec (v1).", "- The system shall list projects."],
            ["- The system must respond within 2 seconds."],
        ]
    )
    parsed = parse_upload("Spec Final.PDF", "application/pdf", data, LIMIT, LIMITS)
    assert parsed.filename == "Spec Final.PDF"
    assert parsed.media_type == PDF_MEDIA_TYPE
    assert parsed.size_bytes == len(data)
    assert parsed.content_sha256 == hashlib.sha256(data).hexdigest()
    assert parsed.text == (
        "Synthetic spec (v1).\n- The system shall list projects.\n\n"
        "- The system must respond within 2 seconds."
    )


@pytest.mark.parametrize("client_type", [None, "", "application/octet-stream", "application/x-pdf"])
def test_pdf_generic_client_types_are_accepted(client_type: str | None) -> None:
    assert parse_upload("a.pdf", client_type, make_pdf([["Hello"]]), LIMIT).text == "Hello"


@pytest.mark.parametrize(
    ("name", "client_type"),
    [("a.pdf", "text/plain"), ("a.pdf", DOCX_MEDIA_TYPE), ("a.docx", "application/pdf")],
)
def test_mismatched_client_type_is_rejected(name: str, client_type: str) -> None:
    with pytest.raises(UnsupportedDocumentTypeError):
        parse_upload(name, client_type, make_pdf([["Hello"]]), LIMIT)


@pytest.mark.parametrize(
    "data", [make_textless_pdf(), make_textless_pdf(pages=3)], ids=["one-page", "three-pages"]
)
def test_scanned_pdf_without_text_layer_is_rejected(data: bytes) -> None:
    with pytest.raises(InvalidDocumentError, match="no extractable text.*scanned"):
        parse_upload("scan.pdf", "application/pdf", data, LIMIT, LIMITS)


@pytest.mark.parametrize(
    "algorithm",
    [
        None,
        pytest.param(
            "AES-256",
            marks=pytest.mark.skipif(
                importlib.util.find_spec("cryptography") is None, reason="needs cryptography"
            ),
        ),
    ],
)
@pytest.mark.parametrize("user_password", ["secret", ""])
def test_encrypted_pdf_is_rejected(algorithm: str | None, user_password: str) -> None:
    # Even a PDF that opens without a password (owner-only protection) is refused.
    data = make_encrypted_pdf(["The system shall list projects."], user_password, algorithm)
    with pytest.raises(InvalidDocumentError, match="encrypted or password-protected"):
        parse_upload("locked.pdf", "application/pdf", data, LIMIT, LIMITS)


@pytest.mark.parametrize(
    "data", [b"", b"plain text, not a pdf", b"%PDF-1.4\n%garbage without objects\n"]
)
def test_invalid_pdf_bytes_are_rejected(data: bytes) -> None:
    with pytest.raises(InvalidDocumentError):
        parse_upload("bad.pdf", "application/pdf", data, LIMIT, LIMITS)


def test_embedded_javascript_is_never_extracted_or_run() -> None:
    script = b"app.alert('INJECTED-JS-MARKER')"
    data = make_pdf(
        [["Visible text."]],
        catalog_extra=b"/OpenAction << /S /JavaScript /JS (" + script + b") >> ",
    )
    text = extract_pdf_text(data, LIMITS)
    assert text == "Visible text."
    assert "INJECTED-JS-MARKER" not in text


def test_pdf_page_limit_boundary() -> None:
    limits = ExtractionLimits(max_pages=2, max_chars=10_000)
    assert extract_pdf_text(make_pdf([["one"], ["two"]]), limits) == "one\n\ntwo"
    with pytest.raises(DocumentTooLargeError, match="3 pages; the limit is 2 pages"):
        extract_pdf_text(make_pdf([["one"], ["two"], ["three"]]), limits)


def test_pdf_character_limit_boundary() -> None:
    line = "x" * 50
    assert extract_pdf_text(make_pdf([[line]]), ExtractionLimits(max_chars=50)) == line
    pages = make_pdf([[line], [line]])
    assert len(extract_pdf_text(pages, ExtractionLimits(max_chars=102))) == 102
    with pytest.raises(DocumentTooLargeError, match="101 character limit"):
        extract_pdf_text(pages, ExtractionLimits(max_chars=101))


def test_raw_upload_byte_limit_still_applies_to_pdf() -> None:
    data = make_pdf([["Hello"]])
    with pytest.raises(DocumentTooLargeError, match="byte limit"):
        parse_upload("a.pdf", "application/pdf", data, len(data) - 1, LIMITS)


def test_docx_paragraphs_and_tables_are_extracted_in_order() -> None:
    data = make_docx(
        ["Synthetic spec.", "- The system shall list projects."],
        table=[["ID", "Requirement"], ["NFR-001", "The system must respond within 2 seconds."]],
        after=["Closing note."],
    )
    parsed = parse_upload("Spec.docx", DOCX_MEDIA_TYPE, data, LIMIT, LIMITS)
    assert parsed.media_type == DOCX_MEDIA_TYPE
    assert parsed.size_bytes == len(data)
    assert parsed.text == (
        "Synthetic spec.\n- The system shall list projects.\n"
        "ID\tRequirement\nNFR-001\tThe system must respond within 2 seconds.\n"
        "Closing note."
    )


def test_empty_docx_is_rejected() -> None:
    with pytest.raises(InvalidDocumentError, match="DOCX contains no text"):
        parse_upload("empty.docx", None, make_docx(["", "   "]), LIMIT, LIMITS)


@pytest.mark.parametrize(
    "data", [b"", b"not a zip", make_pdf([["Hello"]])], ids=["empty", "text", "pdf"]
)
def test_invalid_docx_bytes_are_rejected(data: bytes) -> None:
    with pytest.raises(InvalidDocumentError):
        parse_upload("bad.docx", None, data, LIMIT, LIMITS)


def _with_extra_entry(data: bytes, name: str, payload: bytes) -> bytes:
    out = BytesIO()
    with (
        zipfile.ZipFile(BytesIO(data)) as source,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for item in source.infolist():
            target.writestr(item, source.read(item.filename))
        target.writestr(name, payload)
    return out.getvalue()


def test_docx_with_macros_is_rejected() -> None:
    data = _with_extra_entry(make_docx(["Hello"]), "word/vbaProject.bin", b"\x00macro")
    with pytest.raises(InvalidDocumentError, match="macros"):
        parse_upload("macro.docx", None, data, LIMIT, LIMITS)


def test_docx_expanding_beyond_limit_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(document_extraction, "MAX_DOCX_UNCOMPRESSED_BYTES", 100_000)
    data = _with_extra_entry(make_docx(["Hello"]), "word/media/bomb.bin", b"\x00" * 200_000)
    assert len(data) < 100_000
    with pytest.raises(DocumentTooLargeError, match="expands beyond"):
        parse_upload("bomb.docx", None, data, LIMIT, LIMITS)


def test_docx_character_limit_boundary() -> None:
    data = make_docx(["a" * 40, "b" * 40])
    assert extract_docx_text(data, ExtractionLimits(max_chars=81)) == f"{'a' * 40}\n{'b' * 40}"
    with pytest.raises(DocumentTooLargeError, match="80 character limit"):
        extract_docx_text(data, ExtractionLimits(max_chars=80))
