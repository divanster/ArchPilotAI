"""Task decomposition (Phase 13D): an ordered checklist of cohesive coding steps for a plan task.

Pure functions only. Nothing is invented:
- every acceptance criterion of a step, or of the manual-verification list, is a verbatim
  criterion of the task, and every task criterion is covered by a step or marked for manual
  verification;
- an expected file that claims to exist (source task, dependency or repository) is a known file:
  an accepted workspace file or a file of the repository code map. A file a step will create is
  marked "new". A step without expected files says that file discovery happens during the run.

The deterministic build groups criteria into cohesive steps. A criterion joins an earlier group
when no member conflicts with it (same dependency prerequisites; when both sides are known, the
same architectural component and substantially the same files) and a member shares a positive
signal with it (the same component, the same files or, without either, the same subject term).
A step covers at most GROUP_MAX_CRITERIA criteria.
"""

import heapq
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.domain.patch import path_problem
from app.domain.plan import find_path

STEP_KEY = re.compile(r"^S-\d{2}$")
MAX_STEPS = 40
MAX_TITLE_CHARS = 200
MAX_GOAL_CHARS = 1000
MAX_REASON_CHARS = 500
MAX_NOTE_CHARS = 300
MAX_FILES_PER_STEP = 8  # a revision with more is refused
MAX_RELATED_PER_STEP = 5
TITLE_CUT = 100
# The deterministic build stays within the quality limits below.
GROUP_MAX_CRITERIA = 5
GROUP_MAX_FILES = 5
FILE_OVERLAP = 0.5  # "substantially the same files": Jaccard similarity of the expected paths
# A step beyond these limits gets a quality warning (it is still valid).
QUALITY_MAX_FILES = 5
QUALITY_MAX_CRITERIA = 5
# Where an expected file comes from, in order of preference; "new" is a file the step creates.
FILE_SOURCES = ("task", "dependency", "repository", "new")
EXISTING_SOURCES = frozenset(FILE_SOURCES[:3])
COMPONENT_KIND = "component"
REPOSITORY_STATES = ("none", "unavailable", "mapped")
# Only explicit wording marks a criterion for manual verification in the deterministic build.
_MANUAL = re.compile(
    r"\b(manual(ly)?\s+(verif\w*|check\w*|test\w*|review\w*|inspect\w*)"
    r"|(verified|checked|tested|reviewed|inspected)\s+manually"
    r"|sign[- ]?off|signed[- ]off)\b",
    re.IGNORECASE,
)
MANUAL_REASON = "The criterion asks for manual verification: a person checks it; it is not coded."
_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "into", "are", "can", "has", "have",
        "not", "all", "any", "each", "its", "their", "when", "then", "must", "should", "will",
        "shall", "may", "only", "per", "via", "use", "uses", "used", "user", "users", "one",
        "new", "app", "src", "test", "tests", "index", "main", "init", "file",
        "py", "ts", "tsx", "js", "json", "md", "toml", "yaml", "yml", "css", "html", "sql", "txt",
    }
)  # fmt: skip


@dataclass(frozen=True)
class ExpectedFile:
    path: str
    source: str  # one of FILE_SOURCES

    def as_json(self) -> dict[str, str]:
        return {"path": self.path, "source": self.source}


@dataclass(frozen=True)
class RelatedItem:
    """A graph item the step touches (from the task's affected graph items)."""

    node_id: str
    kind: str
    label: str

    def as_json(self) -> dict[str, str]:
        return {"node_id": self.node_id, "kind": self.kind, "label": self.label}


@dataclass(frozen=True)
class CriterionReason:
    """A criterion with a reason: why a step repeats it, or why it is verified manually."""

    criterion: str
    reason: str

    def as_json(self) -> dict[str, str]:
        return {"criterion": self.criterion, "reason": self.reason}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "CriterionReason":
        return cls(str(data["criterion"]), str(data["reason"]))


