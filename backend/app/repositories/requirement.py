import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.requirement import ExtractionRun, Requirement


class RequirementRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, project_id: uuid.UUID, requirement_id: uuid.UUID) -> Requirement | None:
        stmt = select(Requirement).where(
            Requirement.project_id == project_id, Requirement.id == requirement_id
        )
        return self._session.scalars(stmt).one_or_none()

    def list(
        self,
        project_id: uuid.UUID,
        status: str | None = None,
        document_id: uuid.UUID | None = None,
    ) -> Sequence[Requirement]:
        stmt = select(Requirement).where(Requirement.project_id == project_id)
        if status is not None:
            stmt = stmt.where(Requirement.status == status)
        if document_id is not None:
            stmt = stmt.where(Requirement.document_id == document_id)
        stmt = stmt.order_by(Requirement.created_at, Requirement.source_start, Requirement.id)
        return self._session.scalars(stmt).all()

    def existing_spans(self, document_id: uuid.UUID) -> set[tuple[int, int, str]]:
        """Spans already claimed by any requirement (including rejected) of this document."""
        stmt = select(Requirement.source_start, Requirement.source_end, Requirement.type).where(
            Requirement.document_id == document_id
        )
        return {(row[0], row[1], row[2]) for row in self._session.execute(stmt)}

    def save(self, requirement: Requirement) -> Requirement:
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(requirement)
        return requirement

    def record_failed_run(self, run: ExtractionRun) -> ExtractionRun:
        self._session.add(run)
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return run

    def persist_extraction(
        self, run: ExtractionRun, requirements: Sequence[Requirement]
    ) -> Sequence[Requirement]:
        """Run and requirements are written in one transaction: all of them or none."""
        try:
            self._session.add(run)
            self._session.flush()
            self._session.add_all(requirements)
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        return requirements
