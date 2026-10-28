"""Building the model-facing view of a stored draft for "revise with findings".

A stored draft holds requirement UUIDs and passage chunk ids; the model only ever sees the
request-scoped aliases (R001, P001, ...). This converts one way, deterministically, and never
changes the stored draft. Citations of passages that are not offered in the current request are
left out of the view, since the model cannot cite what it was not given.
"""

import copy
from collections.abc import Mapping
from typing import Any

from app.domain.architecture import (
    _DECISION_GROUPS,
    _PASSAGE_SECTIONS,
    _REQUIREMENT_SECTIONS,
    RESERVED_KEYS,
)


def draft_for_revision(
    title: str,
    summary: str,
    content: Mapping[str, Any],
    requirement_alias: Mapping[str, str],
    passage_alias: Mapping[str, str],
) -> dict[str, Any]:
    """`requirement_alias` maps requirement UUID strings, `passage_alias` chunk-id strings."""
    draft: dict[str, Any] = {
        k: copy.deepcopy(v) for k, v in content.items() if k not in RESERVED_KEYS
    }
    draft["title"] = title
    draft["summary"] = summary

    def swap(item: dict[str, Any], key: str, table: Mapping[str, str]) -> None:
        values = item.get(key)
        if isinstance(values, list):
            item[key] = [table[str(v)] for v in values if str(v) in table]

    for section in (*_DECISION_GROUPS, *_REQUIREMENT_SECTIONS):
        field = "supporting_requirement_ids" if section in _DECISION_GROUPS else "requirement_ids"
        for item in draft.get(section) or []:
            swap(item, field, requirement_alias)
    for section in (*_DECISION_GROUPS, *_PASSAGE_SECTIONS):
        for item in draft.get(section) or []:
            swap(item, "source_passage_ids", passage_alias)
    for entry in draft.get("coverage_matrix") or []:
        entry["requirement_id"] = requirement_alias.get(
            str(entry["requirement_id"]), entry["requirement_id"]
        )
    return draft


def findings_for_revision(report: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Stored findings as the model sees them: fixed rule codes, schema paths, listed terms."""
    out = []
    for finding in (report or {}).get("findings") or []:
        out.append(
            {
                "rule": finding["rule"],
                "path": finding.get("path"),
                "message": finding["message"],
                "terms": list(finding.get("terms") or []),
            }
        )
    return out
