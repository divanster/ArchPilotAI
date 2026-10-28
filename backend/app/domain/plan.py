"""Implementation plan derived from an approved architecture. Pure and deterministic: no LLM.

One task per component and one per decision in `integrations`, `security_architecture` and
`non_functional_design`. Nothing is invented: requirement links come from stored requirement ids
and the coverage matrix, acceptance criteria are the coverage `verification` texts, and
dependencies come from diagram edges (and the graph's `depends_on` edges, which are derived from
them). An edge `source -> target` means the source component depends on the target, so the
source's task comes after the target's. A dependency cycle fails the build; no order is guessed.
"""

import heapq
import uuid
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field

from app.domain.architecture import (
    ArchitectureContent,
    CoverageEntry,
    CoverageStatus,
    Decision,
    DecisionOrigin,
    NodeType,
)

AI_RECOMMENDATION_FLAG = "AI recommendation: confirm technology before implementing"
CLARIFICATION_FLAG = "Origin requires clarification before implementing"
NO_COVERAGE_FLAG = "No coverage matrix recorded: acceptance criteria are unavailable"
NEEDS_CLARIFICATION = "needs clarification"

PLAN_STATUSES = ("draft", "approved", "rejected")
TASK_KINDS = ("setup", "component", "integration", "security", "data", "test", "nfr")

_DATA_TYPES = frozenset({NodeType.DATABASE, NodeType.STORAGE})
# Tie-break among tasks that are ready at the same time: components first, then decisions.
_GROUPS: tuple[tuple[str, str, str], ...] = (
    ("integrations", "integration", "Integrate"),
    ("security_architecture", "security", "Secure"),
    ("non_functional_design", "nfr", "NFR"),
)


class PlanCycleError(Exception):
    def __init__(self, cycle: list[str]) -> None:
        self.cycle = cycle
        super().__init__(
            "The architecture has a dependency cycle: "
            + " -> ".join(cycle)
            + ". No order can be derived; fix the diagram and generate a new architecture version."
        )


@dataclass(frozen=True)
class PlanTaskSpec:
    key: str
    order_index: int
    title: str
    kind: str
    source_ref: str
    component_ref: str | None
    description: str
    requirement_ids: list[uuid.UUID]
    acceptance_criteria: list[str]
    depends_on: list[str]
    flags: list[str]
    blocking_questions: list[str]


@dataclass(frozen=True)
class PlanSpec:
    tasks: list[PlanTaskSpec]
    # Open questions that name no requirement shared with any task, so they block none.
    unattached_questions: list[str]


@dataclass
class _Draft:
    rank: tuple[int, int]
    source_ref: str
    title: str
    kind: str
    description: str
    element_id: str | None
    component_ref: str | None
    own_requirements: set[uuid.UUID]
    origin: DecisionOrigin | None = None
    requirement_ids: list[uuid.UUID] = field(default_factory=list)


def criterion(entry: CoverageEntry) -> str:
    if entry.status is CoverageStatus.ADDRESSED:
        return entry.verification
    return f"{entry.verification} ({NEEDS_CLARIFICATION}; gap: {entry.gap})"


def _drafts(content: ArchitectureContent) -> list[_Draft]:
    drafts: list[_Draft] = []
    for i, comp in enumerate(content.components):
        drafts.append(
            _Draft(
                rank=(0, i),
                source_ref=f"components[{i}]",
                title=f"Implement {comp.name}",
                kind="data" if comp.type in _DATA_TYPES else "component",
                description=comp.responsibility,
                element_id=comp.id,
                component_ref=comp.id,
                own_requirements=set(comp.requirement_ids),
            )
        )
    for rank, (group, kind, verb) in enumerate(_GROUPS, start=1):
        decisions: list[Decision] = getattr(content, group)
        for i, decision in enumerate(decisions):
            drafts.append(
                _Draft(
                    rank=(rank, i),
                    source_ref=f"{group}[{i}]",
                    title=f"{verb}: {decision.name}",
                    kind=kind,
                    description=decision.decision,
                    element_id=decision.id,
                    component_ref=None,
                    own_requirements=set(decision.supporting_requirement_ids),
                    origin=decision.origin,
                )
            )
    return drafts


def _order(drafts: list[_Draft], deps: dict[int, set[int]]) -> list[int]:
    """Kahn's algorithm; the ready task with the smallest (group, index) goes first."""
    remaining = {i: set(d) for i, d in deps.items()}
    dependents: dict[int, list[int]] = {i: [] for i in deps}
    for i, targets in deps.items():
        for t in targets:
            dependents[t].append(i)
    ready = [(drafts[i].rank, i) for i, d in remaining.items() if not d]
    heapq.heapify(ready)
    order: list[int] = []
    while ready:
        _, i = heapq.heappop(ready)
        order.append(i)
        for dep in dependents[i]:
            remaining[dep].discard(i)
            if not remaining[dep]:
                heapq.heappush(ready, (drafts[dep].rank, dep))
    if len(order) != len(drafts):
        stuck = {i for i, d in remaining.items() if d}
        raise PlanCycleError(_find_cycle(drafts, deps, stuck))
    return order


