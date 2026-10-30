"""The pure parts of the iterative CI repair loop (Phase 13E).

Failure signatures, classification of a CI result against the unchanged repository baseline
(novelty and file attribution kept apart), the convergence rule, edit batching, the run-scoped
approval check and the resume checks. No I/O here; the service in app.services.ci_loop does the
GitHub calls and the persistence.
"""

import hashlib
import json
import posixpath
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.domain.execution import FAILING, CaseResult

# Terminal conditions of a CI loop run (AgentRun.stop_reason).
GREEN = "green"
NOT_CONVERGING = "not_converging"
TERMINAL_REASONS = (
    GREEN,
    NOT_CONVERGING,
    "step_limit",
    "token_limit",
    "time_limit",
    "cancelled",
    "ci_unavailable",
    "ci_timeout",
    "approval_revoked",
    "reasoning_budget_exhausted",
)
# Non-green ends that still hand over a reviewable patch when deterministic validation passes.
# The others keep the draft as a checkpoint to resume from instead.
PATCH_ON_STOP = frozenset({NOT_CONVERGING, "step_limit", "token_limit", "time_limit", "ci_timeout"})
CONVERGENCE_ITERATIONS = 3
EDIT_ACTIONS = frozenset({"edit_file", "write_file", "delete_file"})
PROTECTED_BRANCHES = frozenset({"main", "master"})
MAX_FEED_FAILURES = 20
MAX_STORED_RESULTS = 200
MAX_IMPORT_DEPTH = 6
MAX_IMPORT_NODES = 2_000

NEW, PRE_EXISTING, FIXED, FLAKY = "new", "pre_existing", "fixed", "flaky"
CHANGED, ELSEWHERE = "changed", "elsewhere"


# ---- failure signature ---------------------------------------------------------------------

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HEX = re.compile(r"\b0x[0-9a-f]+\b|\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,}\b", re.I)
_DURATION = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:ms|msec|milliseconds?|s|secs?|seconds?|minutes?|min)\b", re.I
)
_LINE = re.compile(r"\bline\s+\d+\b|(?<=\w)(?::\d+)+\b", re.I)
_VOLATILE_ID = re.compile(r"\b(id|pid|port|run|job|thread|object at)([\s=:#]*)\d+\b", re.I)
_TMP = re.compile(r"(/tmp|/var/folders|[A-Za-z]:/Users/[^/\s]+/AppData/Local/Temp)/[^\s'\"]+")
_SPACES = re.compile(r"\s+")
_WORKSPACE = (
    re.compile(r"^.*?/(?:runner/work|__w)/[^/]+/[^/]+/"),  # Linux runners and containers
    re.compile(r"^[A-Za-z]:/a/[^/]+/[^/]+/"),  # Windows runners
    re.compile(r"^/github/workspace/"),
)
_EXCEPTION = re.compile(r"^\s*([A-Za-z_][\w.]*(?:Error|Exception|Failure|Exit|Interrupt))\b")


def normalize_path(path: str | None) -> str:
    """A repository-relative path: CI workspace prefixes, ./ and slashes normalised."""
    if not path:
        return ""
    text = path.replace("\\", "/").strip()
    for pattern in _WORKSPACE:
        text = pattern.sub("", text)
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def normalize_message(message: str) -> str:
    """The message with volatile ids, temporary paths, durations and line numbers removed."""
    text = message.replace("\\", "/")
    text = _UUID.sub("<id>", text)
    text = _TMP.sub("<tmp>", text)
    text = _VOLATILE_ID.sub(r"\1\2<n>", text)
    text = _HEX.sub("<hex>", text)
    text = _DURATION.sub("<duration>", text)
    text = _LINE.sub("", text)
    return _SPACES.sub(" ", text).strip()


def error_category(outcome: str, message: str) -> str:
    """The outcome and, when the message starts with one, the exception type."""
    found = _EXCEPTION.match(message)
    return f"{outcome}:{found.group(1).rsplit('.', 1)[-1]}" if found else outcome


def failure_signature(case: CaseResult) -> str:
    """Deterministic: test name, normalised file, error category and normalised message."""
    parts = [
        case.name,
        normalize_path(case.file),
        error_category(case.outcome, case.message),
        normalize_message(case.message),
    ]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:16]


# ---- import map: does a test reach a changed file? -----------------------------------------

