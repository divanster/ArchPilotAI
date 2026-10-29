"""Execution provider interface (Phase 11B-prep). Pure: no process, no file, no network.

Nothing here runs code. A provider is whatever will run an allowlisted command id on a copy of
the files; this phase ships only a scripted fake (tests) and an "unavailable" provider. A
container provider can come later behind the same interface.

Test results are structured: pytest (and vitest through its JUnit reporter) are asked for JUnit
XML and that XML is parsed. Output text is never scraped for test names or outcomes.
"""

import threading
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

# command id -> (what it is, whether it reports structured test cases as JUnit XML)
COMMANDS: dict[str, tuple[str, bool]] = {
    "pytest": ("python -m pytest, JUnit XML report", True),
    "ruff": ("ruff check", False),
    "mypy": ("mypy", False),
    "tsc": ("tsc --noEmit", False),
    "npm test": ("npm test, JUnit XML report", True),
}
TEST_COMMANDS = tuple(c for c, (_, junit) in COMMANDS.items() if junit)

MAX_LOG_CHARS = 1_000_000  # the full output kept in the run log
MAX_REPORT_BYTES = 5_000_000
MAX_CASES = 5_000
MAX_MESSAGE_CHARS = 300
DEFAULT_TIMEOUT = 120.0
DEFAULT_OUTPUT_CAP = 4_000

IN_CHANGED = "in changed files"
ELSEWHERE = "elsewhere"
UNAVAILABLE_MESSAGE = "tests unavailable"


class UnknownCommandError(ValueError):
    """A command id that is not on the allowlist. Free text is never a command."""


class ProviderUnavailableError(RuntimeError):
    pass


def check_command(command_id: str) -> str:
    if not isinstance(command_id, str) or command_id not in COMMANDS:
        raise UnknownCommandError("Unknown command id. Allowed: " + ", ".join(COMMANDS))
    return command_id


def check_select(select: str) -> str:
    """A single test id for a re-run: bounded, printable, never an option ("-x") or a path jump."""
    if not select or len(select) > 300 or select.startswith("-") or not select.isprintable():
        raise UnknownCommandError("Invalid test selection")
    return select


def tail(text: str, cap: int) -> tuple[str, bool]:
    """The last `cap` characters and whether anything was cut."""
    if cap <= 0:
        return "", bool(text)
    if len(text) <= cap:
        return text, False
    return text[-cap:], True


@dataclass(frozen=True)
class ExecutionResult:
    exit_code: int | None  # None: no exit code (timeout, cancel)
    stdout_tail: str = ""
    stderr_tail: str = ""
    duration: float = 0.0  # seconds
    truncated: bool = False
    timed_out: bool = False
    cancelled: bool = False
    report: str | None = None  # JUnit XML text, when the command produces one
    log: str = ""  # the full output; kept in the run log, never sent to the model


def make_result(
    *,
    exit_code: int | None,
    stdout: str = "",
    stderr: str = "",
    duration: float = 0.0,
    output_cap: int = DEFAULT_OUTPUT_CAP,
    timed_out: bool = False,
    cancelled: bool = False,
    report: str | None = None,
) -> ExecutionResult:
    out, cut_out = tail(stdout, output_cap)
    err, cut_err = tail(stderr, output_cap)
    log = (stdout + ("\n" if stdout and stderr else "") + stderr)[:MAX_LOG_CHARS]
    return ExecutionResult(
        exit_code, out, err, duration, cut_out or cut_err, timed_out, cancelled, report, log
    )


class ExecutionProvider(Protocol):
    """Runs one allowlisted command on a copy of `files`. Implementations must not run it
    anywhere but their own isolated environment."""

    name: str

    def status(self) -> tuple[bool, str]:
        """(available, reason). The reason is shown when it is not available."""
        ...

    def run(
        self,
        command_id: str,
        files: Mapping[str, str],
        timeout: float,
        output_cap: int,
        *,
        job_id: str = "",
        select: str | None = None,
    ) -> ExecutionResult:
        """`select` is one test id to run alone (never a shell string)."""
        ...

    def cancel(self, job_id: str) -> None: ...


class UnavailableExecutionProvider:
    """No execution environment is configured. It never runs anything and says so."""

    name = "unavailable"

    def __init__(self, reason: str = "No execution provider is configured") -> None:
        self._reason = reason

    def status(self) -> tuple[bool, str]:
        return False, self._reason

    def run(
        self,
        command_id: str,
        files: Mapping[str, str],
        timeout: float,
        output_cap: int,
        *,
        job_id: str = "",
        select: str | None = None,
    ) -> ExecutionResult:
        check_command(command_id)
        raise ProviderUnavailableError(UNAVAILABLE_MESSAGE)

    def cancel(self, job_id: str) -> None:
        return None


@dataclass
class FakeCall:
    command_id: str
    paths: list[str]
    select: str | None
    job_id: str


