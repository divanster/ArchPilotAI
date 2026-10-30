import pytest

from app.domain.document import (
    DocumentTooLargeError,
    InvalidDocumentError,
    UnsupportedDocumentTypeError,
    parse_upload,
    sanitize_filename,
)

LIMIT = 1024


def test_parses_text_and_hashes_raw_bytes() -> None:
    parsed = parse_upload("notes.txt", "text/plain", b"hello\n", LIMIT)
    assert parsed.filename == "notes.txt"
    assert parsed.media_type == "text/plain"
    assert parsed.size_bytes == 6
    assert parsed.text == "hello\n"
    assert (
        parsed.content_sha256 == "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"
    )


def test_markdown_media_type_is_derived_from_extension() -> None:
    parsed = parse_upload("README.MD", "application/octet-stream", b"# Title", LIMIT)
    assert parsed.media_type == "text/markdown"


def test_bom_is_stripped_from_text_but_counted_in_size() -> None:
    parsed = parse_upload("a.txt", None, b"\xef\xbb\xbfhi", LIMIT)
    assert parsed.text == "hi"
    assert parsed.size_bytes == 5


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd.txt", "passwd.txt"),
        ("..\\..\\evil.md", "evil.md"),
        ("C:\\temp\\a.txt", "a.txt"),
        ("na\x00me\n.txt", "name.txt"),
        ("  spaced.txt  ", "spaced.txt"),
    ],
)
def test_filename_is_sanitized_metadata(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "dir/", "x" * 256])
def test_invalid_filenames(raw: str | None) -> None:
    with pytest.raises(InvalidDocumentError):
        sanitize_filename(raw)


@pytest.mark.parametrize(
    "name", ["a.doc", "a.docm", "a.pdf.exe", "a", ".txt", ".pdf", "a.txt.exe", "a.md.png"]
)
def test_unsupported_extensions(name: str) -> None:
    with pytest.raises(UnsupportedDocumentTypeError):
        parse_upload(name, "text/plain", b"x", LIMIT)


def test_unsupported_client_content_type() -> None:
    with pytest.raises(UnsupportedDocumentTypeError):
        parse_upload("a.txt", "application/pdf", b"x", LIMIT)


def test_content_type_parameters_are_ignored() -> None:
    assert parse_upload("a.txt", "text/plain; charset=utf-8", b"x", LIMIT).text == "x"


def test_size_limit_boundary() -> None:
    assert parse_upload("a.txt", None, b"a" * LIMIT, LIMIT).size_bytes == LIMIT
    with pytest.raises(DocumentTooLargeError):
        parse_upload("a.txt", None, b"a" * (LIMIT + 1), LIMIT)


@pytest.mark.parametrize("data", [b"", b"   \n\t ", b"\xef\xbb\xbf  ", b"\xff\xfe", b"a\x00b"])
def test_invalid_content(data: bytes) -> None:
    with pytest.raises(InvalidDocumentError):
        parse_upload("a.txt", None, data, LIMIT)
