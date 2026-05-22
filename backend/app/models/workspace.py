import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

WORKSPACE_STATUSES = ("creating", "ready", "busy", "error", "stopped")


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint(
            "status in ('creating', 'ready', 'busy', 'error', 'stopped')",
            name="ck_workspaces_status",
        ),
        UniqueConstraint("owner_id", name="uq_workspaces_owner_id"),
        Index("ix_workspaces_owner_id", "owner_id"),
        Index("ix_workspaces_slug", "slug"),
        Index("ix_workspaces_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(180), nullable=False)
    repo_url: Mapped[str] = mapped_column(Text, nullable=False)
    default_branch: Mapped[str] = mapped_column(String(120), nullable=False, default="main")
    local_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="creating")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class WorkspaceEnvVar(Base):
    __tablename__ = "workspace_env_vars"
    __table_args__ = (
        UniqueConstraint("workspace_id", "key", name="uq_workspace_env_vars_workspace_key"),
        Index("ix_workspace_env_vars_workspace_id", "workspace_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    encrypted_value: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
