"""Deterministic, offset-exact document chunking.

Every chunk is an unmodified slice of the source: ``text[chunk.start:chunk.end] == chunk.text``.
Offsets are Python code points. Text is never normalized (CRLF, Unicode and spacing survive).

Algorithm (``para-v1``):
1. Paragraphs are runs of text separated by one or more blank lines (``\\n``, ``\\r\\n`` or ``\\r``,
   optionally with spaces or tabs on the blank line). Each paragraph is trimmed of whitespace.
2. Consecutive paragraphs are packed into one chunk while the chunk, including the original
   whitespace between them, stays within ``size`` characters.
3. A paragraph longer than ``size`` is split into windows of at most ``size`` characters. Each
   window ends at the last whitespace in its second half (a hard cut if there is none). The next
   window starts ``overlap`` characters earlier, moved forward to a word boundary. Overlap only
   exists between windows of one long paragraph, never between packed paragraphs.

Limitation: splits fall between code points, so a combining mark may land in the next window.
"""

import re
from dataclasses import dataclass

CHUNKER_ID = "para-v1"
MIN_CHUNK_SIZE = 100
MAX_CHUNK_SIZE = 8000
MAX_CHUNKS_PER_DOCUMENT = 5000

_PARAGRAPH_BREAK = re.compile(r"(?:\r\n|\r|\n)(?:[ \t]*(?:\r\n|\r|\n))+")


class ChunkingError(Exception):
    pass


class InvalidChunkSettingsError(ChunkingError, ValueError):
    pass


class TooManyChunksError(ChunkingError):
    pass


@dataclass(frozen=True)
class ChunkSettings:
    size: int = 1000
    overlap: int = 150

    def __post_init__(self) -> None:
        if not MIN_CHUNK_SIZE <= self.size <= MAX_CHUNK_SIZE:
            raise InvalidChunkSettingsError(
                f"chunk size must be between {MIN_CHUNK_SIZE} and {MAX_CHUNK_SIZE}"
            )
        # Overlap of at most half the window guarantees every window advances.
        if not 0 <= self.overlap <= self.size // 2:
            raise InvalidChunkSettingsError("chunk overlap must be between 0 and size // 2")

    @property
    def version(self) -> str:
        return f"{CHUNKER_ID}:{self.size}:{self.overlap}"


@dataclass(frozen=True)
class TextChunk:
    index: int
    start: int
    end: int
    text: str


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _paragraphs(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    position = 0
    for match in _PARAGRAPH_BREAK.finditer(text):
        spans.append((position, match.start()))
        position = match.end()
    spans.append((position, len(text)))
    trimmed = (_trim(text, s, e) for s, e in spans)
    return [(s, e) for s, e in trimmed if s < e]


def _split_long(text: str, start: int, end: int, settings: ChunkSettings) -> list[tuple[int, int]]:
    size, overlap = settings.size, settings.overlap
    windows: list[tuple[int, int]] = []
    position = start
    while True:
        limit = position + size
        if limit >= end:
            windows.append(_trim(text, position, end))
            return [w for w in windows if w[0] < w[1]]
        cut = limit
        floor = position + size // 2
        for candidate in range(limit, floor, -1):
            if text[candidate].isspace():
                cut = candidate
                break
        windows.append(_trim(text, position, cut))
        following = cut - overlap
        if overlap and following > position and not text[following].isspace():
            boundary = next((i for i in range(following, cut) if text[i].isspace()), None)
            if boundary is not None:
                following = boundary
        # cut - position > size // 2 >= overlap, so this strictly advances.
        position = min(max(following, position + 1), cut)


def chunk_text(text: str, settings: ChunkSettings) -> list[TextChunk]:
    spans: list[tuple[int, int]] = []
    group: tuple[int, int] | None = None
    for start, end in _paragraphs(text):
        if end - start > settings.size:
            if group is not None:
                spans.append(group)
                group = None
            spans.extend(_split_long(text, start, end, settings))
        elif group is None:
            group = (start, end)
        elif end - group[0] <= settings.size:
            group = (group[0], end)
        else:
            spans.append(group)
            group = (start, end)
        if len(spans) > MAX_CHUNKS_PER_DOCUMENT:
            raise TooManyChunksError(f"Document needs more than {MAX_CHUNKS_PER_DOCUMENT} chunks")
    if group is not None:
        spans.append(group)
    if len(spans) > MAX_CHUNKS_PER_DOCUMENT:
        raise TooManyChunksError(f"Document needs more than {MAX_CHUNKS_PER_DOCUMENT} chunks")
    return [TextChunk(i, s, e, text[s:e]) for i, (s, e) in enumerate(spans)]
