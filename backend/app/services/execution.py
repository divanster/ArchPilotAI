"""Runs allowlisted commands through an execution provider as jobs (Phase 11B-prep).

This service never runs anything itself: it only calls the provider it was given. With the
"unavailable" provider it raises and the caller reports "tests unavailable"; there is no
fallback to local execution.
"""

import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.domain.execution import (
    COMMANDS,
    ELSEWHERE,
    FAILING,
    IN_CHANGED,
    CaseResult,
    ExecutionProvider,
    ExecutionResult,
    ProviderUnavailableError,
    check_command,
    compare_baseline,
    parse_junit,
    scope_of,
)

MAX_NEW_FAILURES = 20  # failures listed to the model; the rest are counted
MAX_RERUNS = 10  # failed tests re-run alone, per command
# CI facts the model may see (no person's name, no token: there is none in the meta at all).
CI_PAYLOAD_KEYS = ("provider", "status", "conclusion", "workflow_url", "branch", "error")

# ---- open jobs of running runs ---------------------------------------------------------------
# `stop` arrives on another thread and session, so open jobs are found here, by run id.

_lock = threading.Lock()
_open: dict[uuid.UUID, dict[str, ExecutionProvider]] = {}
_cancelled: set[uuid.UUID] = set()


def cancel_run_jobs(run_id: uuid.UUID) -> int:
    """Cancel every open job of the run and refuse new ones. Returns how many were open."""
    with _lock:
        _cancelled.add(run_id)
        jobs = dict(_open.get(run_id, {}))
    for job_id, provider in jobs.items():
        provider.cancel(job_id)
    return len(jobs)


def release_run(run_id: uuid.UUID) -> None:
    cancel_run_jobs(run_id)
    with _lock:
        _open.pop(run_id, None)
        _cancelled.discard(run_id)


def open_jobs(run_id: uuid.UUID) -> list[str]:
    with _lock:
        return sorted(_open.get(run_id, {}))


@dataclass
class JobRecord:
    id: str
    command_id: str
    variant: str  # baseline | changed | rerun
    status: str  # done | timed_out | cancelled
    timeout: float
    exit_code: int | None = None
    duration_ms: int = 0
    truncated: bool = False
    select: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    output: str = ""


@dataclass
class TestOutcome:
    __test__ = False  # not a pytest class

    payload: dict[str, Any]  # what the model gets (bounded)
    jobs: list[JobRecord]


