import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.change_request import CLASSES, STATUSES
from app.models.base import Base
from app.models.project import _utcnow


class ChangeRequest(Base):
    """A free-form change request against an approved architecture and plan (Phase 15A).

    The computed class is advisory; the effective class is the person's override when set.
    Architecture and plan versions, runs and patches link back to it; none is edited in place.
    """

    __tablename__ = "change_requests"
    __table_args__ = (
        UniqueConstraint("project_id", "key", name="uq_change_request_key"),
        UniqueConstraint("id", "project_id", name="uq_change_request_id_project"),
        CheckConstraint(f"status IN {STATUSES}", name="ck_change_request_status"),
        CheckConstraint(
            f"impact_class IS NULL OR impact_class IN {CLASSES}", name="ck_change_request_class"
        ),
        CheckConstraint(
            f"class_override IS NULL OR class_override IN {CLASSES}",
            name="ck_change_request_override",
        ),
        Index("ix_change_requests_project_status", "project_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(20), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    author: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    impact_class: Mapped[str | None] = mapped_column(String(20), nullable=True)
    impact: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    class_override: Mapped[str | None] = mapped_column(String(20), nullable=True)
    overridden_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    override_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # Source structure before a fast-path run, compared with the repository after a patch.
    baseline: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    drift: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON(none_as_null=True), nullable=True
    )
    proposed_requirement_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    removals_confirmed: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    pull_request_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
