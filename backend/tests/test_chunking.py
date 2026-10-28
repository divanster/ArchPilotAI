import pytest

from app.domain.chunking import (
    MAX_CHUNKS_PER_DOCUMENT,
    ChunkSettings,
    InvalidChunkSettingsError,
    TooManyChunksError,
    chunk_text,
)

SMALL = ChunkSettings(size=100, overlap=20)


def check_exact(text: str, settings: ChunkSettings) -> list:  # type: ignore[type-arg]
    chunks = chunk_text(text, settings)
    for i, chunk in enumerate(chunks):
        assert chunk.index == i
        assert text[chunk.start : chunk.end] == chunk.text
        assert chunk.text.strip() == chunk.text and chunk.text
        assert len(chunk.text) <= settings.size
        assert chunk.end > chunk.start
    return chunks


def non_whitespace(text: str) -> set[int]:
    return {i for i, ch in enumerate(text) if not ch.isspace()}


def covered(chunks: list) -> set[int]:  # type: ignore[type-arg]
    return {i for c in chunks for i in range(c.start, c.end)}


@pytest.mark.parametrize(
    ("size", "overlap"), [(99, 0), (8001, 0), (100, -1), (100, 51), (1000, 600)]
)
def test_invalid_settings_are_rejected(size: int, overlap: int) -> None:
    with pytest.raises(InvalidChunkSettingsError):
        ChunkSettings(size=size, overlap=overlap)


def test_boundary_settings_are_accepted_and_versioned() -> None:
    assert ChunkSettings(100, 50).version == "para-v1:100:50"
    assert ChunkSettings(8000, 0).version == "para-v1:8000:0"


def test_empty_and_whitespace_text_yield_no_chunks() -> None:
    assert chunk_text("", SMALL) == []
    assert chunk_text(" \n\r\n\t ", SMALL) == []


def test_short_paragraphs_are_packed_with_original_whitespace() -> None:
    text = "alpha one\n\nbeta two\n\ngamma three"
    chunks = check_exact(text, ChunkSettings(size=100, overlap=0))
    assert [c.text for c in chunks] == [text]


def test_paragraph_boundaries_are_preferred() -> None:
    first, second = "a" * 60, "b" * 60
    text = f"{first}\n\n{second}"
    chunks = check_exact(text, SMALL)
    assert [c.text for c in chunks] == [first, second]
    assert (chunks[1].start, chunks[1].end) == (62, 122)


def test_crlf_and_cr_are_preserved_not_normalized() -> None:
    text = "first paragraph\r\n\r\nsecond paragraph\r\n \r\nthird\rlines\r\rend"
    chunks = check_exact(text, ChunkSettings(size=100, overlap=0))
    assert "\r\n" in chunks[0].text
    assert chunks[0].text == text[chunks[0].start : chunks[0].end]
    split = check_exact(text, SMALL)
    assert any("\r" in c.text for c in split)


def test_crlf_paragraph_split_offsets() -> None:
    a, b = "x" * 70, "y" * 70
    text = f"{a}\r\n\r\n{b}"
    chunks = check_exact(text, SMALL)
    assert [c.text for c in chunks] == [a, b]
    assert chunks[1].start == 74


def test_unicode_offsets_are_code_points_not_bytes_or_utf16() -> None:
    word = "na\u00efve caf\u00e9 \u6771\u4eac \U0001f600 emoji"
    text = (word + " ") * 12
    chunks = check_exact(text, SMALL)
    assert len(chunks) > 1
    assert any("\U0001f600" in c.text for c in chunks)
    # Astral characters occupy one code point, so offsets never drift from slicing.
    assert non_whitespace(text) <= covered(chunks)


def test_long_paragraph_splits_with_overlap_and_full_coverage() -> None:
    words = [f"w{i:03d}" for i in range(200)]
    text = " ".join(words)
    chunks = check_exact(text, SMALL)
    assert len(chunks) > 5
    for earlier, later in zip(chunks, chunks[1:], strict=False):
        assert later.start > earlier.start  # always progresses
        assert later.start < earlier.end  # overlap exists
        assert later.end > earlier.end
    assert non_whitespace(text) <= covered(chunks)
    # Overlap windows start on word boundaries, never mid-word.
    for chunk in chunks:
        assert chunk.text.split(" ")[0] in words


def test_zero_overlap_long_paragraph_does_not_repeat_text() -> None:
    text = " ".join(f"w{i:03d}" for i in range(100))
    chunks = check_exact(text, ChunkSettings(size=100, overlap=0))
    for earlier, later in zip(chunks, chunks[1:], strict=False):
        assert later.start >= earlier.end


def test_token_longer_than_window_is_hard_split_and_terminates() -> None:
    text = "z" * 1000
    chunks = check_exact(text, SMALL)
    assert len(chunks) >= 10
    assert chunks[0].start == 0 and chunks[-1].end == 1000
    assert all(b.start > a.start for a, b in zip(chunks, chunks[1:], strict=False))


def test_maximum_overlap_still_terminates() -> None:
    text = ("lorem ipsum dolor sit amet " * 400).strip()
    chunks = check_exact(text, ChunkSettings(size=100, overlap=50))
    assert chunks[-1].end == len(text)


def test_chunking_is_deterministic() -> None:
    text = "\n\n".join(f"sentence number {i}. " * 20 for i in range(15))
    assert chunk_text(text, SMALL) == chunk_text(text, SMALL)


def test_mixed_short_and_long_paragraphs() -> None:
    text = "intro line\n\n" + ("long " * 80).strip() + "\n\nclosing line"
    chunks = check_exact(text, SMALL)
    assert chunks[0].text == "intro line"
    assert chunks[-1].text == "closing line"


def test_leading_and_trailing_whitespace_is_excluded_but_offsets_exact() -> None:
    text = "  \n\n  padded paragraph  \n\n\n"
    chunks = check_exact(text, SMALL)
    assert [c.text for c in chunks] == ["padded paragraph"]
    assert text[chunks[0].start : chunks[0].end] == "padded paragraph"


def test_chunk_count_limit_is_enforced() -> None:
    text = "\n\n".join(["p" * 100] * (MAX_CHUNKS_PER_DOCUMENT + 1))
    with pytest.raises(TooManyChunksError):
        chunk_text(text, ChunkSettings(size=100, overlap=0))
