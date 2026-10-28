import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.project import _utcnow

TASK_KINDS = "('setup', 'component', 'integration', 'security', 'data', 'test', 'nfr')"


class ImplementationPlan(Base):
    """Planning only: an ordered task list derived from one approved architecture version."""

    __tablename__ = "implementation_plans"
    __table_args__ = (
        ForeignKeyConstraint(
            ["proposal_id", "project_id"],
            ["architecture_proposals.id", "architecture_proposals.project_id"],
            ondelete="CASCADE",
            name="fk_plan_proposal",
        ),
        UniqueConstraint("project_id", "version", name="uq_plan_project_version"),
        # Target of the composite foreign key from plan_tasks.
        UniqueConstraint("id", "project_id", name="uq_plan_id_project"),
        CheckConstraint("status IN ('draft', 'approved', 'rejected')", name="ck_plan_status"),
        CheckConstraint("version >= 1", name="ck_plan_version"),
        Index("ix_plans_project_status", "project_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    proposal_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    source_graph_built_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Open questions that name no requirement shared with any task (so they block no task).
    unattached_questions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class PlanTask(Base):
    __tablename__ = "plan_tasks"
    __table_args__ = (
        # A task belongs to a plan of the same project.
        ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["implementation_plans.id", "implementation_plans.project_id"],
            ondelete="CASCADE",
            name="fk_plan_task_plan",
        ),
        UniqueConstraint("plan_id", "key", name="uq_plan_task_key"),
        UniqueConstraint("plan_id", "order_index", name="uq_plan_task_order"),
        CheckConstraint(f"kind IN {TASK_KINDS}", name="ck_plan_task_kind"),
        CheckConstraint("status IN ('todo')", name="ck_plan_task_status"),
        Index("ix_plan_tasks_plan", "plan_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    key: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    # Path of the architecture item the task came from, e.g. "components[0]".
    source_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    component_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    requirement_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    acceptance_criteria: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    depends_on: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    flags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    blocking_questions: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="todo")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    def as_json(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "kind": self.kind,
            "description": self.description,
            "source_ref": self.source_ref,
            "component_ref": self.component_ref,
            "requirement_ids": list(self.requirement_ids),
            "acceptance_criteria": list(self.acceptance_criteria),
            "depends_on": list(self.depends_on),
            "flags": list(self.flags),
            "blocking_questions": list(self.blocking_questions),
            "order_index": self.order_index,
            "status": self.status,
        }
