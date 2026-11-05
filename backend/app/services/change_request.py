import hashlib
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.architecture import ArchitectureContent, ProposalStatus
from app.domain.change_request import (
    CLASSES,
    MAX_TEXT,
    detect_drift,
    diff_architecture,
    removal_ids,
    structure,
    tokens,
)
from app.domain.change_request import classify as classify_text
from app.models.agent import AgentRun
from app.models.architecture import ArchitectureProposal
from app.models.change_request import ChangeRequest
from app.models.document import Document
from app.models.plan import ImplementationPlan, PlanTask
from app.models.requirement import ExtractionRun, Requirement
from app.repositories.agent import AgentRepository
from app.repositories.architecture import ArchitectureRepository
from app.repositories.decision import DecisionRepository
from app.repositories.patch import PatchRepository
from app.repositories.plan import PlanRepository
from app.repositories.project import ProjectRepository
from app.repositories.requirement import RequirementRepository
from app.services.architecture import (
    ArchitectureService,
    ProposalChange,
    RemovalsNotConfirmedError,
)
from app.services.architecture_generation import ArchitectureGenerationService
from app.services.ci_loop import ITERATION
from app.services.plan import PlanDetail, PlanService
from app.services.project import ProjectNotFoundError

logger = logging.getLogger(__name__)

PROVIDER = "change_request"
OPEN_STATUSES = ("draft", "analysed", "approved", "in_progress")


class ChangeRequestNotFoundError(Exception):
    def __init__(self, change_id: uuid.UUID) -> None:
        super().__init__(f"Change request {change_id} not found")


class ChangeRequestStateError(Exception):
    """The request is not in a state that allows the action."""


class ChangeRequestInputError(ValueError):
    pass


class NoBaselineError(Exception):
    """A change request needs an approved architecture and an approved plan."""


@dataclass(frozen=True)
class RemovalConfirmation:
    kind: str
    key: str
    reason: str


def effective_class(cr: ChangeRequest) -> str | None:
    return cr.class_override or cr.impact_class


