import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.project import _utcnow


class GitRemoteSettings(Base):
    """One external Git repository per project. Holds no secret: only a credential-store
    reference to the personal access token, its provider and its last four characters."""

    __tablename__ = "git_remote_settings"
    __table_args__ = (
        CheckConstraint(
            "provider IN ('github', 'github_enterprise', 'azure_devops', 'gitlab', 'generic')",
            name="ck_git_remote_provider",
        ),
        CheckConstraint(
            "auth_method IN ('credential_manager', 'ssh', 'pat')", name="ck_git_remote_auth"
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    remote_url: Mapped[str] = mapped_column(String(500), nullable=False)
    # Relative to ARCHPILOT_WORKSPACE_ROOT.
    clone_folder: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    base_branch: Mapped[str] = mapped_column(String(100), nullable=False, default="main")
    branch_prefix: Mapped[str] = mapped_column(String(50), nullable=False, default="agent/")
    author_name: Mapped[str] = mapped_column(String(100), nullable=False)
    author_email: Mapped[str] = mapped_column(String(200), nullable=False)
    auth_method: Mapped[str] = mapped_column(
        String(20), nullable=False, default="credential_manager"
    )
    pat_ref: Mapped[str | None] = mapped_column(String(300), nullable=True)
    pat_provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    pat_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
