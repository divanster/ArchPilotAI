"""Builds the frontend contract fixtures from the API models.

Run `python -m tests.contract_fixtures` from backend/ to rewrite them; the test suite fails when
the committed files drift from what the models produce.
"""

import datetime as dt
import json
import uuid
from pathlib import Path
from typing import Any

from app.api.patches import (
    PatchResponse,
    RemovalItemOut,
    RemovalMissingOut,
    RemovalsNotConfirmedBody,
)

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "frontend" / "src" / "fixtures"


def patch_fixture() -> dict[str, Any]:
    patch = PatchResponse(
        id=uuid.UUID(int=1),
        task_key="T-001",
        version=1,
        status="proposed",
        provider="fake",
        model="fake",
        prompt_version="v1",
        created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        reviewed_by=None,
        review_reason=None,
        plan_id=uuid.UUID(int=2),
        task_id=uuid.UUID(int=3),
        input_tokens=None,
        output_tokens=None,
        latency_ms=None,
        notes=[],
        assumptions=[],
        test_suggestions=[],
        warnings=[],
        blocking_acknowledged=False,
        removed_from_accepted=[
            RemovalItemOut(
                kind="class", name="Invoice", file="src/a.py", listed=True, reason="replaced"
            ),
            RemovalItemOut(kind="function", name="total", file="src/a.py", listed=False, reason=""),
        ],
        needs_justification=True,
        validation="passed",
        stale=[],
        files=[],
    )
    return patch.model_dump(mode="json")


def conflict_fixture() -> dict[str, Any]:
    body = RemovalsNotConfirmedBody(
        message="Removals not confirmed",
        missing=[RemovalMissingOut(file="src/a.py", name="total")],
    )
    return {"detail": body.model_dump()}


FIXTURES = {
    "patch_with_removals.json": patch_fixture,
    "removals_not_confirmed.json": conflict_fixture,
}


def render(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2) + "\n"


if __name__ == "__main__":
    for name, build in FIXTURES.items():
        (FIXTURE_DIR / name).write_text(render(build()), encoding="utf-8", newline="\n")