def _find_cycle(drafts: list[_Draft], deps: dict[int, set[int]], stuck: set[int]) -> list[str]:
    """A deterministic cycle among the tasks that could not be ordered."""

    def label(i: int) -> str:
        return drafts[i].element_id or drafts[i].source_ref

    state: dict[int, int] = {}  # 1 = on the current path, 2 = finished
    path: list[int] = []

    def visit(i: int) -> list[int] | None:
        state[i] = 1
        path.append(i)
        for t in sorted(deps[i] & stuck, key=lambda n: drafts[n].rank):
            if state.get(t) == 1:
                return [*path[path.index(t) :], t]
            if t not in state:
                found = visit(t)
                if found:
                    return found
        path.pop()
        state[i] = 2
        return None

    for start in sorted(stuck, key=lambda n: drafts[n].rank):
        if start not in state:
            found = visit(start)
            if found:
                return [label(i) for i in found]
    return []  # unreachable: leftover tasks always contain a cycle


def build_plan(
    content: ArchitectureContent,
    confirmed: Collection[uuid.UUID],
    graph_dependencies: Iterable[tuple[str, str]] = (),
) -> PlanSpec:
    """`graph_dependencies` are (source, target) `components[i]` paths of the graph's depends_on
    edges. `confirmed` are the currently confirmed requirement ids; others are never linked."""
    drafts = _drafts(content)
    by_ref = {d.source_ref: i for i, d in enumerate(drafts)}
    by_component = {d.component_ref: i for i, d in enumerate(drafts) if d.component_ref}
    confirmed_set = set(confirmed)

    coverage: dict[uuid.UUID, CoverageEntry] = {}
    linked: dict[str, set[uuid.UUID]] = {}
    for entry in content.coverage_matrix or []:
        coverage.setdefault(entry.requirement_id, entry)
        for related in entry.related_ids:
            linked.setdefault(related, set()).add(entry.requirement_id)

    deps: dict[int, set[int]] = {i: set() for i in range(len(drafts))}
    for edge in content.diagram.edges:
        src, tgt = by_component.get(edge.source), by_component.get(edge.target)
        if src is not None and tgt is not None and src != tgt:
            deps[src].add(tgt)
    for source_ref, target_ref in graph_dependencies:
        src, tgt = by_ref.get(source_ref), by_ref.get(target_ref)
        if src is not None and tgt is not None and src != tgt:
            deps[src].add(tgt)

    order = _order(drafts, deps)
    keys = {i: f"T-{n:03d}" for n, i in enumerate(order, start=1)}

    skipped: dict[int, int] = {}
    for i, draft in enumerate(drafts):
        wanted = draft.own_requirements | linked.get(draft.element_id or "", set())
        draft.requirement_ids = sorted(wanted & confirmed_set, key=str)
        skipped[i] = len(wanted - confirmed_set)

    blocking: dict[int, list[str]] = {i: [] for i in range(len(drafts))}
    unattached: list[str] = []
    for question in content.open_questions:
        text = question.question
        if question.why_it_matters:
            text = f"{text} — {question.why_it_matters}"
        affected = [
            i
            for i, d in enumerate(drafts)
            if set(question.requirement_ids) & set(d.requirement_ids)
        ]
        for i in affected:
            blocking[i].append(text)
        if not affected:
            unattached.append(text)

    tasks: list[PlanTaskSpec] = []
    for position, i in enumerate(order, start=1):
        draft = drafts[i]
        flags: list[str] = []
        if draft.origin is DecisionOrigin.RECOMMENDED_BY_AI:
            flags.append(AI_RECOMMENDATION_FLAG)
        if draft.origin is DecisionOrigin.REQUIRES_CLARIFICATION:
            flags.append(CLARIFICATION_FLAG)
        if content.coverage_matrix is None:
            flags.append(NO_COVERAGE_FLAG)
        if skipped[i]:
            flags.append(f"{skipped[i]} linked requirement(s) are no longer confirmed")
        criteria: list[str] = []
        for rid in draft.requirement_ids:
            covered = coverage.get(rid)
            if covered is not None and criterion(covered) not in criteria:
                criteria.append(criterion(covered))
        tasks.append(
            PlanTaskSpec(
                key=keys[i],
                order_index=position,
                title=draft.title,
                kind=draft.kind,
                source_ref=draft.source_ref,
                component_ref=draft.component_ref,
                description=draft.description,
                requirement_ids=draft.requirement_ids,
                acceptance_criteria=criteria,
                depends_on=sorted(keys[t] for t in deps[i]),
                flags=flags,
                blocking_questions=blocking[i],
            )
        )
    return PlanSpec(tasks, unattached)