class TestRunner:
    __test__ = False  # not a pytest class

    def __init__(self, provider: ExecutionProvider, *, timeout: float, output_cap: int) -> None:
        self._provider = provider
        self._timeout = timeout
        self._cap = output_cap

    def status(self) -> tuple[bool, str]:
        return self._provider.status()

    @property
    def provider_name(self) -> str:
        return self._provider.name

    def bind(self, project_id: uuid.UUID, run_id: uuid.UUID) -> None:
        """Lets a provider that works per run (the CI provider) know which run it serves."""
        binder = getattr(self._provider, "bind", None)
        if callable(binder):
            self._provider = binder(project_id, run_id)

    def release(self, run_id: uuid.UUID) -> None:
        releaser = getattr(self._provider, "release", None)
        if callable(releaser):
            releaser(run_id)

    def _supports(self, capability: str) -> bool:
        return bool(getattr(self._provider, capability, True))

    def _job(
        self,
        run_id: uuid.UUID,
        command_id: str,
        files: Mapping[str, str],
        variant: str,
        jobs: list[JobRecord],
        select: str | None = None,
    ) -> ExecutionResult:
        job_id = uuid.uuid4().hex
        record = JobRecord(job_id, command_id, variant, "done", self._timeout, select=select)
        jobs.append(record)
        with _lock:
            if run_id in _cancelled:
                result = ExecutionResult(None, cancelled=True)
                record.status = "cancelled"
                return result
            _open.setdefault(run_id, {})[job_id] = self._provider
        started = time.monotonic()
        try:
            result = self._provider.run(
                command_id, files, self._timeout, self._cap, job_id=job_id, select=select
            )
        finally:
            with _lock:
                _open.get(run_id, {}).pop(job_id, None)
        record.status = (
            "cancelled" if result.cancelled else "timed_out" if result.timed_out else "done"
        )
        record.exit_code = result.exit_code
        record.duration_ms = int((result.duration or time.monotonic() - started) * 1000)
        record.truncated = result.truncated
        record.output = result.log  # the full output stays here, in the run log
        if result.meta:
            record.summary.update(result.meta)
        return result

    @staticmethod
    def _cases(result: ExecutionResult) -> list[CaseResult] | None:
        if result.report is None:
            return None
        try:
            return parse_junit(result.report)
        except ValueError:
            return None

    def run(
        self,
        run_id: uuid.UUID,
        command_id: str,
        changed_files: Mapping[str, str],
        baseline_files: Mapping[str, str] | None,
        changed_paths: list[str],
    ) -> TestOutcome:
        """Baseline (the unmodified workspace, when there is one) and changed copy, then a lone
        re-run of each failed test. Raises UnknownCommandError / ProviderUnavailableError."""
        check_command(command_id)
        ok, reason = self._provider.status()
        if not ok:
            raise ProviderUnavailableError(reason)
        jobs: list[JobRecord] = []
        has_baseline = bool(baseline_files) and self._supports("supports_baseline")
        before: ExecutionResult | None = None
        if has_baseline:
            before = self._job(run_id, command_id, baseline_files or {}, "baseline", jobs)
        after = self._job(run_id, command_id, changed_files, "changed", jobs)
        payload: dict[str, Any] = {
            "command_id": command_id,
            "exit_code": after.exit_code,
            "baseline": has_baseline,
            "truncated": after.truncated,
            "stdout_tail": after.stdout_tail,
            "stderr_tail": after.stderr_tail,
        }
        ci = (after.meta or {}).get("ci")
        if isinstance(ci, Mapping):
            payload["ci"] = {k: ci.get(k) for k in CI_PAYLOAD_KEYS if k in ci}
        if after.cancelled or after.timed_out:
            payload["status"] = "cancelled" if after.cancelled else "timed_out"
            return TestOutcome(payload, jobs)
        payload["status"] = "ok" if after.exit_code == 0 else "failed"
        cases = self._cases(after)
        if cases is None:
            if COMMANDS[command_id][1]:
                payload["structured"] = False
                payload["note"] = "no readable JUnit report; results are the exit code only"
            elif has_baseline and before is not None and before.exit_code not in (0, None):
                payload["pre_existing_failure"] = after.exit_code not in (0, None)
            return TestOutcome(payload, jobs)
        base_cases = self._cases(before) if before is not None else None
        comparison = compare_baseline(base_cases if has_baseline else None, cases)
        flaky = self._reruns(run_id, command_id, changed_files, comparison, jobs)
        flaky_names = {c.name for c in flaky}
        new = [c for c in comparison.new if c.name not in flaky_names]
        old = [c for c in comparison.pre_existing if c.name not in flaky_names]
        paths = set(changed_paths)
        in_changed = [c for c in new if scope_of(c.file, paths) == IN_CHANGED]
        payload.update(
            {
                "structured": True,
                "total": len(cases),
                "failed": len(new) + len(old),
                "new": [
                    {**c.as_json(), "scope": IN_CHANGED} for c in in_changed[:MAX_NEW_FAILURES]
                ],
                "new_in_changed": len(in_changed),
                "new_elsewhere": len(new) - len(in_changed),
                "scope_labels": {"changed": IN_CHANGED, "other": ELSEWHERE},
                "pre_existing": len(old),
                "fixed": len(comparison.fixed),
                "flaky": sorted(flaky_names),
                "listed_truncated": len(in_changed) > MAX_NEW_FAILURES,
            }
        )
        return TestOutcome(payload, jobs)

    def _reruns(
        self,
        run_id: uuid.UUID,
        command_id: str,
        files: Mapping[str, str],
        comparison: Any,
        jobs: list[JobRecord],
    ) -> list[CaseResult]:
        """Each failed test is run alone once. A pass marks it flaky (recorded on its job)."""
        flaky: list[CaseResult] = []
        if not self._supports("supports_rerun"):
            return flaky
        failed = [c for c in [*comparison.new, *comparison.pre_existing] if c.outcome in FAILING]
        for case in failed[:MAX_RERUNS]:
            result = self._job(run_id, command_id, files, "rerun", jobs, select=case.name)
            again = self._cases(result)
            passed = (
                result.exit_code == 0
                if again is None
                else any(c.name == case.name and c.outcome == "passed" for c in again)
            )
            jobs[-1].summary.update({"test": case.name, "flaky": passed})
            if passed and not (result.cancelled or result.timed_out):
                flaky.append(case)
        return flaky
