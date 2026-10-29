import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.decision import ProjectDecision


class DecisionRepository:
    """Every query is scoped by project."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def current(self, project_id: uuid.UUID) -> Sequence[ProjectDecision]:
        stmt = (
            select(ProjectDecision)
            .where(
                ProjectDecision.project_id == project_id,
                ProjectDecision.superseded_by.is_(None),
            )
            .order_by(ProjectDecision.area)
        )
        return self._session.scalars(stmt).all()

    def history(self, project_id: uuid.UUID) -> Sequence[ProjectDecision]:
        stmt = (
            select(ProjectDecision)
            .where(
                ProjectDecision.project_id == project_id,
                ProjectDecision.superseded_by.is_not(None),
            )
            .order_by(ProjectDecision.decided_at.desc(), ProjectDecision.id)
        )
        return self._session.scalars(stmt).all()

    def current_for_area(self, project_id: uuid.UUID, area: str) -> ProjectDecision | None:
        stmt = select(ProjectDecision).where(
            ProjectDecision.project_id == project_id,
            ProjectDecision.area == area,
            ProjectDecision.superseded_by.is_(None),
        )
        return self._session.scalars(stmt).one_or_none()

    def replace(self, row: ProjectDecision) -> ProjectDecision:
        """Insert `row` as the current decision; the previous one is marked superseded.

        The old row is updated first (the partial unique index allows one current row), which
        the deferred foreign key permits before the new row exists.
        """
        previous = self.current_for_area(row.project_id, row.area)
        try:
            if previous is not None:
                previous.superseded_by = row.id
                self._session.flush()
            self._session.add(row)
            self._session.flush()
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return row
