"""Reviewer feedback from a rejected patch, as a bounded, labelled context block.

Pure and deterministic. The block is advice about a previous attempt, never a requirement.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any

FEEDBACK_LABEL = "reviewer feedback (not a requirement)"
MAX_FEEDBACK_FILES = 20
MAX_FEEDBACK_NOTES = 10
MAX_FEEDBACK_CODES = 5
MAX_REASON_CHARS = 500
MAX_NOTE_CHARS = 300
MAX_PATH_CHARS = 200
MAX_CODE_TITLE_CHARS = 200
MAX_CODE_TEXT_CHARS = 600

# Same shape as the codes in requirement source quotes ("FR-004: ...").
_CODE = re.compile(r"\b[A-Z]{2,4}-\d{3}\b")


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def codes_in(text: str) -> list[str]:
    """Requirement codes mentioned in a text, in order, without repeats, at most five."""
    found: list[str] = []
    for code in _CODE.findall(text):
        if code not in found:
            found.append(code)
    return found[:MAX_FEEDBACK_CODES]


def build_feedback(
    *,
    version: int,
    reason: str,
    files: Sequence[tuple[str, str]],
    notes: Sequence[str],
    requirements: Mapping[str, tuple[str, str]],
) -> dict[str, Any]:
    """`requirements` maps a code to (title, description) for the project's confirmed ones."""
    mentioned = [
        {
            "code": code,
            "title": _cut(requirements[code][0], MAX_CODE_TITLE_CHARS),
            "description": _cut(requirements[code][1], MAX_CODE_TEXT_CHARS),
        }
        for code in codes_in(reason)
        if code in requirements
    ]
    return {
        FEEDBACK_LABEL: {
            "from_version": version,
            "reason": _cut(reason, MAX_REASON_CHARS),
            "rejected_files": [
                {"path": _cut(path, MAX_PATH_CHARS), "change": change}
                for path, change in files[:MAX_FEEDBACK_FILES]
            ],
            "rejected_files_total": len(files),
            "notes": [_cut(n, MAX_NOTE_CHARS) for n in notes[:MAX_FEEDBACK_NOTES]],
            "requirements_mentioned": mentioned,
        }
    }
