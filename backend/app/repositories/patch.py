import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.patch import content_hash
from app.models.patch import CodePatch, PatchFile, TechnologyConfirmation, WorkspaceFile


class PatchRepository:
    """Every query is scoped by project."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # ---- workspace -------------------------------------------------------------------------

    def workspace(self, project_id: uuid.UUID) -> Sequence[WorkspaceFile]:
        stmt = (
            select(WorkspaceFile)
            .where(WorkspaceFile.project_id == project_id)
            .order_by(WorkspaceFile.path)
        )
        return self._session.scalars(stmt).all()

    def workspace_hashes(self, project_id: uuid.UUID) -> dict[str, str]:
        stmt = select(WorkspaceFile.path, WorkspaceFile.content_hash).where(
            WorkspaceFile.project_id == project_id
        )
        return {path: digest for path, digest in self._session.execute(stmt).all()}

    # ---- patches ---------------------------------------------------------------------------

    def get(self, project_id: uuid.UUID, patch_id: uuid.UUID) -> CodePatch | None:
        stmt = select(CodePatch).where(CodePatch.project_id == project_id, CodePatch.id == patch_id)
        return self._session.scalars(stmt).one_or_none()

    def files(self, project_id: uuid.UUID, patch_id: uuid.UUID) -> Sequence[PatchFile]:
        stmt = (
            select(PatchFile)
            .where(PatchFile.project_id == project_id, PatchFile.patch_id == patch_id)
            .order_by(PatchFile.path)
        )
        return self._session.scalars(stmt).all()

    def for_task(self, project_id: uuid.UUID, task_id: uuid.UUID) -> Sequence[CodePatch]:
        """Newest version first."""
        stmt = (
            select(CodePatch)
            .where(CodePatch.project_id == project_id, CodePatch.task_id == task_id)
            .order_by(CodePatch.version.desc())
        )
        return self._session.scalars(stmt).all()

    def next_version(self, project_id: uuid.UUID, task_id: uuid.UUID) -> int:
        stmt = select(func.max(CodePatch.version)).where(
            CodePatch.project_id == project_id, CodePatch.task_id == task_id
        )
        return (self._session.scalar(stmt) or 0) + 1

    def statuses_by_task(
        self, project_id: uuid.UUID, plan_id: uuid.UUID
    ) -> dict[uuid.UUID, set[str]]:
        stmt = select(CodePatch.task_id, CodePatch.status).where(
            CodePatch.project_id == project_id, CodePatch.plan_id == plan_id
        )
        out: dict[uuid.UUID, set[str]] = defaultdict(set)
        for task_id, status in self._session.execute(stmt).all():
            out[task_id].add(status)
        return out

    def add(self, patch: CodePatch, files: Sequence[PatchFile]) -> CodePatch:
        try:
            self._session.add(patch)
            self._session.flush()
            self._session.add_all(files)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(patch)
        return patch

    def reject(self, patch: CodePatch, reviewer: str, reason: str) -> CodePatch:
        self._mark(patch, "rejected", reviewer, reason)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(patch)
        return patch

    def accept(
        self, patch: CodePatch, files: Sequence[PatchFile], reviewer: str, reason: str
    ) -> CodePatch:
        """Apply the files to the workspace and mark the patch accepted in one transaction."""
        try:
            for file in files:
                existing = self._session.get(WorkspaceFile, (patch.project_id, file.path))
                if file.change == "delete":
                    if existing is not None:
                        self._session.delete(existing)
                elif existing is None:
                    self._session.add(
                        WorkspaceFile(
                            project_id=patch.project_id,
                            path=file.path,
                            content=file.content,
                            content_hash=content_hash(file.content),
                            source_patch_id=patch.id,
                        )
                    )
                else:
                    existing.content = file.content
                    existing.content_hash = content_hash(file.content)
                    existing.source_patch_id = patch.id
            self._mark(patch, "accepted", reviewer, reason)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(patch)
        return patch

    @staticmethod
    def _mark(patch: CodePatch, status: str, reviewer: str, reason: str) -> None:
        patch.status = status
        patch.reviewed_by = reviewer
        patch.review_reason = reason
        patch.reviewed_at = datetime.now(UTC)

    # ---- technology confirmations ----------------------------------------------------------

    def confirmations(
        self, project_id: uuid.UUID, plan_id: uuid.UUID, task_key: str | None = None
    ) -> Sequence[TechnologyConfirmation]:
        stmt = select(TechnologyConfirmation).where(
            TechnologyConfirmation.project_id == project_id,
            TechnologyConfirmation.plan_id == plan_id,
        )
        if task_key is not None:
            stmt = stmt.where(TechnologyConfirmation.task_key == task_key)
        return self._session.scalars(
            stmt.order_by(TechnologyConfirmation.created_at, TechnologyConfirmation.id)
        ).all()

    def add_confirmation(self, row: TechnologyConfirmation) -> TechnologyConfirmation:
        try:
            self._session.add(row)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(row)
        return row
