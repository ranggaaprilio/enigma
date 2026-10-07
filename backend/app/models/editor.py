import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

EDITOR_SESSION_STATUSES = ("active", "stopped", "error")


class EditorSession(Base):
    __tablename__ = "editor_sessions"
    __table_args__ = (
        CheckConstraint(
            "status in ('active', 'stopped', 'error')",
            name="ck_editor_sessions_status",
        ),
        Index("ix_editor_sessions_workspace_id", "workspace_id"),
        Index("ix_editor_sessions_user_id", "user_id"),
        Index("ix_editor_sessions_status", "status"),
        Index("ix_editor_sessions_last_active_at", "last_active_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    process_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bind_host: Mapped[str] = mapped_column(String(128), nullable=False)
    bind_port: Mapped[int] = mapped_column(Integer, nullable=False)
    # Legacy of the Daytona signed-URL model. The editor URL is now a stable alias host with no
    # credential in it, so neither column is written any more.
    token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Which worktree code-server was opened on (NULL = base clone) and the resolved absolute
    # sandbox path handed to it as `?folder=`.
    worktree_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspace_worktrees.id", ondelete="SET NULL"),
        nullable=True,
    )
    folder: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