class FakeExecutionProvider:
    """Scripted results for tests. Results are consumed per key in order; the last one repeats.

    Keys: "pytest" for a whole run, "pytest::<test id>" for a single-test re-run. A result may be
    HANG: the call then waits until `timeout` (a timed-out result) or `cancel` (a cancelled one).
    """

    name = "fake"
    HANG = "hang"

    def __init__(self, script: Mapping[str, Sequence[ExecutionResult | str]] | None = None) -> None:
        self._script = {k: list(v) for k, v in (script or {}).items()}
        self._events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self.calls: list[FakeCall] = []

    def status(self) -> tuple[bool, str]:
        return True, ""

    def add(self, key: str, *results: ExecutionResult | str) -> None:
        self._script.setdefault(key, []).extend(results)

    def _next(self, key: str) -> ExecutionResult | str | None:
        queue = self._script.get(key)
        if not queue:
            return None
        return queue.pop(0) if len(queue) > 1 else queue[0]

    def run(
        self,
        command_id: str,
        files: Mapping[str, str],
        timeout: float,
        output_cap: int,
        *,
        job_id: str = "",
        select: str | None = None,
    ) -> ExecutionResult:
        check_command(command_id)
        if select:
            check_select(select)
        event = threading.Event()
        with self._lock:
            self.calls.append(FakeCall(command_id, sorted(files), select, job_id))
            self._events[job_id] = event
            item = self._next(f"{command_id}::{select}" if select else command_id)
        if item is None:
            return make_result(exit_code=0, output_cap=output_cap)
        if item == self.HANG:
            was_cancelled = event.wait(timeout)
            return make_result(
                exit_code=None,
                output_cap=output_cap,
                timed_out=not was_cancelled,
                cancelled=was_cancelled,
                duration=timeout,
            )
        assert isinstance(item, ExecutionResult)
        full = item.log or item.stdout_tail
        stdout, cut_out = tail(full, output_cap)
        stderr, cut_err = tail(item.stderr_tail, output_cap)
        return ExecutionResult(
            item.exit_code,
            stdout,
            stderr,
            item.duration,
            item.truncated or cut_out or cut_err,
            item.timed_out,
            item.cancelled,
            item.report,
            full,
        )

    def cancel(self, job_id: str) -> None:
        with self._lock:
            event = self._events.get(job_id)
        if event is not None:
            event.set()


# ---- structured test results -----------------------------------------------------------------


@dataclass(frozen=True)
class CaseResult:
    name: str
    outcome: str  # passed | failed | error | skipped
    message: str
    file: str | None
    line: int | None

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "outcome": self.outcome,
            "message": self.message,
            "file": self.file,
            "line": self.line,
        }


FAILING = ("failed", "error")


def _file_of(classname: str, attrs: Mapping[str, str]) -> str | None:
    """The test file. JUnit's `file` attribute when present; otherwise the module part of the
    classname (pytest: "tests.test_x.TestY" -> "tests/test_x.py"). Heuristic, documented."""
    if attrs.get("file"):
        return attrs["file"].replace("\\", "/")
    module: list[str] = []
    for part in (p for p in classname.split(".") if p):
        if part[:1].isupper():
            break
        module.append(part)
    return "/".join(module) + ".py" if module else None


def parse_junit(xml_text: str) -> list[CaseResult]:
    """Test cases from a JUnit XML report. Raises ValueError for anything that is not one."""
    if len(xml_text.encode("utf-8", "ignore")) > MAX_REPORT_BYTES:
        raise ValueError("report too large")
    # Entity declarations are how XML bombs work; a JUnit report has none.
    if "<!DOCTYPE" in xml_text or "<!ENTITY" in xml_text:
        raise ValueError("report refused: DOCTYPE/ENTITY")
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        raise ValueError("report is not valid XML") from None
    cases: list[CaseResult] = []
    for node in root.iter("testcase"):
        if len(cases) >= MAX_CASES:
            break
        classname = node.get("classname", "")
        name = node.get("name", "")
        full = f"{classname}::{name}" if classname else name
        outcome, message = "passed", ""
        for child in node:
            if child.tag in ("failure", "error", "skipped"):
                outcome = "failed" if child.tag == "failure" else child.tag
                message = (child.get("message") or (child.text or "")).strip()
                break
        raw_line = node.get("line", "")
        cases.append(
            CaseResult(
                full,
                outcome,
                message[:MAX_MESSAGE_CHARS],
                _file_of(classname, node.attrib),
                int(raw_line) if raw_line.isdigit() else None,
            )
        )
    return cases


# ---- baseline comparison, attribution --------------------------------------------------------


@dataclass(frozen=True)
class Comparison:
    new: list[CaseResult]
    pre_existing: list[CaseResult]
    fixed: list[CaseResult]


def compare_baseline(
    baseline: Iterable[CaseResult] | None, changed: Iterable[CaseResult]
) -> Comparison:
    """Failures of the changed copy as new or pre-existing, and baseline failures now passing.
    Without a baseline (a new repository) every failure is new."""
    after = list(changed)
    before = {c.name: c for c in baseline or []}
    failed_before = {n for n, c in before.items() if c.outcome in FAILING}
    now = {c.name: c for c in after}
    new = [c for c in after if c.outcome in FAILING and c.name not in failed_before]
    old = [c for c in after if c.outcome in FAILING and c.name in failed_before]
    fixed = [now[n] for n in sorted(failed_before) if n in now and now[n].outcome == "passed"]
    return Comparison(new, old, fixed)


def scope_of(file: str | None, changed_paths: Iterable[str]) -> str:
    """ "in changed files" when the finding's file is one the change touches, else "elsewhere".
    A finding with no file cannot be tied to the change, so it is "elsewhere"."""
    if file and file.replace("\\", "/") in set(changed_paths):
        return IN_CHANGED
    return ELSEWHERE


def attribute(
    items: Iterable[Mapping[str, Any]], changed_paths: Iterable[str]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Each finding (a dict with "file") tagged with a "scope", and the counts per scope."""
    paths = set(changed_paths)
    out: list[dict[str, Any]] = []
    counts = {IN_CHANGED: 0, ELSEWHERE: 0}
    for item in items:
        scope = scope_of(item.get("file"), paths)
        counts[scope] += 1
        out.append({**item, "scope": scope})
    return out, counts