@dataclass(frozen=True)
class Step:
    key: str
    title: str
    goal: str
    acceptance_criteria: tuple[str, ...]
    expected_files: tuple[ExpectedFile, ...] = ()
    depends_on: tuple[str, ...] = ()
    related_items: tuple[RelatedItem, ...] = ()
    grouping_reason: str = ""
    discovery_note: str = ""  # set when the files are found during the run
    shared_criteria: tuple[CriterionReason, ...] = ()  # criteria also covered by another step

    def as_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "goal": self.goal,
            "acceptance_criteria": list(self.acceptance_criteria),
            "expected_files": [f.as_json() for f in self.expected_files],
            "depends_on": list(self.depends_on),
            "related_items": [r.as_json() for r in self.related_items],
            "grouping_reason": self.grouping_reason,
            "discovery_note": self.discovery_note,
            "shared_criteria": [c.as_json() for c in self.shared_criteria],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Step":
        return cls(
            key=str(data["key"]),
            title=str(data["title"]),
            goal=str(data["goal"]),
            acceptance_criteria=tuple(str(c) for c in data.get("acceptance_criteria", [])),
            expected_files=tuple(
                ExpectedFile(str(f["path"]), str(f["source"]))
                for f in data.get("expected_files", [])
            ),
            depends_on=tuple(str(d) for d in data.get("depends_on", [])),
            related_items=tuple(
                RelatedItem(str(r["node_id"]), str(r["kind"]), str(r["label"]))
                for r in data.get("related_items", [])
            ),
            grouping_reason=str(data.get("grouping_reason", "") or ""),
            discovery_note=str(data.get("discovery_note", "") or ""),
            shared_criteria=tuple(
                CriterionReason.from_json(c) for c in data.get("shared_criteria", []) or []
            ),
        )


@dataclass(frozen=True)
class Candidates:
    """What a step may point at: known files and the task's affected graph items."""

    task_files: Sequence[str] = ()  # accepted earlier for this task
    dependency_files: Mapping[str, str] = field(default_factory=dict)  # path -> dependency key
    # Source and test files of the repository code map: path -> symbol names.
    repository_files: Mapping[str, Sequence[str]] = field(default_factory=dict)
    graph_items: Sequence[RelatedItem] = ()
    repository: str = "none"  # one of REPOSITORY_STATES
    source_roots: Sequence[str] = ()  # from the repository summary


@dataclass(frozen=True)
class Decomposition:
    steps: list[Step]
    manual: list[CriterionReason]
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Problem:
    code: str
    message: str
    step: str | None = None

    def as_json(self) -> dict[str, str | None]:
        return {"code": self.code, "step": self.step, "message": self.message}


class ChecklistInvalidError(Exception):
    def __init__(self, problems: Sequence[Problem]) -> None:
        self.problems = list(problems)
        super().__init__(
            "The checklist is invalid: " + "; ".join(p.message for p in self.problems[:10])
        )


def normalize(text: str) -> str:
    return " ".join(text.split())


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 4 and word.endswith("s") else word


def tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", text).lower())
    return {_stem(w) for w in words if len(w) >= 3 and w not in _STOPWORDS}


def is_manual(criterion: str) -> bool:
    return bool(_MANUAL.search(criterion))


def known_files(candidates: Candidates) -> dict[str, str]:
    """Every existing file a step may name: path -> source (the first of FILE_SOURCES)."""
    pool: dict[str, str] = {}
    for path in sorted(candidates.task_files):
        pool.setdefault(path, "task")
    for path in sorted(candidates.dependency_files):
        pool.setdefault(path, "dependency")
    for path in sorted(candidates.repository_files):
        pool.setdefault(path, "repository")
    return pool


def _title(criterion: str) -> str:
    text = normalize(criterion)
    if len(text) <= TITLE_CUT:
        return text
    cut = text[:TITLE_CUT].rsplit(" ", 1)[0] or text[:TITLE_CUT]
    return cut + " ..."


def _match_files(
    text: str, pool: Mapping[str, str], candidates: Candidates
) -> tuple[ExpectedFile, ...]:
    """Known files whose path (3 per term) or code-map symbols (2 per term) match the text."""
    wanted = tokens(text)
    if not wanted:
        return ()
    scored = []
    for path, source in pool.items():
        symbols = " ".join(candidates.repository_files.get(path, ())[:100])
        score = 3 * len(wanted & tokens(path)) + 2 * len(wanted & tokens(symbols))
        if score:
            scored.append((-score, FILE_SOURCES.index(source), path, source))
    scored.sort()
    return tuple(ExpectedFile(p, s) for _, _, p, s in scored[:GROUP_MAX_FILES])


