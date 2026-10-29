import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
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


class AgentRun(Base):
    """One bounded agent loop for one plan task. Its result is an ordinary proposed patch.

    The draft workspace of a run is a private copy; nothing reaches the accepted workspace or the
    disk until a person accepts the resulting patch (and exports the workspace).
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "project_id"],
            ["implementation_plans.id", "implementation_plans.project_id"],
            ondelete="CASCADE",
            name="fk_agent_run_plan",
        ),
        ForeignKeyConstraint(
            ["task_id", "project_id"],
            ["plan_tasks.id", "plan_tasks.project_id"],
            ondelete="CASCADE",
            name="fk_agent_run_task",
        ),
        ForeignKeyConstraint(
            ["patch_id", "project_id"],
            ["code_patches.id", "code_patches.project_id"],
            ondelete="CASCADE",
            name="fk_agent_run_patch",
        ),
        UniqueConstraint("id", "project_id", name="uq_agent_run_id_project"),
        CheckConstraint(
            "status IN ('running', 'finished', 'stopped', 'failed')", name="ck_agent_run_status"
        ),
        Index("ix_agent_runs_project_task", "project_id", "task_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    task_key: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    # Why the run ended: finished, user_stop, step_limit, token_budget, time_limit,
    # repeated_error, provider_error, interrupted, internal_error.
    stop_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    stop_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    blocking_acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False)
    token_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    time_limit_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    steps_taken: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    model: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    summary: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    patch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AgentStep(Base):
    """One action of a run and what the server answered. The full action log.

    File contents are never stored here (only sizes): they live in the draft, and a rejected
    write must not leave its content behind.
    """

    __tablename__ = "agent_steps"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "project_id"],
            ["agent_runs.id", "agent_runs.project_id"],
            ondelete="CASCADE",
            name="fk_agent_step_run",
        ),
        UniqueConstraint("run_id", "step_no", name="uq_agent_step_no"),
        CheckConstraint("status IN ('ok', 'error')", name="ck_agent_step_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    path: Mapped[str | None] = mapped_column(String(300), nullable=True)
    args: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    result_summary: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    # The result exactly as sent back to the model (bounded).
    result: Mapped[str] = mapped_column(Text, nullable=False, default="")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class AgentJob(Base):
    """One execution-provider command of a run: id, timeout, outcome and the full output log.

    `summary` is what was sent to the model (bounded); `output` is the whole log, never sent.
    """

    __tablename__ = "agent_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "project_id"],
            ["agent_runs.id", "agent_runs.project_id"],
            ondelete="CASCADE",
            name="fk_agent_job_run",
        ),
        CheckConstraint(
            "status IN ('running', 'done', 'timed_out', 'cancelled', 'unavailable')",
            name="ck_agent_job_status",
        ),
        Index("ix_agent_jobs_run", "run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    command_id: Mapped[str] = mapped_column(String(20), nullable=False)
    variant: Mapped[str] = mapped_column(String(20), nullable=False, default="changed")
    select_test: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    output: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class AgentDraftFile(Base):
    """One file of a run's draft workspace (a copy of the accepted workspace taken at the start)."""

    __tablename__ = "agent_draft_files"
    __table_args__ = (
        ForeignKeyConstraint(
            ["run_id", "project_id"],
            ["agent_runs.id", "agent_runs.project_id"],
            ondelete="CASCADE",
            name="fk_agent_draft_run",
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    path: Mapped[str] = mapped_column(String(300), primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # SHA-256 of the accepted file this draft file started from; null for files created in the run.
    base_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
