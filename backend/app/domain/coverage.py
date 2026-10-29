"""Deterministic check that a draft defines what the acceptance criteria and requirements name.

No model is involved. Key terms are extracted from the text of each item by five simple rules:
  1. quoted text: "Purchase Order", `due_date`
  2. identifiers: CamelCase with two or more humps (PurchaseOrder) and snake_case (due_date)
  3. roles: a fixed vocabulary (admin, manager, customer, ...), singular or plural
  4. statuses: a fixed vocabulary (pending, approved, cancelled, ...), singular or plural
  5. entity names: capitalised words that are not the first word of a sentence; neighbouring ones
     form one name ("Purchase Order")
A term is defined when it appears in a file path or file content of the draft, compared in
lower case with everything but letters and digits removed (so "Purchase Order", PurchaseOrder and
purchase_order all match) and with simple plural and "cancelled/canceled" variants.
An item is covered when all its terms are defined, missing when some are not, and unchecked when
no term could be extracted (it is not counted as missing).
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

MAX_TERMS = 12
MAX_TEXT_CHARS = 200
MAX_MISSING_LINES = 10

ROLES = frozenset(
    {
        "admin",
        "administrator",
        "manager",
        "customer",
        "guest",
        "owner",
        "member",
        "editor",
        "viewer",
        "moderator",
        "employee",
        "supplier",
        "vendor",
        "approver",
        "reviewer",
        "operator",
        "superuser",
    }
)
STATUSES = frozenset(
    {
        "draft",
        "pending",
        "approved",
        "rejected",
        "cancelled",
        "canceled",
        "completed",
        "paid",
        "unpaid",
        "shipped",
        "delivered",
        "inactive",
        "archived",
        "expired",
        "refunded",
        "published",
        "suspended",
        "overdue",
        "submitted",
    }
)
# Capitalised words that are not entity names.
_STOP = frozenset(
    {
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
        "january",
        "february",
        "march",
        "april",
        "june",
        "july",
        "august",
        "september",
        "october",
        "november",
        "december",
        "the",
        "and",
        "but",
        "for",
        "not",
        "then",
        "when",
        "given",
    }
)

_QUOTED = re.compile(r'"([^"\n]{2,40})"|`([^`\n]{2,40})`|“([^”\n]{2,40})”')
_IDENT = re.compile(r"\b(?:[A-Z][a-z0-9]+){2,}\b|\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*|[.!?:;\n(]")
_CAPITALISED = re.compile(r"^[A-Z][a-z]{2,}$")
_GAP = re.compile(r"\s*\(needs clarification;.*\)\s*$", re.IGNORECASE | re.DOTALL)


def normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _entities(text: str) -> list[str]:
    out: list[str] = []
    start = True
    run: list[str] = []
    last_end = -1
    for match in _TOKEN.finditer(text):
        token = match.group()
        if not token[0].isalpha():
            start = True
            run = []
            continue
        entity = not start and bool(_CAPITALISED.match(token)) and token.lower() not in _STOP
        start = False
        if entity:
            if run and text[last_end : match.start()] == " ":
                run.append(token)
                out[-1] = " ".join(run)
            else:
                run = [token]
                out.append(token)
            last_end = match.end()
        else:
            run = []
    return out


def _vocabulary(text: str, vocabulary: frozenset[str]) -> list[str]:
    found: list[str] = []
    for word in re.findall(r"[A-Za-z]+", text.lower()):
        base = word if word in vocabulary else word[:-1] if word[:-1] in vocabulary else ""
        if base:
            found.append(base)
    return found


def extract_terms(text: str) -> list[str]:
    """Key terms of one text, without repeats (by normalised form), at most MAX_TERMS."""
    text = _GAP.sub("", text)  # a plan's "(needs clarification; gap: ...)" is not part of the item
    candidates: list[str] = []
    for match in _QUOTED.finditer(text):
        candidates.append(next(g for g in match.groups() if g))
    candidates += _IDENT.findall(text)
    candidates += _vocabulary(text, ROLES)
    candidates += _vocabulary(text, STATUSES)
    candidates += _entities(text)
    seen: set[str] = set()
    terms: list[str] = []
    for term in candidates:
        key = normalise(term)
        if len(key) >= 3 and key not in seen:
            seen.add(key)
            terms.append(term.strip())
    return terms[:MAX_TERMS]


def _variants(term: str) -> set[str]:
    key = normalise(term)
    out = {key}
    if key.endswith("ies") and len(key) > 4:
        out.add(key[:-3] + "y")
    elif key.endswith("es") and len(key) > 4:
        out.add(key[:-2])
    if key.endswith("s") and len(key) > 3:
        out.add(key[:-1])
    for variant in list(out):
        if "cancelled" in variant:
            out.add(variant.replace("cancelled", "canceled"))
        if "canceled" in variant:
            out.add(variant.replace("canceled", "cancelled"))
    return out


@dataclass(frozen=True)
class CoverageItem:
    kind: str  # "criterion" or "requirement"
    ref: str  # "criterion 2" or a requirement code
    text: str


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def check_coverage(items: Sequence[CoverageItem], files: Mapping[str, str]) -> dict[str, Any]:
    """Covered / missing / unchecked per item, against the text of `files` (path -> content)."""
    corpus = normalise("\n".join(f"{path}\n{content}" for path, content in files.items()))
    rows: list[dict[str, Any]] = []
    for item in items:
        covered: list[str] = []
        missing: list[str] = []
        for term in extract_terms(item.text):
            (covered if any(v in corpus for v in _variants(term)) else missing).append(term)
        status = "missing" if missing else "covered" if covered else "unchecked"
        rows.append(
            {
                "kind": item.kind,
                "ref": item.ref,
                "text": _cut(item.text, MAX_TEXT_CHARS),
                "status": status,
                "covered": covered,
                "missing": missing,
            }
        )
    count = {
        s: sum(1 for r in rows if r["status"] == s) for s in ("covered", "missing", "unchecked")
    }
    return {"items": rows, **count}


def missing_lines(report: Mapping[str, Any]) -> list[str]:
    """One readable line per item that has missing terms."""
    return [
        _cut(
            f"{row['kind'].capitalize()} {row['ref']}: the draft does not define "
            f"{', '.join(row['missing'])} ({row['text']})",
            300,
        )
        for row in report["items"]
        if row["missing"]
    ][:MAX_MISSING_LINES]


def missing_findings(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The missing items as actionable findings for the model."""
    return [
        {"tool": "coverage", "file": "", "line": 0, "code": "missing-term", "message": line}
        for line in missing_lines(report)
    ]