_PY_IMPORT = re.compile(
    r"^\s*(?:from\s+(\.*[\w.]*)\s+import\s+([\w*, ()]+)|import\s+([\w., ]+))", re.M
)
_JS_IMPORT = re.compile(
    r"""(?:\bfrom\s+|\bimport\s*\(?\s*|\brequire\s*\(\s*)['"](\.{1,2}/[^'"]+)['"]"""
)
_JS_SUFFIXES = (
    "", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    "/index.ts", "/index.tsx", "/index.js", "/index.jsx",
)  # fmt: skip


class ImportMap:
    """Static imports between the draft's files: Python import/from and relative JS/TS imports.
    Bounded and heuristic; used only to decide whether an "elsewhere" failure is linked."""

    def __init__(self, files: Mapping[str, str]) -> None:
        self._files = {normalize_path(p): c for p, c in files.items()}
        self._modules: dict[str, set[str]] = {}
        for path in self._files:
            if not path.endswith(".py"):
                continue
            parts = path[:-3].split("/")
            if parts[-1] == "__init__":
                parts = parts[:-1]
            for start in range(len(parts)):
                self._modules.setdefault(".".join(parts[start:]), set()).add(path)
        self._edges: dict[str, set[str]] = {}

    def _python(self, path: str, content: str) -> set[str]:
        package = path.rsplit("/", 1)[0].split("/") if "/" in path else []
        out: set[str] = set()
        for match in _PY_IMPORT.finditer(content):
            source, names, plain = match.groups()
            modules: list[str] = []
            if plain:
                modules = [m.strip().split(" ")[0] for m in plain.split(",") if m.strip()]
            elif source is not None:
                dots = len(source) - len(source.lstrip("."))
                base = source.lstrip(".")
                if dots:
                    up = package[: len(package) - (dots - 1)] if dots - 1 <= len(package) else []
                    base = ".".join([*up, base] if base else up)
                listed = [n.strip(" ()") for n in names.split(",") if n.strip(" ()*")]
                modules = [f"{base}.{n}" if base else n for n in listed] + [base]
            for module in modules:
                if module:
                    out |= self._modules.get(module, set())
        out.discard(path)
        return out

    def _js(self, path: str, content: str) -> set[str]:
        folder = path.rsplit("/", 1)[0] if "/" in path else ""
        out: set[str] = set()
        for spec in _JS_IMPORT.findall(content):
            joined = posixpath.normpath(posixpath.join(folder, spec))
            for suffix in _JS_SUFFIXES:
                if joined + suffix in self._files:
                    out.add(joined + suffix)
                    break
        return out

    def imports(self, path: str) -> set[str]:
        path = normalize_path(path)
        if path not in self._edges:
            content = self._files.get(path, "")
            if path.endswith(".py"):
                self._edges[path] = self._python(path, content)
            elif path.endswith((".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")):
                self._edges[path] = self._js(path, content)
            else:
                self._edges[path] = set()
        return self._edges[path]

    def reaches(self, start: str, targets: Iterable[str]) -> str | None:
        """The first changed file the start file imports, directly or transitively."""
        wanted = {normalize_path(t) for t in targets}
        origin = normalize_path(start)
        seen = {origin}
        frontier = [origin]
        for _ in range(MAX_IMPORT_DEPTH):
            following: list[str] = []
            for path in frontier:
                for found in sorted(self.imports(path)):
                    if found in wanted:
                        return found
                    if found not in seen and len(seen) < MAX_IMPORT_NODES:
                        seen.add(found)
                        following.append(found)
            if not following:
                return None
            frontier = following
        return None


# ---- classification ------------------------------------------------------------------------


@dataclass(frozen=True)
class Classified:
    """One CI result against the baseline: novelty and file attribution are separate labels."""

    name: str
    file: str
    outcome: str
    category: str
    message: str
    signature: str
    novelty: str  # new | pre_existing | fixed | flaky
    scope: str  # changed | elsewhere (the test's file against the changed files)
    linked_via: str | None = None  # a changed file the trace or the import map reaches

    @property
    def actionable(self) -> bool:
        """What the model is asked to fix: a new failure in a changed file or linked to one."""
        return self.novelty == NEW and (self.scope == CHANGED or self.linked_via is not None)

    def as_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "file": self.file,
            "outcome": self.outcome,
            "category": self.category,
            "message": self.message,
            "signature": self.signature,
            "novelty": self.novelty,
            "scope": self.scope,
            "linked_via": self.linked_via,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Classified":
        return cls(
            str(data["name"]), str(data.get("file") or ""), str(data["outcome"]),
            str(data.get("category") or ""), str(data.get("message") or ""),
            str(data.get("signature") or ""), str(data["novelty"]), str(data["scope"]),
            data.get("linked_via"),
        )  # fmt: skip


