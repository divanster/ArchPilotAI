import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.domain.architecture import ArchitectureContent, safe_finish_reason
from app.domain.coverage import (
    CoverageItem,
    check_coverage,
    removed_line,
    removed_symbols,
    scope_lines,
)
from app.domain.feedback import build_feedback
from app.domain.frameworks import FrameworkRules
from app.domain.patch import (
    PATCH_PROMPT_VERSION,
    RESPONSE_CHARS_PER_TOKEN,
    Failure,
    GeneratedPatch,
    PatchInvalidError,
    PatchLimits,
    content_hash,
    parse_patch,
    requirement_code,
    stale_failures,
    task_status,
    unified_diff,
    validate_patch,
)
from app.domain.plan import AI_RECOMMENDATION_FLAG
from app.llm.contracts import LLMProvider, LLMRequest
from app.llm.patch_prompt import SYSTEM_PROMPT, build_prompt
from app.models.patch import CodePatch, PatchFile, TechnologyConfirmation, WorkspaceFile
from app.models.plan import ImplementationPlan, PlanTask
from app.repositories.architecture import ArchitectureRepository
from app.repositories.patch import PatchRepository
from app.repositories.plan import PlanRepository
from app.repositories.project import ProjectRepository
from app.repositories.requirement import RequirementRepository
from app.services.decision import DecisionService
from app.services.plan import PlanNotFoundError
from app.services.project import ProjectNotFoundError

logger = logging.getLogger(__name__)

# One generation at a time per process: bounds provider cost and keeps version numbers dense.
_generation_lock = threading.Lock()

MAX_REQUIREMENTS = 20
MAX_RELATED_ITEMS = 8
MAX_WORKSPACE_PATHS = 200
MAX_CONTEXT_FILES = 6
MAX_FILE_CHARS = 8_000
MAX_FIELD_CHARS = 600
_GENERIC_WORDS = frozenset({"implement", "integrate", "secure", "service", "module", "with"})
_SOURCE_REF = re.compile(r"^(\w+)\[(\d+)\]$")


class TaskNotFoundError(Exception):
    def __init__(self, key: str) -> None:
        super().__init__(f"Task {key} not found in this plan")


class PatchNotFoundError(Exception):
    def __init__(self, patch_id: uuid.UUID | str) -> None:
        super().__init__(f"Patch {patch_id} not found")


class PatchGateError(Exception):
    """A precondition for generation is not met (HTTP 409). `code` is a fixed label."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class PatchBusyError(Exception):
    pass


class PatchContextTooLargeError(Exception):
    pass


class PatchNotProposedError(Exception):
    pass


class StalePatchError(Exception):
    def __init__(self, failures: Sequence[Failure]) -> None:
        self.failures = list(failures)
        super().__init__(
            "The workspace changed since this patch was proposed; regenerate the patch. "
            + ", ".join(sorted({f.code for f in failures}))
        )


class ReviewInputError(Exception):
    pass


@dataclass(frozen=True)
class FileView:
    file: PatchFile
    diff: str
    diff_truncated: bool


@dataclass(frozen=True)
class PatchDetail:
    patch: CodePatch
    files: Sequence[PatchFile]
    views: Sequence[FileView] = field(default_factory=tuple)
    # For a proposed patch: why it can no longer be accepted against the current workspace.
    stale: Sequence[Failure] = field(default_factory=tuple)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _strip(value: Any, codes: dict[str, str]) -> Any:
    """An architecture item for the prompt: requirement UUIDs become codes, passages are dropped."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key == "source_passage_ids":
                continue
            if key in ("requirement_ids", "supporting_requirement_ids"):
                out[key] = sorted(codes.get(str(r), "?") for r in item)
            else:
                out[key] = _strip(item, codes)
        return out
    if isinstance(value, list):
        return [_strip(v, codes) for v in value]
    if isinstance(value, str):
        return _cut(value, MAX_FIELD_CHARS)
    return value


