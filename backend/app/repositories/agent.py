import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from app.models.agent import AgentDraftFile, AgentHookSetting, AgentJob, AgentRun, AgentStep
from app.models.patch import WorkspaceFile


class AgentRepository:
    """Every query is scoped by project."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_run(
        self,
        run: AgentRun,
        workspace: Sequence[WorkspaceFile],
        overlay: Sequence[tuple[str, str, str | None, bool]] = (),
        retire: uuid.UUID | None = None,
    ) -> AgentRun:
        """Save the run together with its draft copy of the accepted workspace.

        `overlay` holds (path, content, base_hash, deleted) of a checkpoint's changed files and is
        applied on top; the draft of the `retire` run (that checkpoint) is dropped in the same
        transaction.
        """
        files = {
            f.path: AgentDraftFile(
                run_id=run.id,
                path=f.path,
                project_id=run.project_id,
                content=f.content,
                base_hash=f.content_hash,
                deleted=False,
            )
            for f in workspace
        }
        for path, content, base_hash, deleted in overlay:
            existing = files.get(path)
            if existing is not None:
                existing.content = content
                existing.deleted = deleted
            else:
                files[path] = AgentDraftFile(
                    run_id=run.id,
                    path=path,
                    project_id=run.project_id,
                    content=content,
                    base_hash=base_hash,
                    deleted=deleted,
                )
        try:
            self._session.add(run)
            self._session.flush()
            self._session.add_all(files.values())
            if retire is not None:
                self.drop_draft(run.project_id, retire)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(run)
        return run

    def get_run(self, project_id: uuid.UUID, run_id: uuid.UUID) -> AgentRun | None:
        stmt = select(AgentRun).where(AgentRun.project_id == project_id, AgentRun.id == run_id)
        return self._session.scalars(stmt).one_or_none()

    def refresh(self, run: AgentRun) -> AgentRun:
        """Re-read the row: a stop request is written by another session."""
        self._session.refresh(run)
        return run

    def runs_for_task(self, project_id: uuid.UUID, task_id: uuid.UUID) -> Sequence[AgentRun]:
        stmt = (
            select(AgentRun)
            .where(AgentRun.project_id == project_id, AgentRun.task_id == task_id)
            .order_by(AgentRun.started_at.desc())
        )
        return self._session.scalars(stmt).all()

    def runs_for_checklist(
        self, project_id: uuid.UUID, checklist_id: uuid.UUID
    ) -> Sequence[AgentRun]:
        """Newest first."""
        stmt = (
            select(AgentRun)
            .where(AgentRun.project_id == project_id, AgentRun.checklist_id == checklist_id)
            .order_by(AgentRun.started_at.desc(), AgentRun.id)
        )
        return self._session.scalars(stmt).all()

    def runs_with_draft(
        self, project_id: uuid.UUID, run_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        if not run_ids:
            return set()
        stmt = (
            select(AgentDraftFile.run_id)
            .where(AgentDraftFile.project_id == project_id, AgentDraftFile.run_id.in_(run_ids))
            .distinct()
        )
        return set(self._session.scalars(stmt).all())

    def steps(self, project_id: uuid.UUID, run_id: uuid.UUID) -> Sequence[AgentStep]:
        stmt = (
            select(AgentStep)
            .where(AgentStep.project_id == project_id, AgentStep.run_id == run_id)
            .order_by(AgentStep.step_no)
        )
        return self._session.scalars(stmt).all()

    def add_step(self, step: AgentStep) -> None:
        self._session.add(step)

    def add_jobs(self, jobs: Sequence[AgentJob]) -> None:
        self._session.add_all(jobs)

    def jobs(self, project_id: uuid.UUID, run_id: uuid.UUID) -> Sequence[AgentJob]:
        stmt = (
            select(AgentJob)
            .where(AgentJob.project_id == project_id, AgentJob.run_id == run_id)
            .order_by(AgentJob.created_at, AgentJob.id)
        )
        return self._session.scalars(stmt).all()

    def commit(self) -> None:
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def disabled_hooks(self, project_id: uuid.UUID) -> set[str]:
        stmt = select(AgentHookSetting.hook).where(
            AgentHookSetting.project_id == project_id, AgentHookSetting.enabled.is_(False)
        )
        return set(self._session.scalars(stmt).all())

    def set_hook_enabled(self, project_id: uuid.UUID, hook: str, enabled: bool) -> None:
        row = self._session.get(AgentHookSetting, (project_id, hook))
        if row is None:
            self._session.add(AgentHookSetting(project_id=project_id, hook=hook, enabled=enabled))
        else:
            row.enabled = enabled
        self.commit()

    def rollback(self) -> None:
        self._session.rollback()

    def request_stop(self, project_id: uuid.UUID, run_id: uuid.UUID) -> bool:
        result = self._session.execute(
            update(AgentRun)
            .where(
                AgentRun.project_id == project_id,
                AgentRun.id == run_id,
                AgentRun.status == "running",
            )
            .values(stop_requested=True)
        )
        self._session.commit()
        return bool(getattr(result, "rowcount", 0))

    # ---- draft -----------------------------------------------------------------------------

    def draft(self, project_id: uuid.UUID, run_id: uuid.UUID) -> Sequence[AgentDraftFile]:
        stmt = (
            select(AgentDraftFile)
            .where(AgentDraftFile.project_id == project_id, AgentDraftFile.run_id == run_id)
            .order_by(AgentDraftFile.path)
        )
        return self._session.scalars(stmt).all()

    def save_draft_file(
        self,
        project_id: uuid.UUID,
        run_id: uuid.UUID,
        path: str,
        content: str,
        base_hash: str | None,
        deleted: bool,
    ) -> None:
        row = self._session.get(AgentDraftFile, (run_id, path))
        if row is None:
            self._session.add(
                AgentDraftFile(
                    run_id=run_id,
                    path=path,
                    project_id=project_id,
                    content=content,
                    base_hash=base_hash,
                    deleted=deleted,
                )
            )
        else:
            row.content = content
            row.deleted = deleted

    def remove_draft_file(self, run_id: uuid.UUID, path: str) -> None:
        self._session.execute(
            delete(AgentDraftFile).where(
                AgentDraftFile.run_id == run_id, AgentDraftFile.path == path
            )
        )

    def drop_draft(self, project_id: uuid.UUID, run_id: uuid.UUID) -> None:
        """The draft is only needed while the run is active."""
        self._session.execute(
            delete(AgentDraftFile).where(
                AgentDraftFile.project_id == project_id, AgentDraftFile.run_id == run_id
            )
        )

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)
