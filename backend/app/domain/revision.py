"""Building the model-facing view of a stored draft for "revise with findings".

A stored draft holds requirement UUIDs and passage chunk ids; the model only ever sees the
request-scoped aliases (R001, P001, ...). This converts one way, deterministically, and never
changes the stored draft. Citations of passages that are not offered in the current request are
left out of the view, since the model cannot cite what it was not given.
"""

import copy
from collections import Counter
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
    """Stored findings as the model sees them: fixed rule codes, schema paths, listed terms,
    and the targeted instruction for that finding."""
    out = []
    for finding in (report or {}).get("findings") or []:
        item = {
            "rule": finding["rule"],
            "path": finding.get("path"),
            "message": finding["message"],
            "terms": list(finding.get("terms") or []),
        }
        item["instruction"] = finding_instruction(item)
        out.append(item)
    return out


def finding_instruction(finding: Mapping[str, Any]) -> str:
    """The exact edit asked for one finding. Built from fixed text plus the finding's own path
    and listed terms; never from model output."""
    rule = finding["rule"]
    path = finding.get("path")
    products = ", ".join(finding.get("terms") or []) or "the named product"
    if rule in ("origin_overclaim", "preference_overclaim"):
        claim = "required" if rule == "origin_overclaim" else "preferred"
        return (
            f"At {path} the item is labelled {claim}-by-client but names {products}, which none "
            f"of its cited requirements name. Do ONE of: (a) change the `origin` field of {path} "
            f"to recommended_by_ai; or (b) keep the origin of {path}, remove {products} from "
            f"that item, and add a separate new item with origin recommended_by_ai that names "
            f"{products}. Keep the requirement references. Do not claim client approval."
        )
    if rule == "completed_implementation_wording":
        return (
            f"Rewrite only the field at {path or 'the flagged text'} using proposal language "
            f'("is proposed", "is designed to", "targets"). Do not change any other field.'
        )
    if rule == "coverage_all_addressed":
        return (
            "Review every coverage_matrix entry. Mark an entry partial or unresolved where the "
            "design lacks a concrete mechanism for it or depends on an open question. Every entry "
            "you mark partial or unresolved MUST state its gap; never mark one partial without a "
            "stated gap. Keep entries that are truly addressed as addressed."
        )
    if rule == "frontend_only_backend_requirement":
        return (
            f"At {path or 'the flagged entry'} add the backend component or decision that "
            "processes the requirement to the entry's related_ids; add a backend component only "
            "if the draft has none."
        )
    if rule == "passages_not_cited":
        return (
            "Cite the supplied passages (P-aliases, in source_passage_ids) on items that they "
            "actually support. If none is relevant, add an open question explaining why the "
            "supplied passages were not used. Never cite a passage only to silence this finding."
        )
    return "Resolve this finding by changing only the item it names."


def revision_outcome(
    before: Mapping[str, Any] | None, after: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Per finding type: how many findings of the source are resolved or still present.

    Findings match on (rule, path); whole-proposal findings have no path and match on the rule.
    `introduced` counts findings the revision added. None when either side was not linted.
    """
    if before is None or after is None:
        return None
    old = Counter((f["rule"], f.get("path")) for f in before.get("findings") or [])
    new = Counter((f["rule"], f.get("path")) for f in after.get("findings") or [])
    rules: dict[str, dict[str, int]] = {}
    for (rule, path), n in old.items():
        entry = rules.setdefault(rule, {"before": 0, "resolved": 0, "still_present": 0})
        kept = min(n, new[(rule, path)])
        entry["before"] += n
        entry["still_present"] += kept
        entry["resolved"] += n - kept
    for (rule, path), n in new.items():
        extra = n - min(n, old[(rule, path)])
        if extra:
            entry = rules.setdefault(rule, {"before": 0, "resolved": 0, "still_present": 0})
            entry["introduced"] = entry.get("introduced", 0) + extra
    return {
        "rules": rules,
        "resolved": sum(e["resolved"] for e in rules.values()),
        "still_present": sum(e["still_present"] for e in rules.values()),
        "introduced": sum(e.get("introduced", 0) for e in rules.values()),
    }
