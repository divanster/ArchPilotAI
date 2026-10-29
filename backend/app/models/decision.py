import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.decisions import AREAS
from app.models.base import Base
from app.models.project import _utcnow

_AREA_LIST = ", ".join(f"'{a}'" for a in AREAS)


class ProjectDecision(Base):
    """A human technology decision. Rows are never edited except to set `superseded_by` once."""

    __tablename__ = "project_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["superseded_by", "project_id"],
            ["project_decisions.id", "project_decisions.project_id"],
            ondelete="CASCADE",
            deferrable=True,
            initially="DEFERRED",
            name="fk_decision_superseded_by",
        ),
        # Target of the composite foreign key above.
        UniqueConstraint("id", "project_id", name="uq_decision_id_project"),
        # At most one current decision per area and project.
        Index(
            "uq_decision_current_area",
            "project_id",
            "area",
            unique=True,
            sqlite_where=text("superseded_by IS NULL"),
            postgresql_where=text("superseded_by IS NULL"),
        ),
        CheckConstraint(f"area IN ({_AREA_LIST})", name="ck_decision_area"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    area: Mapped[str] = mapped_column(String(20), nullable=False)
    choice: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(100), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    def as_json(self) -> dict[str, Any]:
        return {
            "id": str(self.id),
            "area": self.area,
            "choice": self.choice,
            "reason": self.reason,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at.isoformat(),
            "superseded_by": str(self.superseded_by) if self.superseded_by else None,
        }
