"""Text extraction from PDF and DOCX uploads.

Only the text layer is read. Embedded JavaScript, actions, attachments, macros and scripts are
never executed or extracted: pypdf only parses content streams and python-docx only reads the
WordprocessingML body. Encrypted PDFs are refused rather than decrypted.
"""

import zipfile
from dataclasses import dataclass
from io import BytesIO

import docx
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

from app.domain.document_errors import DocumentTooLargeError, InvalidDocumentError

DEFAULT_MAX_PAGES = 300
DEFAULT_MAX_EXTRACTED_CHARS = 1_000_000
# Zip-bomb guards for DOCX: declared sizes bound what zipfile will ever decompress.
MAX_DOCX_ENTRIES = 2_000
MAX_DOCX_UNCOMPRESSED_BYTES = 100 * 1_048_576
_ENCRYPTED_MESSAGE = (
    "PDF is encrypted or password-protected; remove the protection and upload it again"
)


@dataclass(frozen=True)
class ExtractionLimits:
    max_pages: int = DEFAULT_MAX_PAGES
    max_chars: int = DEFAULT_MAX_EXTRACTED_CHARS


def _normalize(text: str) -> str:
    # NUL cannot be stored in PostgreSQL text; line endings are unified for chunking.
    return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")


def _check_chars(total: int, limits: ExtractionLimits) -> None:
    if total > limits.max_chars:
        raise DocumentTooLargeError(
            f"Extracted text exceeds the {limits.max_chars} character limit"
        )


def extract_pdf_text(data: bytes, limits: ExtractionLimits) -> str:
    if b"%PDF-" not in data[:1024]:
        raise InvalidDocumentError("File is not a valid PDF")
    try:
        reader = PdfReader(BytesIO(data), strict=False)
        encrypted = reader.is_encrypted
    except Exception as exc:
        if b"/Encrypt" in data:
            raise InvalidDocumentError(_ENCRYPTED_MESSAGE) from exc
        raise InvalidDocumentError("PDF could not be read; the file may be damaged") from exc
    if encrypted:
        raise InvalidDocumentError(_ENCRYPTED_MESSAGE)

    try:
        page_count = len(reader.pages)
    except Exception as exc:
        raise InvalidDocumentError("PDF could not be read; the file may be damaged") from exc
    if page_count > limits.max_pages:
        raise DocumentTooLargeError(
            f"PDF has {page_count} pages; the limit is {limits.max_pages} pages"
        )

    parts: list[str] = []
    total = 0
    for index in range(page_count):
        try:
            page_text = _normalize(reader.pages[index].extract_text()).strip("\n")
        except Exception as exc:
            raise InvalidDocumentError(
                f"Text could not be extracted from PDF page {index + 1}"
            ) from exc
        if not page_text.strip():
            continue
        total += len(page_text) + (2 if parts else 0)
        parts.append(page_text)
        _check_chars(total, limits)

    text = "\n\n".join(parts)
    if not text.strip():
        raise InvalidDocumentError(
            "PDF contains no extractable text; it is probably scanned or image-only. "
            "Run OCR on it, or upload a text-based PDF"
        )
    return text


def _check_docx_archive(data: bytes) -> None:
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise InvalidDocumentError("File is not a valid DOCX document") from exc
    if len(entries) > MAX_DOCX_ENTRIES or (
        sum(e.file_size for e in entries) > MAX_DOCX_UNCOMPRESSED_BYTES
    ):
        raise DocumentTooLargeError("DOCX archive expands beyond the allowed size")
    if any(e.filename.lower().endswith("vbaproject.bin") for e in entries):
        raise InvalidDocumentError(
            "DOCX contains macros; macro-enabled documents are not supported"
        )


def _table_lines(table: Table) -> list[str]:
    lines: list[str] = []
    for row in table.rows:
        seen: set[int] = set()
        cells: list[str] = []
        for cell in row.cells:
            # Merged cells are repeated by python-docx; keep each underlying cell once.
            if id(cell._tc) in seen:
                continue
            seen.add(id(cell._tc))
            cells.append(cell.text)
        lines.append("\t".join(cells))
    return lines


def extract_docx_text(data: bytes, limits: ExtractionLimits) -> str:
    _check_docx_archive(data)
    try:
        document = docx.Document(BytesIO(data))
    except Exception as exc:
        raise InvalidDocumentError("DOCX could not be read; the file may be damaged") from exc

    lines: list[str] = []
    total = 0
    try:
        for block in document.iter_inner_content():
            new = [block.text] if isinstance(block, Paragraph) else _table_lines(block)
            for line in new:
                line = _normalize(line)
                total += len(line) + (1 if lines else 0)
                lines.append(line)
            _check_chars(total, limits)
    except DocumentTooLargeError:
        raise
    except Exception as exc:
        raise InvalidDocumentError("Text could not be extracted from the DOCX document") from exc

    text = "\n".join(lines).strip("\n")
    if not text.strip():
        raise InvalidDocumentError("DOCX contains no text")
    return text