def _match_items(text: str, items: Sequence[RelatedItem]) -> tuple[RelatedItem, ...]:
    wanted = tokens(text)
    scored = []
    for item in items:
        score = len(wanted & tokens(item.label))
        if score:
            scored.append((-score, item.kind, item.label, item.node_id, item))
    scored.sort(key=lambda s: s[:4])
    return tuple(s[4] for s in scored[:MAX_RELATED_PER_STEP])


@dataclass(frozen=True)
class _Facts:
    text: str
    terms: frozenset[str]
    files: tuple[ExpectedFile, ...]
    component: RelatedItem | None
    prerequisites: tuple[str, ...]


def _facts(text: str, pool: Mapping[str, str], candidates: Candidates) -> _Facts:
    files = _match_files(text, pool, candidates)
    components = [i for i in candidates.graph_items if i.kind == COMPONENT_KIND]
    matched = _match_items(text, components)
    return _Facts(
        text=text,
        terms=frozenset(tokens(text)),
        files=files,
        component=matched[0] if matched else None,
        prerequisites=tuple(
            sorted({candidates.dependency_files[f.path] for f in files if f.source == "dependency"})
        ),
    )


def _overlap(a: _Facts, b: _Facts) -> float:
    left, right = {f.path for f in a.files}, {f.path for f in b.files}
    return len(left & right) / len(left | right)


def _conflict(a: _Facts, b: _Facts) -> bool:
    if a.prerequisites != b.prerequisites:
        return True
    if a.component and b.component and a.component.node_id != b.component.node_id:
        return True
    return bool(a.files and b.files and _overlap(a, b) < FILE_OVERLAP)


def _signals(a: _Facts, b: _Facts) -> list[str]:
    """Why two compatible criteria belong together; empty when nothing links them."""
    out = []
    if a.component and b.component:
        out.append(f"the same component ({a.component.label})")
    if a.files and b.files:
        out.append("substantially the same files")
    if not out:
        shared = sorted(a.terms & b.terms)
        if shared:
            out.append(f"the same subject ({', '.join(shared[:3])})")
    return out


@dataclass
class _Group:
    members: list[_Facts]
    reasons: list[str]
    continues: int | None = None  # index of the full group this one continues


def _group(facts: Sequence[_Facts]) -> list[_Group]:
    groups: list[_Group] = []
    for fact in facts:
        placed = False
        overflow: int | None = None
        for n, group in enumerate(groups):
            if any(_conflict(fact, m) for m in group.members):
                continue
            reasons = [r for m in group.members for r in _signals(fact, m)]
            if not reasons:
                continue
            if len(group.members) >= GROUP_MAX_CRITERIA:
                overflow = n if overflow is None else overflow
                continue
            group.members.append(fact)
            group.reasons.extend(r for r in dict.fromkeys(reasons) if r not in group.reasons)
            placed = True
            break
        if not placed:
            groups.append(_Group([fact], [], overflow))
    return groups


def _grouping_reason(group: _Group, keys: Sequence[str]) -> str:
    if len(group.members) > 1:
        text = "Grouped: the criteria share " + "; ".join(group.reasons) + "."
    elif group.continues is not None:
        text = (
            f"Continues {keys[group.continues]}: a step covers at most "
            f"{GROUP_MAX_CRITERIA} criteria."
        )
    else:
        text = "One criterion: no other criterion shares its component, files and prerequisites."
    prerequisites = group.members[0].prerequisites
    if prerequisites:
        text += f" Builds on the accepted files of {', '.join(prerequisites)}."
    return text[:MAX_REASON_CHARS]


def discovery_note(candidates: Candidates) -> str:
    if candidates.repository == "mapped":
        roots = ", ".join(list(candidates.source_roots)[:4])
        where = f" (source roots: {roots})" if roots else ""
        return (
            "No file of the repository code map matches these criteria: file discovery will "
            f"happen during the run{where}."
        )[:MAX_NOTE_CHARS]
    if candidates.repository == "unavailable":
        return "The repository map is unavailable: file discovery will happen during the run."
    return "No repository is connected: file discovery will happen during the run."