@dataclass(frozen=True)
class Classification:
    results: list[Classified]
    counts: dict[str, int]

    @property
    def new(self) -> list[Classified]:
        return [r for r in self.results if r.novelty == NEW]

    @property
    def actionable(self) -> list[Classified]:
        return [r for r in self.results if r.actionable]

    @property
    def unlinked_elsewhere(self) -> list[Classified]:
        """Shown to the reviewer only; never sent to the model."""
        return [r for r in self.new if r.scope == ELSEWHERE and r.linked_via is None]

    @property
    def failing(self) -> list[Classified]:
        return [r for r in self.results if r.outcome in FAILING]

    @property
    def blocking_signatures(self) -> set[str]:
        return {r.signature for r in self.new}

    def model_feed(self) -> dict[str, Any]:
        """Only new actionable failures, flaky ones, and counts of the rest."""

        def brief(r: Classified) -> dict[str, Any]:
            item = {
                "test": r.name,
                "file": r.file,
                "category": r.category,
                "message": r.message,
                "in_changed_file": r.scope == CHANGED,
            }
            if r.linked_via:
                item["linked_to_changed_file"] = r.linked_via
            return item

        return {
            "actionable_failures": [brief(r) for r in self.actionable[:MAX_FEED_FAILURES]],
            "actionable_total": len(self.actionable),
            "flaky": [{"test": r.name, "file": r.file} for r in self.results if r.novelty == FLAKY][
                :MAX_FEED_FAILURES
            ],
            "counts": {
                "pre_existing": self.counts[PRE_EXISTING],
                "elsewhere": self.counts[ELSEWHERE],
                "fixed": self.counts[FIXED],
            },
        }


def _link(case: CaseResult, file: str, changed: set[str], imports: ImportMap | None) -> str | None:
    trace = case.trace.replace("\\", "/")
    for path in sorted(changed):
        if path and path in trace:
            return path
    if imports is not None and file:
        return imports.reaches(file, changed)
    return None


def classify(
    cases: Iterable[CaseResult],
    *,
    baseline_failing: Iterable[str] | None,
    previous_failing: Iterable[str] = (),
    changed_paths: Iterable[str],
    imports: ImportMap | None = None,
    scrub: Any = None,
) -> Classification:
    """Each failing, flaky or fixed result of a CI run, against the unchanged baseline.

    novelty: new (failing, not failing in the baseline), pre_existing (failing in both), fixed
    (failing in the baseline or the previous iteration, passing now), flaky (JUnit rerun
    markers). scope: whether the test's file is a changed file. A new failure elsewhere is
    linked when its trace names a changed file or its imports reach one."""
    clean = scrub or (lambda text: text)
    changed = {normalize_path(p) for p in changed_paths}
    before = set(baseline_failing or ())
    earlier = set(previous_failing) | before
    results: list[Classified] = []
    for case in cases:
        failing = case.outcome in FAILING
        if case.flaky:
            novelty = FLAKY
        elif failing:
            novelty = PRE_EXISTING if case.name in before else NEW
        elif case.outcome == "passed" and case.name in earlier:
            novelty = FIXED
        else:
            continue
        file = normalize_path(case.file)
        scope = CHANGED if file and file in changed else ELSEWHERE
        linked = None
        if novelty == NEW and scope == ELSEWHERE:
            linked = _link(case, file, changed, imports)
        results.append(
            Classified(
                case.name[:300],
                file[:300],
                case.outcome,
                error_category(case.outcome, case.message),
                clean(case.message)[:300],
                failure_signature(case),
                novelty,
                scope,
                linked,
            )
        )
    counts = {
        NEW: sum(1 for r in results if r.novelty == NEW),
        PRE_EXISTING: sum(1 for r in results if r.novelty == PRE_EXISTING),
        FIXED: sum(1 for r in results if r.novelty == FIXED),
        FLAKY: sum(1 for r in results if r.novelty == FLAKY),
        ELSEWHERE: sum(1 for r in results if r.novelty == NEW and r.scope == ELSEWHERE),
    }
    return Classification(results, counts)


