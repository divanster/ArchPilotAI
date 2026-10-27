import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.architecture import ArchitectureProposal


class ArchitectureRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, project_id: uuid.UUID, proposal_id: uuid.UUID) -> ArchitectureProposal | None:
        stmt = select(ArchitectureProposal).where(
            ArchitectureProposal.project_id == project_id, ArchitectureProposal.id == proposal_id
        )
        return self._session.scalars(stmt).one_or_none()

    def list(
        self, project_id: uuid.UUID, status: str | None = None
    ) -> Sequence[ArchitectureProposal]:
        stmt = select(ArchitectureProposal).where(ArchitectureProposal.project_id == project_id)
        if status is not None:
            stmt = stmt.where(ArchitectureProposal.status == status)
        return self._session.scalars(
            stmt.order_by(ArchitectureProposal.version.desc(), ArchitectureProposal.id)
        ).all()

    def next_version(self, project_id: uuid.UUID) -> int:
        stmt = select(func.max(ArchitectureProposal.version)).where(
            ArchitectureProposal.project_id == project_id
        )
        return (self._session.scalar(stmt) or 0) + 1

    def add(self, proposal: ArchitectureProposal) -> ArchitectureProposal:
        self._session.add(proposal)
        return self._commit(proposal)

    def save(self, proposal: ArchitectureProposal) -> ArchitectureProposal:
        return self._commit(proposal)

    def _commit(self, proposal: ArchitectureProposal) -> ArchitectureProposal:
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise
        self._session.refresh(proposal)
        return proposal
