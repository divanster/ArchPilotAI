import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.project import _utcnow

CHECKLIST_STATUSES = ("proposed", "approved", "rejected", "superseded")


class TaskChecklist(Base):
    """One version of the coding checklist of a plan task (Phase 13D).

    Only a person approves a checklist; once approved it is read-only. The status of each step
    is derived from its agent runs and their patches, never stored.
    """

    __tablename__ = "task_checklists"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["implementation_plans.id", "implementation_plans.project_id"],
            ondelete="CASCADE",
            name="fk_checklist_plan",
        ),
        ForeignKeyConstraint(
            ["task_id", "project_id"],
            ["plan_tasks.id", "plan_tasks.project_id"],
            ondelete="CASCADE",
            name="fk_checklist_task",
        ),
        UniqueConstraint("task_id", "version", name="uq_checklist_task_version"),
        UniqueConstraint("id", "project_id", name="uq_checklist_id_project"),
        CheckConstraint(f"status IN {CHECKLIST_STATUSES}", name="ck_checklist_status"),
        CheckConstraint("version >= 1", name="ck_checklist_version"),
        Index("ix_task_checklists_project_task", "project_id", "task_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    task_key: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="proposed")
    # Ordered steps (domain.checklist.Step.as_json), dependencies first.
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    warnings: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    # Criteria a person verifies instead of a coding step: [{"criterion", "reason"}].
    manual_verification: Mapped[list[dict[str, str]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    # "system" (deterministic build), "agent" (a revision from the model) or a person's name.
    proposed_by: Mapped[str] = mapped_column(String(100), nullable=False)
    proposal_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
