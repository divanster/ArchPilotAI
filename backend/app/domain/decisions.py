"""Human technology decisions: areas, task-to-area mapping and the labelled context block.

Pure and deterministic. A task is mapped to an area by keywords in its title, then by its kind;
a task that maps to no area can never be released by a decision.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Any, Protocol

from app.domain.plan import AI_RECOMMENDATION_FLAG

AREAS = ("backend", "frontend", "database", "hosting", "auth", "testing", "style")
BLOCK_LABEL = "Human technology decisions (binding)"

# Checked in this order; the first area with a matching word wins.
_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "auth",
        ("auth", "authentication", "authorization", "login", "oauth", "sso", "jwt", "identity"),
    ),
    ("testing", ("test", "tests", "testing", "qa", "pytest", "vitest")),
    ("database", ("database", "postgres", "postgresql", "mysql", "sqlite", "sql", "storage")),
    (
        "hosting",
        ("hosting", "deploy", "deployment", "cloud", "azure", "aws", "docker", "kubernetes"),
    ),
    ("frontend", ("frontend", "front-end", "ui", "react", "vue", "angular", "web")),
    ("style", ("style", "styling", "css", "tailwind", "theme", "design")),
    ("backend", ("backend", "back-end", "api", "server", "python", "java", "fastapi", "service")),
)
_KIND_AREA = {"security": "auth", "test": "testing", "data": "database"}


class DecisionLike(Protocol):
    area: str
    choice: str
    reason: str
    decided_by: str


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9][a-z0-9-]*", text.lower()))


def area_for_text(text: str) -> str | None:
    found = _words(text)
    for area, words in _KEYWORDS:
        if found & set(words):
            return area
    return None


def area_for_task(kind: str, title: str) -> str | None:
    return area_for_text(title) or _KIND_AREA.get(kind)


def stack_suggestions(stack: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, str]]:
    """First recommended-stack item per area: {area: {choice, layer}}."""
    out: dict[str, dict[str, str]] = {}
    for item in stack:
        layer, technology = str(item.get("layer", "")), str(item.get("technology", ""))
        area = area_for_text(layer) or area_for_text(technology)
        if area and area not in out and technology:
            out[area] = {"choice": technology, "layer": layer}
    return out


def decisions_block(decisions: Iterable[DecisionLike]) -> dict[str, Any]:
    """The labelled block injected into every generation and agent run (current rows only)."""
    return {
        BLOCK_LABEL: [
            {
                "area": d.area,
                "choice": d.choice,
                "reason": d.reason,
                "decided_by": d.decided_by,
            }
            for d in sorted(decisions, key=lambda d: AREAS.index(d.area))
        ]
    }


def task_flags(
    flags: list[str], decision: DecisionLike | None, recommended: str | None
) -> list[str]:
    """Replace the AI flag by the confirmation, and show a deviation from the AI recommendation."""
    if decision is None or AI_RECOMMENDATION_FLAG not in flags:
        return list(flags)
    out = [f for f in flags if f != AI_RECOMMENDATION_FLAG]
    out.append(f"Confirmed by {decision.decided_by}: {decision.choice}")
    if recommended and recommended.strip().lower() != decision.choice.strip().lower():
        out.append(
            f"Deviation: AI recommended {recommended}, the human decision is {decision.choice}"
        )
    return out