def build_steps(
    task_key: str, task_title: str, criteria: Sequence[str], candidates: Candidates
) -> Decomposition:
    """The proposed checklist: deterministic cohesive steps in task-criterion order.

    A step depends on the nearest earlier step that expects one of its files; a step without
    expected files depends on the step before it. Nothing is added that the task does not say.
    """
    pool = known_files(candidates)
    texts = list(dict.fromkeys(c for c in criteria if c.strip()))
    manual = [CriterionReason(c, MANUAL_REASON) for c in texts if is_manual(c)]
    coding = [c for c in texts if not is_manual(c)]
    warnings: list[str] = []
    if candidates.repository != "mapped" and not pool:
        warnings.append(
            "No repository code map and no accepted files: expected files are left for "
            "discovery during the run."
        )
    if not coding:
        warnings.append(
            f"{task_key} has no acceptance criteria to code: one step without criteria is "
            "proposed; no criterion was invented."
        )
        title = normalize(task_title) or task_key
        files = _match_files(task_title, pool, candidates)
        return Decomposition(
            [
                Step(
                    key="S-01",
                    title=_title(f"Implement {title}"),
                    goal=f"Implement {task_key} as described in the plan task.",
                    acceptance_criteria=(),
                    expected_files=files,
                    related_items=_match_items(task_title, candidates.graph_items),
                    grouping_reason="The task has no acceptance criteria to code.",
                    discovery_note="" if files else discovery_note(candidates),
                )
            ],
            manual,
            warnings,
        )
    groups = _group([_facts(c, pool, candidates) for c in coding])
    if len(groups) > MAX_STEPS:
        size = math.ceil(len(groups) / MAX_STEPS)
        merged = [groups[i : i + size] for i in range(0, len(groups), size)]
        groups = [
            _Group(
                [m for g in chunk for m in g.members],
                ["neighbouring places in the task order (merged to stay within the step limit)"],
            )
            for chunk in merged
        ]
        warnings.append(
            f"{task_key} needs more than {MAX_STEPS} cohesive steps: neighbouring groups were "
            "merged."
        )
    keys = [f"S-{n:02d}" for n in range(1, len(groups) + 1)]
    steps: list[Step] = []
    for key, group in zip(keys, groups, strict=True):
        members = [m.text for m in group.members]
        text = " ".join(members)
        files = _match_files(text, pool, candidates)
        paths = {f.path for f in files}
        depends: tuple[str, ...] = ()
        earlier = next(
            (s for s in reversed(steps) if paths & {f.path for f in s.expected_files}), None
        )
        if earlier is not None:
            depends = (earlier.key,)
        elif not files and steps:
            depends = (steps[-1].key,)
        listed = [normalize(c) for c in members]
        noun = "this acceptance criterion" if len(listed) == 1 else "these acceptance criteria"
        title = _title(listed[0])
        if len(listed) > 1:
            title = f"{title} (+{len(listed) - 1} more)"
        steps.append(
            Step(
                key=key,
                title=title,
                goal=(f"Change the code so that {noun} of {task_key} hold: " + " / ".join(listed))[
                    :MAX_GOAL_CHARS
                ],
                acceptance_criteria=tuple(members),
                expected_files=files,
                depends_on=depends,
                related_items=_match_items(text, candidates.graph_items),
                grouping_reason=_grouping_reason(group, keys),
                discovery_note="" if files else discovery_note(candidates),
            )
        )
    return Decomposition(steps, manual, warnings)


def step_warnings(step: Step) -> list[str]:
    """Quality warnings of one step; the step is still valid."""
    out = []
    if len(step.expected_files) > QUALITY_MAX_FILES:
        out.append(
            f"{step.key} expects {len(step.expected_files)} files (more than "
            f"{QUALITY_MAX_FILES}); consider splitting it."
        )
    if len(step.acceptance_criteria) > QUALITY_MAX_CRITERIA:
        out.append(
            f"{step.key} covers {len(step.acceptance_criteria)} criteria (more than "
            f"{QUALITY_MAX_CRITERIA}); consider splitting it."
        )
    if not step.goal.strip():
        out.append(f"{step.key} has no goal.")
    if not step.expected_files and not step.discovery_note.strip():
        out.append(f"{step.key} has neither expected files nor a file-discovery note.")
    return out