class PatchService:
    def __init__(
        self,
        provider: LLMProvider,
        projects: ProjectRepository,
        plans: PlanRepository,
        architecture: ArchitectureRepository,
        requirements: RequirementRepository,
        patches: PatchRepository,
        *,
        limits: PatchLimits,
        max_output_tokens: int,
        max_context_chars: int,
        decisions: DecisionService | None = None,
    ) -> None:
        self._provider = provider
        self._decisions = decisions
        self._projects = projects
        self._plans = plans
        self._architecture = architecture
        self._requirements = requirements
        self._patches = patches
        self._limits = limits
        self._max_output_tokens = max_output_tokens
        self._max_context_chars = max_context_chars

    # ---- lookups ---------------------------------------------------------------------------

    def _plan_and_task(
        self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str
    ) -> tuple[ImplementationPlan, PlanTask, Sequence[PlanTask]]:
        if self._projects.get(project_id) is None:
            raise ProjectNotFoundError(project_id)
        plan = self._plans.get(project_id, plan_id)
        if plan is None:
            raise PlanNotFoundError(plan_id)
        tasks = self._plans.tasks(project_id, plan_id)
        task = next((t for t in tasks if t.key == task_key), None)
        if task is None:
            raise TaskNotFoundError(task_key)
        return plan, task, tasks

    def task_statuses(self, project_id: uuid.UUID, plan_id: uuid.UUID) -> dict[uuid.UUID, str]:
        return {
            task_id: task_status(statuses)
            for task_id, statuses in self._patches.statuses_by_task(project_id, plan_id).items()
        }

    # ---- technology confirmation -----------------------------------------------------------

    def confirm_technology(
        self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str, name: str, reason: str
    ) -> TechnologyConfirmation:
        _, task, _ = self._plan_and_task(project_id, plan_id, task_key)
        if not name.strip() or not reason.strip():
            raise ReviewInputError("A name and a reason are required")
        if AI_RECOMMENDATION_FLAG not in task.flags:
            raise PatchGateError(
                "technology_not_flagged",
                f"{task.key} has no AI technology recommendation to confirm",
            )
        return self._patches.add_confirmation(
            TechnologyConfirmation(
                id=uuid.uuid4(),
                plan_id=plan_id,
                project_id=project_id,
                task_key=task.key,
                name=name.strip(),
                reason=reason.strip(),
            )
        )

    def confirmations(
        self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str
    ) -> Sequence[TechnologyConfirmation]:
        self._plan_and_task(project_id, plan_id, task_key)
        return self._patches.confirmations(project_id, plan_id, task_key)

    # ---- generation ------------------------------------------------------------------------

    def check_gates(
        self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str, acknowledge: bool
    ) -> tuple[ImplementationPlan, PlanTask, Sequence[PlanTask]]:
        plan, task, tasks = self._plan_and_task(project_id, plan_id, task_key)
        if plan.status != "approved":
            raise PatchGateError(
                "plan_not_approved",
                f"The plan is {plan.status}; only an approved plan can have code generated",
            )
        if (
            AI_RECOMMENDATION_FLAG in task.flags
            and not self._patches.confirmations(project_id, plan_id, task.key)
            and not (
                self._decisions and self._decisions.decision_for_task(project_id, task) is not None
            )
        ):
            raise PatchGateError(
                "technology_unconfirmed",
                f"{task.key} uses an AI-recommended technology; a person must confirm it first "
                "(confirm the task or record a technology decision for its area)",
            )
        if task.blocking_questions and not acknowledge:
            raise PatchGateError(
                "blocking_questions",
                f"{task.key} has {len(task.blocking_questions)} unresolved blocking question(s); "
                "resolve them or acknowledge them explicitly for this generation",
            )
        return plan, task, tasks

    def decision_context(
        self, project_id: uuid.UUID, plan: ImplementationPlan, task: PlanTask
    ) -> tuple[dict[str, Any], list[str]]:
        if self._decisions is None:
            return {}, list(task.flags)
        return self._decisions.task_context(project_id, plan.proposal_id, task)

    def feedback_context(
        self, project_id: uuid.UUID, task: PlanTask
    ) -> tuple[dict[str, Any], int | None]:
        """The labelled reviewer feedback of the task's latest patch, if it was rejected."""
        patches = self._patches.for_task(project_id, task.id)
        latest = patches[0] if patches else None
        if latest is None or latest.status != "rejected" or not latest.review_reason:
            return {}, None
        codes = {
            code: (r.title, r.description)
            for r in self._requirements.list(project_id, "confirmed")
            if (code := requirement_code(r.source_quote))
        }
        files = [(f.path, f.change) for f in self._patches.files(project_id, latest.id)]
        block = build_feedback(
            version=latest.version,
            reason=latest.review_reason,
            files=files,
            notes=list(latest.notes),
            requirements=codes,
        )
        return block, latest.version

    def frameworks(self, project_id: uuid.UUID, paths: Iterable[str]) -> FrameworkRules:
        current = self._decisions.current(project_id) if self._decisions else []
        return FrameworkRules.detect(
            [d.choice for d in current if d.area == "backend"],
            [f"{d.choice} {d.reason}" for d in current],
            paths,
        )

    def coverage(
        self, project_id: uuid.UUID, task: PlanTask, files: Mapping[str, str]
    ) -> dict[str, Any]:
        """Covered/missing per acceptance criterion and linked requirement (domain.coverage)."""
        confirmed = {str(r.id): r for r in self._requirements.list(project_id, "confirmed")}
        items = [
            CoverageItem("criterion", str(n), text)
            for n, text in enumerate(task.acceptance_criteria, start=1)
        ]
        for rid in task.requirement_ids:
            req = confirmed.get(str(rid))
            if req is not None:
                ref = requirement_code(req.source_quote) or _cut(req.title, 60)
                items.append(CoverageItem("requirement", ref, f"{req.title}. {req.description}"))
        return check_coverage(items, files)

    def regression_and_scope(
        self,
        project_id: uuid.UUID,
        plan: ImplementationPlan,
        task: PlanTask,
        merged: Mapping[str, str],
    ) -> tuple[tuple[int, list[str], str] | None, list[str]]:
        """ "Removed since vN" against the previous version of this task, and scope notes.

        Neither blocks anything; both end up in the patch notes and the coverage report.
        """
        removed: tuple[int, list[str], str] | None = None
        previous = self._patches.for_task(project_id, task.id)
        if previous:
            before = {
                f.path: f.content
                for f in self._patches.files(project_id, previous[0].id)
                if f.change != "delete"
            }
            gone = removed_symbols(before, merged)
            if gone:
                names = [f"{kind} {name}" for kind, name in gone]
                removed = (previous[0].version, names, removed_line(previous[0].version, gone))
        confirmed = {str(r.id): r for r in self._requirements.list(project_id, "confirmed")}
        linked: dict[str, list[str]] = {}
        for other in self._plans.tasks(project_id, plan.id):
            if other.id == task.id:
                continue
            for rid in other.requirement_ids:
                if str(rid) not in {str(x) for x in task.requirement_ids}:
                    linked.setdefault(str(rid), []).append(other.key)
        others = [
            (
                requirement_code(confirmed[rid].source_quote) or _cut(confirmed[rid].title, 60),
                f"{confirmed[rid].title}. {confirmed[rid].description}",
                sorted(keys),
            )
            for rid, keys in linked.items()
            if rid in confirmed
        ]
        text = " ".join([task.title, task.description or "", *task.acceptance_criteria])
        return removed, scope_lines(text, others)

    def dependency_warnings(
        self,
        project_id: uuid.UUID,
        plan: ImplementationPlan,
        task: PlanTask,
        tasks: Sequence[PlanTask],
    ) -> list[str]:
        statuses = self._patches.statuses_by_task(project_id, plan.id)
        return [
            f"Dependency {dep.key} has no accepted patch yet; its code is not in the workspace"
            for dep in tasks
            if dep.key in task.depends_on and "accepted" not in statuses.get(dep.id, set())
        ]

    def propose(
        self,
        project_id: uuid.UUID,
        plan: ImplementationPlan,
        task: PlanTask,
        generated: GeneratedPatch,
        *,
        provider: str,
        model: str,
        prompt_version: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        warnings: list[str],
        acknowledged: bool,
        based_on_version: int | None = None,
        affected: dict[str, Any] | None = None,
    ) -> PatchDetail:
        """Validate a generated change set against the workspace and save it as a proposed patch."""
        workspace = {f.path: f for f in self._patches.workspace(project_id)}
        hashes = {path: f.content_hash for path, f in workspace.items()}
        rules = self.frameworks(project_id, [*workspace, *(f.path for f in generated.files)])
        refused = [f.path for f in generated.files if rules.refuses(f.path)]
        if refused:
            generated = generated.model_copy(
                update={
                    "files": [f for f in generated.files if f.path not in refused],
                    "notes": [*generated.notes, rules.note()],
                }
            )
        failures = validate_patch(generated.files, hashes, self._limits)
        if failures:
            # Only fixed codes and paths are logged, never file content.
            logger.warning(
                "patch rejected project=%s task=%s failures=%s",
                project_id,
                task.key,
                json.dumps([f.as_json() for f in failures[:50]], sort_keys=True),
            )
            raise PatchInvalidError(failures)
        merged = {path: f.content for path, f in workspace.items()}
        for changed in generated.files:
            if changed.change == "delete":
                merged.pop(changed.path, None)
            else:
                merged[changed.path] = changed.content
        report = self.coverage(project_id, task, merged)
        removed_note, scope = self.regression_and_scope(project_id, plan, task, merged)
        extra_notes: list[str] = []
        if affected is not None:
            report["affected"] = affected
        if removed_note:
            report["removed"] = {"since": removed_note[0], "items": removed_note[1]}
            extra_notes.append(removed_note[2])
        if scope:
            report["scope"] = scope
            extra_notes += scope
        patch = CodePatch(
            id=uuid.uuid4(),
            project_id=project_id,
            plan_id=plan.id,
            task_id=task.id,
            task_key=task.key,
            version=self._patches.next_version(project_id, task.id),
            status="proposed",
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            notes=[*generated.notes, *extra_notes],
            assumptions=list(generated.assumptions),
            test_suggestions=list(generated.test_suggestions),
            warnings=warnings,
            blocking_acknowledged=acknowledged and bool(task.blocking_questions),
            based_on_version=based_on_version,
            coverage=report,
        )
        files = [
            PatchFile(
                id=uuid.uuid4(),
                patch_id=patch.id,
                project_id=project_id,
                path=f.path,
                change=f.change,
                content="" if f.change == "delete" else f.content,
                previous_content_hash=hashes.get(f.path) if f.change != "create" else None,
                previous_content=workspace[f.path].content
                if f.change != "create" and f.path in workspace
                else None,
            )
            for f in generated.files
        ]
        saved = self._patches.add(patch, files)
        logger.info(
            "patch saved project=%s task=%s version=%d files=%d",
            project_id,
            task.key,
            saved.version,
            len(files),
        )
        return self.detail(project_id, saved.id)

    def generate(
        self,
        project_id: uuid.UUID,
        plan_id: uuid.UUID,
        task_key: str,
        *,
        acknowledge_blocking_questions: bool = False,
    ) -> PatchDetail:
        plan, task, tasks = self.check_gates(
            project_id, plan_id, task_key, acknowledge_blocking_questions
        )
        if not _generation_lock.acquire(blocking=False):
            raise PatchBusyError("A patch generation is already running")
        try:
            return self._generate(
                project_id, plan, task, tasks, acknowledged=acknowledge_blocking_questions
            )
        finally:
            _generation_lock.release()

    def _generate(
        self,
        project_id: uuid.UUID,
        plan: ImplementationPlan,
        task: PlanTask,
        tasks: Sequence[PlanTask],
        *,
        acknowledged: bool,
    ) -> PatchDetail:
        warnings = self.dependency_warnings(project_id, plan, task, tasks)
        workspace = {f.path: f for f in self._patches.workspace(project_id)}
        feedback, based_on = self.feedback_context(project_id, task)
        context = self._build_context(
            project_id, plan, task, tasks, workspace, acknowledged, warnings, feedback
        )
        nonce = uuid.uuid4().hex[:16]
        prompt = build_prompt(context, nonce)
        if len(prompt) > self._max_context_chars + 6_000:
            raise PatchContextTooLargeError("The task context exceeds the size limit")
        started = time.perf_counter()
        response = self._provider.generate(
            LLMRequest(
                prompt=prompt,
                system_prompt=SYSTEM_PROMPT,
                max_output_tokens=self._max_output_tokens,
                temperature=0,
            )
        )
        finish = safe_finish_reason(response.finish_reason)
        logger.info(
            "patch response project=%s task=%s latency_ms=%d finish_reason=%s",
            project_id,
            task.key,
            int((time.perf_counter() - started) * 1000),
            finish,
        )
        generated = parse_patch(
            response.text,
            finish_reason=finish,
            max_chars=self._max_output_tokens * RESPONSE_CHARS_PER_TOKEN,
        )
        return self.propose(
            project_id,
            plan,
            task,
            generated,
            provider=response.provider,
            model=response.model,
            prompt_version=PATCH_PROMPT_VERSION,
            input_tokens=response.usage.input_tokens or 0,
            output_tokens=response.usage.output_tokens or 0,
            latency_ms=response.latency_ms,
            warnings=warnings,
            acknowledged=acknowledged,
            based_on_version=based_on,
        )

    # ---- context ---------------------------------------------------------------------------

    def _build_context(
        self,
        project_id: uuid.UUID,
        plan: ImplementationPlan,
        task: PlanTask,
        tasks: Sequence[PlanTask],
        workspace: dict[str, WorkspaceFile],
        acknowledged: bool,
        warnings: list[str],
        feedback: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        confirmed = {str(r.id): r for r in self._requirements.list(project_id, "confirmed")}
        linked = [confirmed[r] for r in task.requirement_ids if r in confirmed]
        codes: dict[str, str] = {}
        for n, req in enumerate(sorted(confirmed.values(), key=lambda r: str(r.id)), start=1):
            codes[str(req.id)] = requirement_code(req.source_quote) or f"R{n:03d}"
        requirements = sorted(
            (
                {
                    "code": codes[str(r.id)],
                    "type": r.type,
                    "title": _cut(r.title, 200),
                    "description": _cut(r.description, MAX_FIELD_CHARS),
                }
                for r in linked
            ),
            key=lambda r: r["code"],
        )
        if len(requirements) > MAX_REQUIREMENTS:
            warnings.append(
                f"Only {MAX_REQUIREMENTS} of {len(requirements)} linked requirements were sent"
            )
            requirements = requirements[:MAX_REQUIREMENTS]
        decision_block, flags = self.decision_context(project_id, plan, task)
        rules = self.frameworks(project_id, workspace).context()
        context: dict[str, Any] = {
            **decision_block,
            **(feedback or {}),
            **({"framework_rules": rules} if rules else {}),
            "task": {
                "key": task.key,
                "title": task.title,
                "kind": task.kind,
                "description": _cut(task.description, 2000),
                "acceptance_criteria": [
                    _cut(c, MAX_FIELD_CHARS) for c in task.acceptance_criteria[:20]
                ],
                "flags": flags,
                "unresolved_blocking_questions": (
                    [_cut(q, MAX_FIELD_CHARS) for q in task.blocking_questions[:10]]
                    if acknowledged
                    else []
                ),
            },
            "requirements": requirements,
            "architecture": self._architecture_context(project_id, plan, task, codes),
        }
        budget = self._max_context_chars - len(json.dumps(context, ensure_ascii=False))
        if budget <= 0:
            raise PatchContextTooLargeError("The task context exceeds the size limit")
        dependencies, used = self._dependency_context(
            tasks, task, workspace, budget // 3, warnings, project_id
        )
        context["dependencies"] = dependencies
        context["workspace"] = self._workspace_context(
            project_id, task, dependencies, workspace, budget - used, warnings
        )
        return context

    def _architecture_context(
        self,
        project_id: uuid.UUID,
        plan: ImplementationPlan,
        task: PlanTask,
        codes: dict[str, str],
    ) -> dict[str, Any]:
        proposal = self._architecture.get(project_id, plan.proposal_id)
        if proposal is None:
            return {}
        content = ArchitectureContent.model_validate(proposal.content).model_dump(mode="json")
        match = _SOURCE_REF.match(task.source_ref)
        item: Any = None
        if match and match.group(1) in content:
            section, index = match.group(1), int(match.group(2))
            rows = content[section]
            if isinstance(rows, list) and index < len(rows):
                item = {"section": section, **_strip(rows[index], codes)}
        wanted = set(task.requirement_ids)
        related: list[dict[str, Any]] = []
        for section in (
            "recommended_stack",
            "integrations",
            "data_architecture",
            "security_architecture",
            "non_functional_design",
        ):
            for position, row in enumerate(content[section]):
                if f"{section}[{position}]" == task.source_ref:
                    continue
                if wanted & set(row.get("supporting_requirement_ids", [])):
                    related.append({"section": section, **_strip(row, codes)})
        component = item.get("name") if item else None
        edges = [
            {"from": e["source"], "to": e["target"], "relationship": e["relationship"]}
            for e in content["diagram"]["edges"]
            if component
            and component in {_node_name(content, e["source"]), _node_name(content, e["target"])}
        ]
        for edge in edges:
            edge["from"], edge["to"] = (
                _node_name(content, edge["from"]),
                _node_name(content, edge["to"]),
            )
        return {
            "solution_overview": _cut(content["solution_overview"], 1500),
            "item": item,
            "related_items": related[:MAX_RELATED_ITEMS],
            "diagram_edges": edges[:20],
        }

    def _dependency_context(
        self,
        tasks: Sequence[PlanTask],
        task: PlanTask,
        workspace: dict[str, WorkspaceFile],
        budget: int,
        warnings: list[str],
        project_id: uuid.UUID,
    ) -> tuple[list[dict[str, Any]], int]:
        out: list[dict[str, Any]] = []
        used = 0
        cut = False
        for dep in tasks:
            if dep.key not in task.depends_on:
                continue
            accepted = next(
                (p for p in self._patches.for_task(project_id, dep.id) if p.status == "accepted"),
                None,
            )
            files: list[dict[str, Any]] = []
            if accepted is not None:
                for pf in self._patches.files(project_id, accepted.id):
                    current = workspace.get(pf.path)
                    if current is None:
                        continue
                    room = min(MAX_FILE_CHARS, max(budget - used, 0))
                    text = _cut(current.content, room) if room else ""
                    cut = cut or len(current.content) > room
                    used += len(text)
                    files.append({"path": pf.path, "content": text})
            out.append(
                {
                    "key": dep.key,
                    "title": dep.title,
                    "accepted": accepted is not None,
                    "files": files,
                }
            )
        if cut:
            warnings.append("Some dependency file contents were shortened to fit the context limit")
        return out, used

    def _workspace_context(
        self,
        project_id: uuid.UUID,
        task: PlanTask,
        dependencies: list[dict[str, Any]],
        workspace: dict[str, WorkspaceFile],
        budget: int,
        warnings: list[str],
    ) -> dict[str, Any]:
        paths = sorted(workspace)
        if len(paths) > MAX_WORKSPACE_PATHS:
            warnings.append(
                f"Only {MAX_WORKSPACE_PATHS} of {len(paths)} workspace paths were listed"
            )
        already = {f["path"] for d in dependencies for f in d["files"]}
        own = {
            pf.path
            for p in self._patches.for_task(project_id, task.id)
            for pf in self._patches.files(project_id, p.id)
        }
        tokens = {
            w
            for w in re.findall(r"[a-z0-9]+", task.title.lower())
            if len(w) >= 4 and w not in _GENERIC_WORDS
        }
        likely = [
            p
            for p in paths
            if p not in already and (p in own or any(t in p.lower() for t in tokens))
        ]
        files: list[dict[str, Any]] = []
        cut = False
        for path in likely[:MAX_CONTEXT_FILES]:
            room = min(MAX_FILE_CHARS, max(budget, 0))
            if room == 0:
                cut = True
                break
            text = workspace[path].content
            cut = cut or len(text) > room
            files.append({"path": path, "content": _cut(text, room), "truncated": len(text) > room})
            budget -= min(len(text), room)
        if len(likely) > MAX_CONTEXT_FILES or cut:
            warnings.append("Not every likely workspace file was sent in full")
        return {"paths": paths[:MAX_WORKSPACE_PATHS], "paths_total": len(paths), "files": files}

    # ---- reading ---------------------------------------------------------------------------

    def detail(self, project_id: uuid.UUID, patch_id: uuid.UUID) -> PatchDetail:
        if self._projects.get(project_id) is None:
            raise ProjectNotFoundError(project_id)
        patch = self._patches.get(project_id, patch_id)
        if patch is None:
            raise PatchNotFoundError(patch_id)
        files = self._patches.files(project_id, patch.id)
        live = {f.path: f.content for f in self._patches.workspace(project_id)}
        stale: list[Failure] = []
        if patch.status == "proposed":
            hashes = {p: content_hash(c) for p, c in live.items()}
            stale = stale_failures(
                [(f.path, f.change, f.previous_content_hash) for f in files], hashes
            )
        views = []
        for f in files:
            before = live.get(f.path) if patch.status == "proposed" else f.previous_content
            diff, cut = unified_diff(f.path, f.change, before, f.content)
            views.append(FileView(f, diff, cut))
        return PatchDetail(patch, files, views, stale)

    def for_task(
        self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str
    ) -> Sequence[CodePatch]:
        _, task, _ = self._plan_and_task(project_id, plan_id, task_key)
        return self._patches.for_task(project_id, task.id)

    def file_counts(
        self, project_id: uuid.UUID, patch_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        return self._patches.file_counts(project_id, patch_ids)

    def workspace(self, project_id: uuid.UUID) -> Sequence[WorkspaceFile]:
        if self._projects.get(project_id) is None:
            raise ProjectNotFoundError(project_id)
        return self._patches.workspace(project_id)

    # ---- review ----------------------------------------------------------------------------

    def _review_target(
        self, project_id: uuid.UUID, patch_id: uuid.UUID, reviewer: str, reason: str
    ) -> tuple[CodePatch, Sequence[PatchFile]]:
        if self._projects.get(project_id) is None:
            raise ProjectNotFoundError(project_id)
        patch = self._patches.get(project_id, patch_id)
        if patch is None:
            raise PatchNotFoundError(patch_id)
        if not reviewer.strip() or not reason.strip():
            raise ReviewInputError("A reviewer name and a reason are required")
        if patch.status != "proposed":
            raise PatchNotProposedError(
                f"This patch is already {patch.status}; a reviewed patch is immutable. "
                "Regenerate to create a new version."
            )
        return patch, self._patches.files(project_id, patch.id)

    def accept(
        self, project_id: uuid.UUID, patch_id: uuid.UUID, reviewer: str, reason: str
    ) -> PatchDetail:
        patch, files = self._review_target(project_id, patch_id, reviewer, reason)
        hashes = self._patches.workspace_hashes(project_id)
        failures = stale_failures(
            [(f.path, f.change, f.previous_content_hash) for f in files], hashes
        )
        if failures:
            raise StalePatchError(failures)
        self._patches.accept(patch, files, reviewer.strip(), reason.strip())
        return self.detail(project_id, patch_id)

    def reject(
        self, project_id: uuid.UUID, patch_id: uuid.UUID, reviewer: str, reason: str
    ) -> PatchDetail:
        patch, _ = self._review_target(project_id, patch_id, reviewer, reason)
        self._patches.reject(patch, reviewer.strip(), reason.strip())
        return self.detail(project_id, patch_id)


def _node_name(content: dict[str, Any], node_id: str) -> str:
    for node in content["diagram"]["nodes"]:
        if node["id"] == node_id:
            return str(node["name"])
    return node_id