class ChangeRequestService:
    def __init__(
        self,
        session: Session,
        *,
        generation: ArchitectureGenerationService | None = None,
        architecture_service: ArchitectureService | None = None,
        plan_service: PlanService | None = None,
        repo_paths: Callable[[uuid.UUID], list[str]] | None = None,
    ) -> None:
        self._session = session
        self._generation = generation
        self._architecture_service = architecture_service
        self._plan_service = plan_service
        self._repo_paths = repo_paths
        self._projects = ProjectRepository(session)
        self._architecture = ArchitectureRepository(session)
        self._plans = PlanRepository(session)
        self._requirements = RequirementRepository(session)
        self._decisions = DecisionRepository(session)
        self._patches = PatchRepository(session)
        self._agents = AgentRepository(session)

    # ---- reads -----------------------------------------------------------------------------

    def _require_project(self, project_id: uuid.UUID) -> None:
        if self._projects.get(project_id) is None:
            raise ProjectNotFoundError(project_id)

    def get(self, project_id: uuid.UUID, change_id: uuid.UUID) -> ChangeRequest:
        self._require_project(project_id)
        cr = self._session.scalars(
            select(ChangeRequest).where(
                ChangeRequest.project_id == project_id, ChangeRequest.id == change_id
            )
        ).one_or_none()
        if cr is None:
            raise ChangeRequestNotFoundError(change_id)
        return cr

    def list_all(self, project_id: uuid.UUID) -> Sequence[ChangeRequest]:
        self._require_project(project_id)
        return self._session.scalars(
            select(ChangeRequest)
            .where(ChangeRequest.project_id == project_id)
            .order_by(ChangeRequest.created_at.desc(), ChangeRequest.key.desc())
        ).all()

    def _commit(self, cr: ChangeRequest) -> ChangeRequest:
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(cr)
        return cr

    def _approved_architecture(self, project_id: uuid.UUID) -> ArchitectureProposal:
        approved = self._architecture.list(project_id, ProposalStatus.APPROVED.value)
        if not approved:
            raise NoBaselineError("The project has no approved architecture")
        return approved[0]

    def _approved_plan(self, project_id: uuid.UUID) -> ImplementationPlan:
        plans = self._plans.list(project_id, "approved")
        if not plans:
            raise NoBaselineError("The project has no approved plan")
        return plans[0]

    def _paths(self, project_id: uuid.UUID) -> list[str]:
        paths = {f.path for f in self._patches.workspace(project_id)}
        if self._repo_paths is not None:
            paths |= set(self._repo_paths(project_id))
        return sorted(paths)

    # ---- lifecycle -------------------------------------------------------------------------

    def create(self, project_id: uuid.UUID, text: str, author: str) -> ChangeRequest:
        self._require_project(project_id)
        text, author = text.strip(), author.strip()
        if not text or not author:
            raise ChangeRequestInputError("The request text and the author are required")
        if len(text) > MAX_TEXT:
            raise ChangeRequestInputError(f"The request is longer than {MAX_TEXT} characters")
        self._approved_architecture(project_id)
        self._approved_plan(project_id)
        count = (
            self._session.scalar(
                select(func.count())
                .select_from(ChangeRequest)
                .where(ChangeRequest.project_id == project_id)
            )
            or 0
        )
        cr = ChangeRequest(
            project_id=project_id,
            key=f"CR-{count + 1:03d}",
            text=text,
            author=author,
            status="draft",
            proposed_requirement_ids=[],
            removals_confirmed=[],
        )
        self._session.add(cr)
        return self._commit(cr)

    def analyse(self, project_id: uuid.UUID, change_id: uuid.UUID) -> ChangeRequest:
        cr = self.get(project_id, change_id)
        if cr.status not in ("draft", "analysed"):
            raise ChangeRequestStateError(f"A {cr.status} request cannot be analysed again")
        architecture = self._approved_architecture(project_id)
        content = ArchitectureContent.model_validate(architecture.content)
        words = tokens(cr.text)
        paths = self._paths(project_id)
        related = [
            p
            for p, _ in sorted(
                ((p, len(tokens(p.replace("/", " ").replace("_", " ")) & words)) for p in paths),
                key=lambda item: (-item[1], item[0]),
            )
            if _ > 0
        ][:8]
        impact = classify_text(
            cr.text,
            content,
            [{"area": d.area, "choice": d.choice} for d in self._decisions.current(project_id)],
            [
                {"id": str(r.id), "title": r.title}
                for r in self._requirements.list(project_id, "confirmed")
            ],
            related,
        )
        cr.impact_class = impact.impact_class
        cr.impact = impact.as_json() | {"architecture_version": architecture.version}
        cr.status = "analysed"
        return self._commit(cr)

    def set_class(
        self, project_id: uuid.UUID, change_id: uuid.UUID, impact_class: str, by: str, reason: str
    ) -> ChangeRequest:
        cr = self.get(project_id, change_id)
        if impact_class not in CLASSES:
            raise ChangeRequestInputError(f"The class must be one of {', '.join(CLASSES)}")
        if not by.strip() or not reason.strip():
            raise ChangeRequestInputError("The person and a reason are required")
        if cr.status not in ("draft", "analysed", "approved"):
            raise ChangeRequestStateError(f"The class of a {cr.status} request cannot change")
        if cr.impact_class is None:
            raise ChangeRequestStateError("Analyse the request before overriding its class")
        cr.class_override = None if impact_class == cr.impact_class else impact_class
        cr.overridden_by = by.strip() if cr.class_override else None
        cr.override_reason = reason.strip() if cr.class_override else None
        return self._commit(cr)

    def approve(
        self, project_id: uuid.UUID, change_id: uuid.UUID, by: str, reason: str | None
    ) -> ChangeRequest:
        cr = self.get(project_id, change_id)
        if cr.status != "analysed":
            raise ChangeRequestStateError("Only an analysed request can be approved")
        if not by.strip():
            raise ChangeRequestInputError("The approver is required")
        cr.status = "approved"
        cr.decided_by = by.strip()
        cr.decision_reason = (reason or "").strip() or None
        return self._commit(cr)

    def reject(
        self, project_id: uuid.UUID, change_id: uuid.UUID, by: str, reason: str
    ) -> ChangeRequest:
        cr = self.get(project_id, change_id)
        if cr.status not in OPEN_STATUSES:
            raise ChangeRequestStateError(f"A {cr.status} request cannot be rejected")
        if not by.strip() or not reason.strip():
            raise ChangeRequestInputError("The person and a reason are required")
        cr.status = "rejected"
        cr.decided_by = by.strip()
        cr.decision_reason = reason.strip()
        return self._commit(cr)

    def mark_done(self, project_id: uuid.UUID, change_id: uuid.UUID) -> ChangeRequest:
        cr = self.get(project_id, change_id)
        if cr.status != "in_progress":
            raise ChangeRequestStateError("Only a request in progress can be marked done")
        cr.status = "done"
        return self._commit(cr)

    # ---- architecture path -----------------------------------------------------------------

    def _proposed_requirement(self, cr: ChangeRequest) -> None:
        """One proposed requirement from the request text; a person confirms it, never the code."""
        if cr.proposed_requirement_ids:
            return
        name = f"change-request-{cr.key}.txt"
        document = Document(
            project_id=cr.project_id,
            original_filename=name,
            media_type="text/plain",
            size_bytes=len(cr.text.encode("utf-8")),
            content_sha256=hashlib.sha256(cr.text.encode("utf-8")).hexdigest(),
            extracted_text=cr.text,
        )
        self._session.add(document)
        self._session.flush()
        run = ExtractionRun(
            project_id=cr.project_id,
            document_id=document.id,
            provider=PROVIDER,
            model="manual",
            prompt_version="change-request-v1",
            status="succeeded",
        )
        self._session.add(run)
        self._session.flush()
        requirement = Requirement(
            project_id=cr.project_id,
            document_id=document.id,
            extraction_run_id=run.id,
            type="functional",
            title=(cr.text.splitlines()[0] if cr.text else cr.key)[:200],
            description=cr.text,
            source_quote=cr.text,
            source_start=0,
            source_end=len(cr.text),
            status="proposed",
        )
        self._session.add(requirement)
        self._session.flush()
        cr.proposed_requirement_ids = [str(requirement.id)]

    def propose_architecture(
        self,
        project_id: uuid.UUID,
        change_id: uuid.UUID,
        drift: list[dict[str, Any]] | None = None,
    ) -> ArchitectureProposal:
        if self._generation is None:
            raise RuntimeError("No architecture generation service")
        cr = self.get(project_id, change_id)
        if effective_class(cr) != "architecture":
            raise ChangeRequestStateError("Only an architecture-class request has this path")
        if cr.status not in ("approved", "in_progress"):
            raise ChangeRequestStateError("Approve the request before proposing architecture")
        parent = self._approved_architecture(project_id)
        self._proposed_requirement(cr)
        proposal = self._generation.propose_change(
            project_id,
            parent.id,
            {"key": cr.key, "text": cr.text, "impact": (cr.impact or {}).get("reason", "")},
            cr.id,
            drift,
        )
        cr.status = "in_progress"
        self._commit(cr)
        return proposal

    def proposals(self, project_id: uuid.UUID, cr: ChangeRequest) -> list[ArchitectureProposal]:
        return [
            p
            for p in self._architecture.list(project_id)
            if p.change_request_id == cr.id and p.project_id == project_id
        ]

    def diff(self, project_id: uuid.UUID, proposal: ArchitectureProposal) -> dict[str, Any] | None:
        if proposal.revised_from_id is None:
            return None
        parent = self._architecture.get(project_id, proposal.revised_from_id)
        if parent is None:
            return None
        return diff_architecture(
            ArchitectureContent.model_validate(parent.content),
            ArchitectureContent.model_validate(proposal.content),
        )

    def check_approval(self, project_id: uuid.UUID, proposal: ArchitectureProposal) -> None:
        """Guard for ArchitectureService: removals of a change-request draft must be confirmed."""
        if proposal.change_request_id is None:
            return
        diff = self.diff(project_id, proposal)
        cr = self._session.get(ChangeRequest, proposal.change_request_id)
        if diff is None or cr is None or cr.project_id != project_id:
            return
        missing = sorted(removal_ids(diff) - set(cr.removals_confirmed))
        if missing:
            raise RemovalsNotConfirmedError(
                "Confirm the removals (with a reason each) before approving: " + ", ".join(missing)
            )

    def approve_architecture(
        self,
        project_id: uuid.UUID,
        change_id: uuid.UUID,
        confirmations: Sequence[RemovalConfirmation],
    ) -> ArchitectureProposal:
        if self._architecture_service is None:
            raise RuntimeError("No architecture service")
        cr = self.get(project_id, change_id)
        drafts = [p for p in self.proposals(project_id, cr) if p.status == "draft"]
        if not drafts:
            raise ChangeRequestStateError("The request has no draft architecture to approve")
        draft = drafts[0]
        diff = self.diff(project_id, draft) or {}
        wanted = removal_ids(diff)
        given = {f"{c.kind}:{c.key}" for c in confirmations if c.reason.strip()}
        cr.removals_confirmed = sorted(wanted & given)
        self._commit(cr)
        return self._architecture_service.update(
            project_id, draft.id, ProposalChange(status=ProposalStatus.APPROVED)
        )

    def plan_delta(self, project_id: uuid.UUID, change_id: uuid.UUID) -> PlanDetail:
        if self._plan_service is None:
            raise RuntimeError("No plan service")
        cr = self.get(project_id, change_id)
        approved = [p for p in self.proposals(project_id, cr) if p.status == "approved"]
        if not approved:
            raise ChangeRequestStateError("Approve the architecture of the request first")
        if any(p.change_request_id == cr.id for p in self._delta_plans(project_id, cr)):
            raise ChangeRequestStateError("A plan delta already exists; decide it instead")
        return self._plan_service.build_delta(project_id, approved[0].id, cr.id)

    def _delta_plans(self, project_id: uuid.UUID, cr: ChangeRequest) -> list[ImplementationPlan]:
        return [
            p for p in self._plans.list(project_id, None, "delta") if p.change_request_id == cr.id
        ]

    # ---- fast path -------------------------------------------------------------------------

    def anchor_task(
        self, project_id: uuid.UUID, cr: ChangeRequest, task_key: str | None
    ) -> tuple[ImplementationPlan, PlanTask]:
        """The existing approved task a fast-path run is bounded to."""
        if effective_class(cr) != "code_only":
            raise ChangeRequestStateError("Only a code-only request takes the fast path")
        if cr.status not in ("approved", "in_progress"):
            raise ChangeRequestStateError("Approve the request before running it")
        plan = self._approved_plan(project_id)
        tasks = self._plans.tasks(project_id, plan.id)
        if task_key is not None:
            found = next((t for t in tasks if t.key == task_key), None)
            if found is None:
                raise ChangeRequestInputError(f"Task {task_key} is not in the approved plan")
            return plan, found
        names = {c["name"].lower() for c in (cr.impact or {}).get("components", [])}
        words = tokens(cr.text)

        def score(t: PlanTask) -> tuple[int, int]:
            by_name = int(any(n in (t.title + " " + t.description).lower() for n in names))
            return by_name, len(tokens(t.title + " " + t.description) & words)

        runnable = [t for t in tasks if t.kind != "setup"] or list(tasks)
        if not runnable:
            raise NoBaselineError("The approved plan has no task")
        return plan, max(runnable, key=lambda t: (score(t), -t.order_index))

    def take_baseline(self, project_id: uuid.UUID, cr: ChangeRequest) -> bool:
        """Snapshot the source structure that the drift check compares with; True when new."""
        if cr.baseline is not None:
            return False
        cr.baseline = structure(self._paths(project_id))
        self._commit(cr)
        return True

    def clear_baseline(self, cr: ChangeRequest) -> None:
        cr.baseline = None
        self._commit(cr)

    def mark_started(self, cr: ChangeRequest) -> None:
        cr.status = "in_progress"
        self._commit(cr)

    # ---- drift -----------------------------------------------------------------------------

    def after_patch_accepted(
        self, project_id: uuid.UUID, patch_id: uuid.UUID
    ) -> ChangeRequest | None:
        run = self._session.scalars(
            select(AgentRun).where(
                AgentRun.project_id == project_id,
                AgentRun.patch_id == patch_id,
                AgentRun.change_request_id.is_not(None),
            )
        ).first()
        if run is None or run.change_request_id is None:
            return None
        cr = self.get(project_id, run.change_request_id)
        content = ArchitectureContent.model_validate(
            self._approved_architecture(project_id).content
        )
        texts = {
            f.path: f.content
            for f in self._patches.files(project_id, patch_id)
            if f.change != "delete"
        }
        items = detect_drift(content, cr.baseline, self._paths(project_id), texts)
        known = {(d["kind"], d["name"]) for d in (cr.drift or [])}
        cr.drift = [*(cr.drift or []), *(i for i in items if (i["kind"], i["name"]) not in known)]
        return self._commit(cr)

    def propose_architecture_update(
        self, project_id: uuid.UUID, change_id: uuid.UUID, by: str
    ) -> ArchitectureProposal:
        cr = self.get(project_id, change_id)
        if not cr.drift:
            raise ChangeRequestStateError("No architecture drift is flagged on this request")
        if effective_class(cr) != "architecture":
            cr.class_override = "architecture"
            cr.overridden_by = by.strip() or "drift-check"
            cr.override_reason = "Architecture drift found after the patch was accepted"
            self._commit(cr)
        return self.propose_architecture(project_id, change_id, list(cr.drift))

    def record_pull_request(
        self, project_id: uuid.UUID, patch_id: uuid.UUID, url: str | None
    ) -> None:
        if not url:
            return
        run = self._session.scalars(
            select(AgentRun).where(
                AgentRun.project_id == project_id,
                AgentRun.patch_id == patch_id,
                AgentRun.change_request_id.is_not(None),
            )
        ).first()
        if run is None or run.change_request_id is None:
            return
        cr = self.get(project_id, run.change_request_id)
        cr.pull_request_url = url[:500]
        self._commit(cr)

    # ---- change log ------------------------------------------------------------------------

    def log(self, project_id: uuid.UUID, cr: ChangeRequest) -> dict[str, Any]:
        runs = list(
            self._session.scalars(
                select(AgentRun)
                .where(AgentRun.project_id == project_id, AgentRun.change_request_id == cr.id)
                .order_by(AgentRun.started_at, AgentRun.id)
            )
        )
        patch_ids = [r.patch_id for r in runs if r.patch_id is not None]
        patches = []
        for pid in dict.fromkeys(patch_ids):
            patch = self._patches.get(project_id, pid)
            if patch is not None:
                patches.append(
                    {"id": str(patch.id), "task_key": patch.task_key, "status": patch.status}
                )
        ci = [
            {"run_id": str(row.run_id), **dict(row.summary or {})}
            for row in self._agents.loop_rows(project_id, [r.id for r in runs], ITERATION)
        ]
        return {
            "architecture": [
                {
                    "id": str(p.id),
                    "version": p.version,
                    "status": p.status,
                    "parent_version": p.revised_from_version,
                }
                for p in self.proposals(project_id, cr)
            ],
            "plan_deltas": [
                {
                    "id": str(p.id),
                    "version": p.version,
                    "status": p.status,
                    "base_plan_id": str(p.base_plan_id) if p.base_plan_id else None,
                }
                for p in self._delta_plans(project_id, cr)
            ],
            "runs": [{"id": str(r.id), "task_key": r.task_key, "status": r.status} for r in runs],
            "patches": patches,
            "ci_runs": ci,
            "pull_request_url": cr.pull_request_url,
            "requirement_ids": list(cr.proposed_requirement_ids),
        }