def _cycle(graph: dict[str, list[str]], left: set[str]) -> list[str]:
    for key in sorted(left):
        for dep in sorted(graph[key]):
            if dep in left:
                path = find_path(graph, dep, key)
                if path is not None:
                    return [key, *path]
    return sorted(left)  # unreachable for a real cycle


def order_steps(steps: Sequence[Step]) -> list[Step]:
    """Dependencies first, otherwise the given position. Raises on a cycle."""
    by_key = {s.key: s for s in steps}
    position = {s.key: n for n, s in enumerate(steps)}
    remaining = {s.key: {d for d in s.depends_on if d in by_key} for s in steps}
    ready = [(position[k], k) for k, deps in remaining.items() if not deps]
    heapq.heapify(ready)
    out: list[Step] = []
    while ready:
        _, key = heapq.heappop(ready)
        out.append(by_key[key])
        for other, deps in remaining.items():
            if key in deps:
                deps.discard(key)
                if not deps:
                    heapq.heappush(ready, (position[other], other))
    if len(out) != len(steps):
        left = {k for k in by_key if k not in {s.key for s in out}}
        cycle = _cycle({s.key: list(s.depends_on) for s in steps}, left)
        raise ChecklistInvalidError(
            [Problem("dependency_cycle", "Dependency cycle: " + " -> ".join(cycle), cycle[0])]
        )
    return out


def _text(value: Any, limit: int) -> str:
    return normalize(str(value or ""))[:limit]


def _criterion_reasons(raw: Any) -> list[tuple[str, str]]:
    out = []
    for entry in raw or []:
        if isinstance(entry, Mapping):
            out.append((str(entry.get("criterion", "")), _text(entry.get("reason"), 1000)))
    return out


