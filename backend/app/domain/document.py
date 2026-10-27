import hashlib
import re
from dataclasses import dataclass

ALLOWED_EXTENSIONS: dict[str, str] = {".txt": "text/plain", ".md": "text/markdown"}
ACCEPTED_CLIENT_TYPES = {
    "",
    "application/octet-stream",
    "text/plain",
    "text/markdown",
    "text/x-markdown",
}
MAX_FILENAME_LENGTH = 255
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class DocumentError(Exception):
    """Base class for document validation errors."""


class UnsupportedDocumentTypeError(DocumentError):
    pass


class DocumentTooLargeError(DocumentError):
    pass


class InvalidDocumentError(DocumentError):
    pass


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


def parse_upload(
    filename: str | None, content_type: str | None, data: bytes, max_bytes: int
) -> ParsedDocument:
    name = sanitize_filename(filename)
    lowered = name.lower()
    extension = next((ext for ext in ALLOWED_EXTENSIONS if lowered.endswith(ext)), None)
    if extension is None or len(name) == len(extension):
        raise UnsupportedDocumentTypeError("Only .txt and .md files are supported")

    client_type = (content_type or "").split(";")[0].strip().lower()
    if client_type not in ACCEPTED_CLIENT_TYPES:
        raise UnsupportedDocumentTypeError(f"Unsupported content type: {client_type}")

    if len(data) > max_bytes:
        raise DocumentTooLargeError(f"File exceeds the {max_bytes} byte limit")

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidDocumentError("File is not valid UTF-8 text") from exc
    text = text.removeprefix("\ufeff")
    if "\x00" in text:
        raise InvalidDocumentError("File contains NUL characters")
    if not text.strip():
        raise InvalidDocumentError("File is empty or whitespace only")

    return ParsedDocument(
        filename=name,
        media_type=ALLOWED_EXTENSIONS[extension],
        size_bytes=len(data),
        content_sha256=hashlib.sha256(data).hexdigest(),
        text=text,
    )
