import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.checklist import TaskChecklist


class ChecklistRepository:
    """Every query is scoped by project."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def current(self, project_id: uuid.UUID, task_id: uuid.UUID) -> TaskChecklist | None:
        """The latest version; older versions are superseded, rejected or history."""
        stmt = (
            select(TaskChecklist)
            .where(TaskChecklist.project_id == project_id, TaskChecklist.task_id == task_id)
            .order_by(TaskChecklist.version.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).one_or_none()

    def history(self, project_id: uuid.UUID, task_id: uuid.UUID) -> Sequence[TaskChecklist]:
        stmt = (
            select(TaskChecklist)
            .where(TaskChecklist.project_id == project_id, TaskChecklist.task_id == task_id)
            .order_by(TaskChecklist.version.desc())
        )
        return self._session.scalars(stmt).all()

    def get(self, project_id: uuid.UUID, checklist_id: uuid.UUID) -> TaskChecklist | None:
        stmt = select(TaskChecklist).where(
            TaskChecklist.project_id == project_id, TaskChecklist.id == checklist_id
        )
        return self._session.scalars(stmt).one_or_none()

    def add(self, checklist: TaskChecklist, supersede: TaskChecklist | None) -> TaskChecklist:
        """Save a new version; a still-proposed previous version becomes superseded."""
        try:
            checklist.version = (
                self._session.scalar(
                    select(func.max(TaskChecklist.version)).where(
                        TaskChecklist.project_id == checklist.project_id,
                        TaskChecklist.task_id == checklist.task_id,
                    )
                )
                or 0
            ) + 1
            if supersede is not None and supersede.status == "proposed":
                supersede.status = "superseded"
            self._session.add(checklist)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(checklist)
        return checklist

    def decide(self, checklist: TaskChecklist, status: str, by: str, reason: str) -> TaskChecklist:
        checklist.status = status
        checklist.decided_by = by
        checklist.decision_reason = reason
        checklist.decided_at = datetime.now(UTC)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(checklist)
        return checklist