def validate_checklist(
    raw: Sequence[Mapping[str, Any]],
    raw_manual: Sequence[Mapping[str, Any]],
    criteria: Sequence[str],
    previous: Sequence[Step] = (),
    known: Mapping[str, str] | None = None,
) -> Decomposition:
    """Check a revised checklist (from a person or the agent); steps come back in dependency order.

    `previous` supplies the related graph items of steps that keep their key; they are not
    editable. `known` (path -> source) is every existing file; when given, a file that claims to
    exist must be one of them. Raises ChecklistInvalidError with every problem found.
    """
    problems: list[Problem] = []
    allowed = {normalize(c): c for c in criteria if c.strip()}

    def verbatim(criterion: Any, where: str, key: str | None) -> str | None:
        found = allowed.get(normalize(str(criterion)))
        if found is None:
            problems.append(
                Problem(
                    "invented_criterion",
                    f"{where}: {normalize(str(criterion))[:80]!r} is not an acceptance "
                    "criterion of the task",
                    key,
                )
            )
        return found

    if not raw:
        problems.append(Problem("no_steps", "A checklist needs at least one step"))
    if len(raw) > MAX_STEPS:
        problems.append(Problem("too_many_steps", f"A checklist has at most {MAX_STEPS} steps"))
    keys = [str(s.get("key", "")) for s in raw]
    seen: set[str] = set()
    for key in keys:
        if not STEP_KEY.match(key):
            problems.append(Problem("invalid_key", f"Step key {key[:20]!r} is not S-NN", key[:20]))
        elif key in seen:
            problems.append(Problem("duplicate_key", f"Step key {key} is used twice", key))
        seen.add(key)
    related = {s.key: s.related_items for s in previous}
    steps: list[Step] = []
    for item, key in zip(raw, keys, strict=True):
        title = normalize(str(item.get("title", "")))
        goal = normalize(str(item.get("goal", "") or ""))
        if not title or len(title) > MAX_TITLE_CHARS:
            problems.append(
                Problem("invalid_title", f"{key}: title needs 1-{MAX_TITLE_CHARS} chars", key)
            )
        if len(goal) > MAX_GOAL_CHARS:
            problems.append(
                Problem("invalid_goal", f"{key}: goal has at most {MAX_GOAL_CHARS} chars", key)
            )
        texts: list[str] = []
        for criterion in item.get("acceptance_criteria", []) or []:
            found = verbatim(criterion, key, key)
            if found is not None and found not in texts:
                texts.append(found)
        shared: list[CriterionReason] = []
        for criterion, reason in _criterion_reasons(item.get("shared_criteria")):
            found = allowed.get(normalize(criterion))
            if found is None or found not in texts or not reason:
                problems.append(
                    Problem(
                        "invalid_shared_criterion",
                        f"{key}: a shared criterion must be one of the step's criteria and "
                        "have a reason",
                        key,
                    )
                )
            elif all(c.criterion != found for c in shared):
                shared.append(CriterionReason(found, reason))
        files: list[ExpectedFile] = []
        for entry in item.get("expected_files", []) or []:
            path = str(entry.get("path", "")) if isinstance(entry, Mapping) else str(entry)
            source = str(entry.get("source", "new")) if isinstance(entry, Mapping) else "new"
            problem = path_problem(path)
            if problem:
                problems.append(Problem(problem, f"{key}: expected file {path[:80]!r}", key))
                continue
            if source not in FILE_SOURCES:
                source = "new"
            if known is not None:
                if source in EXISTING_SOURCES and path not in known:
                    problems.append(
                        Problem(
                            "unknown_path",
                            f"{key}: {path[:80]!r} is not an existing file; mark a file the "
                            "step creates as new",
                            key,
                        )
                    )
                    continue
                if source == "new" and path in known:
                    source = known[path]
            if all(f.path != path for f in files):
                files.append(ExpectedFile(path, source))
        if len(files) > MAX_FILES_PER_STEP:
            problems.append(
                Problem("too_many_files", f"{key}: at most {MAX_FILES_PER_STEP} files", key)
            )
        depends: list[str] = []
        for dep in item.get("depends_on", []) or []:
            dep = str(dep)
            if dep == key:
                problems.append(Problem("self_dependency", f"{key} depends on itself", key))
            elif dep not in keys:
                problems.append(
                    Problem("unknown_dependency", f"{key} depends on unknown step {dep[:20]}", key)
                )
            elif dep not in depends:
                depends.append(dep)
        steps.append(
            Step(
                key=key,
                title=title,
                goal=goal,
                acceptance_criteria=tuple(texts),
                expected_files=tuple(files),
                depends_on=tuple(sorted(depends)),
                related_items=related.get(key, ()),
                grouping_reason=_text(item.get("grouping_reason"), MAX_REASON_CHARS),
                discovery_note=_text(item.get("discovery_note"), MAX_NOTE_CHARS),
                shared_criteria=tuple(shared),
            )
        )
    in_steps: dict[str, list[Step]] = {}
    for step in steps:
        for criterion in step.acceptance_criteria:
            in_steps.setdefault(criterion, []).append(step)
    for criterion, holders in in_steps.items():
        explained = [s for s in holders if any(c.criterion == criterion for c in s.shared_criteria)]
        if len(explained) < len(holders) - 1:
            problems.append(
                Problem(
                    "repeated_criterion",
                    f"{normalize(criterion)[:80]!r} is in {', '.join(s.key for s in holders)}; "
                    "every repeat needs a reason in shared_criteria",
                    holders[-1].key,
                )
            )
    manual: list[CriterionReason] = []
    for criterion, reason in _criterion_reasons(raw_manual):
        found = verbatim(criterion, "manual verification", None)
        if found is None:
            continue
        if not reason:
            problems.append(
                Problem(
                    "manual_reason_missing",
                    f"Manual verification of {normalize(found)[:80]!r} needs a reason",
                )
            )
        elif found in in_steps:
            problems.append(
                Problem(
                    "manual_and_step",
                    f"{normalize(found)[:80]!r} is both in a coding step and marked manual",
                )
            )
        elif all(m.criterion != found for m in manual):
            manual.append(CriterionReason(found, reason))
    covered = {normalize(c) for c in in_steps} | {normalize(m.criterion) for m in manual}
    for text in allowed:
        if text not in covered:
            problems.append(
                Problem(
                    "uncovered_criterion",
                    f"Acceptance criterion {text[:80]!r} is neither in a step nor marked for "
                    "manual verification",
                )
            )
    if problems:
        raise ChecklistInvalidError(problems)
    return Decomposition(order_steps(steps), manual)
