"""CSV of every run and a short Markdown summary comparing deployments."""

import csv
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from statistics import mean

from app.eval.harness import CSV_FIELDS, RunMetrics


def write_csv(rows: Sequence[RunMetrics], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: "" if v is None else v for k, v in asdict(row).items()})


def _avg(values: Sequence[int]) -> str:
    return f"{mean(values):.1f}" if values else "-"


def _reasoning(rows: Sequence[RunMetrics]) -> str:
    reported = [r.reasoning_tokens for r in rows if r.reasoning_tokens is not None]
    return _avg(reported) if reported else "n/r"


def markdown_summary(
    rows: Sequence[RunMetrics], *, task_key: str, live: bool, database: Path | None = None
) -> str:
    deployments = list(dict.fromkeys(r.deployment for r in rows))
    lines = [
        f"# Agent evaluation: {task_key}",
        "",
        f"- Provider: {'live (real model calls)' if live else 'fake (no model calls)'}",
        f"- Runs: {len(rows)} across {len(deployments)} deployment(s)",
    ]
    if database is not None:
        lines.append(f"- Database: `{database}`")
    lines += [
        "",
        "Means per run; 'n/r' = not reported by the provider.",
        "",
        "| Deployment | Runs | Finished | Patch | Steps | Prompt tok | Completion tok "
        "| Reasoning tok | Latency ms | Covered | Missing | Unchecked | Removals | Deleted files "
        "| Findings | Stop reasons |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for dep in deployments:
        group = [r for r in rows if r.deployment == dep]
        started = [r for r in group if r.status != "not_started"]
        reasons = Counter(r.stop_reason or r.status for r in group)
        cells = [
            dep,
            str(len(group)),
            str(sum(r.status == "finished" for r in group)),
            str(sum(bool(r.patch_id) for r in group)),
            _avg([r.steps for r in started]),
            _avg([r.prompt_tokens for r in started]),
            _avg([r.completion_tokens for r in started]),
            _reasoning(started),
            _avg([r.latency_ms for r in started]),
            _avg([r.coverage_covered for r in started]),
            _avg([r.coverage_missing for r in started]),
            _avg([r.coverage_unchecked for r in started]),
            _avg([r.removals_from_accepted for r in started]),
            _avg([r.deleted_accepted_files for r in started]),
            _avg([r.static_findings for r in started]),
            ", ".join(f"{k} x{n}" for k, n in sorted(reasons.items())),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    unavailable = sorted({t for r in rows for t in r.static_unavailable.split(";") if t})
    if unavailable:
        lines += ["", f"Static checkers unavailable in some runs: {', '.join(unavailable)}."]
    errors = Counter(r.error for r in rows if r.error)
    if errors:
        lines += [
            "",
            "Runs that did not start: " + ", ".join(f"{k} x{n}" for k, n in errors.items()),
        ]
    return "\n".join(lines) + "\n"
