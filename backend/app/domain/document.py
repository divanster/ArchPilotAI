import hashlib
import re
from dataclasses import dataclass

from app.domain.document_errors import (
    DocumentError,
    DocumentTooLargeError,
    InvalidDocumentError,
    UnsupportedDocumentTypeError,
)
from app.domain.document_extraction import (
    ExtractionLimits,
    extract_docx_text,
    extract_pdf_text,
)

__all__ = [
    "DocumentError",
    "DocumentTooLargeError",
    "ExtractionLimits",
    "InvalidDocumentError",
    "ParsedDocument",
    "UnsupportedDocumentTypeError",
    "parse_upload",
    "sanitize_filename",
]

PDF_MEDIA_TYPE = "application/pdf"
DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
ALLOWED_EXTENSIONS: dict[str, str] = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".pdf": PDF_MEDIA_TYPE,
    ".docx": DOCX_MEDIA_TYPE,
}
_GENERIC_CLIENT_TYPES = frozenset({"", "application/octet-stream"})
_TEXT_CLIENT_TYPES = _GENERIC_CLIENT_TYPES | {"text/plain", "text/markdown", "text/x-markdown"}
# Client-declared types accepted per extension; the extension decides how content is parsed.
ACCEPTED_CLIENT_TYPES: dict[str, frozenset[str]] = {
    ".txt": _TEXT_CLIENT_TYPES,
    ".md": _TEXT_CLIENT_TYPES,
    ".pdf": _GENERIC_CLIENT_TYPES | {PDF_MEDIA_TYPE, "application/x-pdf"},
    ".docx": _GENERIC_CLIENT_TYPES | {DOCX_MEDIA_TYPE},
}
MAX_FILENAME_LENGTH = 255
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class ParsedDocument:
    filename: str
    media_type: str
    size_bytes: int
    content_sha256: str
    text: str


def sanitize_filename(raw: str | None) -> str:
    """Return a display-only filename; it is metadata and never used as a path."""
    name = re.split(r"[\\/]", raw or "")[-1]
    name = _CONTROL_CHARS.sub("", name).strip()
    if not name or len(name) > MAX_FILENAME_LENGTH:
        raise InvalidDocumentError("Filename is missing or too long")
    return name


def _decode_text(data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidDocumentError("File is not valid UTF-8 text") from exc
    text = text.removeprefix("\ufeff")
    if "\x00" in text:
        raise InvalidDocumentError("File contains NUL characters")
    return text


def parse_upload(
    filename: str | None,
    content_type: str | None,
    data: bytes,
    max_bytes: int,
    limits: ExtractionLimits | None = None,
) -> ParsedDocument:
    name = sanitize_filename(filename)
    lowered = name.lower()
    extension = next((ext for ext in ALLOWED_EXTENSIONS if lowered.endswith(ext)), None)
    if extension is None or len(name) == len(extension):
        raise UnsupportedDocumentTypeError("Only .txt, .md, .pdf and .docx files are supported")

    client_type = (content_type or "").split(";")[0].strip().lower()
    if client_type not in ACCEPTED_CLIENT_TYPES[extension]:
        raise UnsupportedDocumentTypeError(f"Unsupported content type: {client_type}")

    if len(data) > max_bytes:
        raise DocumentTooLargeError(f"File exceeds the {max_bytes} byte limit")

    limits = limits or ExtractionLimits()
    if extension == ".pdf":
        text = extract_pdf_text(data, limits)
    elif extension == ".docx":
        text = extract_docx_text(data, limits)
    else:
        text = _decode_text(data)
    if not text.strip():
        raise InvalidDocumentError("File is empty or whitespace only")

    return ParsedDocument(
        filename=name,
        media_type=ALLOWED_EXTENSIONS[extension],
        size_bytes=len(data),
        content_sha256=hashlib.sha256(data).hexdigest(),
        text=text,
    )
