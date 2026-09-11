import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

PREVIEW_SESSION_STATUSES = ("active", "stopped", "error")


class PreviewSession(Base):
    __tablename__ = "preview_sessions"
    __table_args__ = (
        CheckConstraint(
            "status in ('active', 'stopped', 'error')",
            name="ck_preview_sessions_status",
        ),
        # At most one active session per (workspace, port) so two services can never race
        # each other onto the same preview port.
        Index(
            "uq_preview_sessions_active_port",
            "workspace_id",
            "port",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_preview_sessions_workspace_id", "workspace_id"),
        Index("ix_preview_sessions_status", "status"),
        Index("ix_preview_sessions_last_seen_at", "last_seen_at"),
        Index("ix_preview_sessions_worktree_id", "worktree_id"),
        Index("ix_preview_sessions_service_id", "service_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Which worktree this preview belongs to (NULL = base clone).
    worktree_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspace_worktrees.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Which service this preview was detected for (NULL = detected via the legacy scan,
    # before Epic 11's per-service tracking, or a workspace with no service rows yet).
    service_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspace_services.id", ondelete="SET NULL"),
        nullable=True,
    )
    port: Mapped[int] = mapped_column(Integer, nullable=False)
    public_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