def is_green(classification: Classification, conclusion: str, report: bool) -> bool:
    """Tests pass: no new failure (pre-existing and flaky ones are tolerated, and shown)."""
    if not report:
        return conclusion == "success"
    if classification.new:
        return False
    if conclusion == "success":
        return True
    # A failed workflow is green only when every failing case was already failing or is flaky.
    return conclusion == "failure" and bool(classification.failing)


# ---- convergence ---------------------------------------------------------------------------


@dataclass
class Convergence:
    """Not converging: one normalised failure signature is in each of the last three CI
    iterations and the number of blocking failures did not go down. A result of a workflow run
    already counted (cached or repeated) is not a new iteration."""

    limit: int = CONVERGENCE_ITERATIONS
    history: list[tuple[int, list[str], int]] = field(default_factory=list)

    def add(self, workflow_run_id: int, signatures: Iterable[str], blocking: int) -> bool:
        if any(h[0] == workflow_run_id for h in self.history):
            return False
        self.history.append((workflow_run_id, sorted(set(signatures)), blocking))
        return True

    def not_converging(self) -> bool:
        if len(self.history) < self.limit:
            return False
        last = self.history[-self.limit :]
        common = set(last[0][1]).intersection(*(set(h[1]) for h in last[1:]))
        if not common:
            return False
        first = last[0][2]
        return all(h[2] >= first for h in last[1:])

    def as_json(self) -> list[list[Any]]:
        return [[r, s, n] for r, s, n in self.history]

    @classmethod
    def from_json(cls, data: Any) -> "Convergence":
        found = cls()
        for item in data or []:
            if isinstance(item, list) and len(item) == 3:
                found.history.append((int(item[0]), [str(s) for s in item[1]], int(item[2])))
        return found


# ---- edit batching -------------------------------------------------------------------------


@dataclass
class Batch:
    """Edits are batched; CI runs only when the model asks (run_tests/finish), when the
    automatic static hook reports zero actionable findings after an edit, or when the batch
    reaches its size."""

    max_edits: int
    edits: int = 0

    def after(self, action: str, ok: bool, static_total: int | None) -> str | None:
        if not ok or action not in EDIT_ACTIONS:
            return None
        self.edits += 1
        if static_total == 0:
            return "static_clean"
        if self.edits >= self.max_edits:
            return "batch_full"
        return None

    def reset(self) -> None:
        self.edits = 0


# ---- approval and resume -------------------------------------------------------------------


def protected(branch: str, *, base_branch: str, default_branch: str) -> bool:
    name = branch.strip().lower()
    return name in PROTECTED_BRANCHES | {base_branch.lower(), default_branch.lower()}


def approval_problem(
    record: Mapping[str, Any] | None,
    *,
    project_id: str,
    run_id: str,
    branch: str,
    base_branch: str,
    default_branch: str,
) -> str | None:
    """Why the stored approval does not permit pushing this branch now (None: it does)."""
    if protected(branch, base_branch=base_branch, default_branch=default_branch):
        return "the branch is a base, default, main or master branch"
    if record is None:
        return "no approval"
    if record.get("revoked_at"):
        return "the approval was revoked"
    if (
        record.get("project_id") != project_id
        or record.get("run_id") != run_id
        or record.get("branch") != branch
    ):
        return "the approval is for another project, run or branch"
    return None


def resume_problems(
    checkpoint: Mapping[str, Any],
    *,
    remote: str,
    base_branch: str,
    checklist_id: str | None,
    checklist_version: int | None,
    branch_head: str | None,
) -> list[tuple[str, str]]:
    """Why a CI checkpoint cannot be resumed: (code, message) pairs, empty when it can."""
    problems: list[tuple[str, str]] = []
    if checkpoint.get("remote") != remote:
        problems.append(("remote_changed", "The repository remote changed since the checkpoint"))
    if checkpoint.get("base_branch") != base_branch:
        problems.append(("base_branch_changed", "The base branch changed since the checkpoint"))
    if (
        checkpoint.get("checklist_id") != checklist_id
        or checkpoint.get("checklist_version") != checklist_version
    ):
        problems.append(
            ("checklist_changed", "The approved checklist version changed since the checkpoint")
        )
    expected = checkpoint.get("pushed_commit") or checkpoint.get("parent_commit")
    if branch_head is None:
        problems.append(("ci_branch_missing", "The CI branch no longer exists"))
    elif expected and branch_head != expected:
        problems.append(
            ("foreign_commits", "The CI branch has commits that this run did not create")
        )
    return problems
