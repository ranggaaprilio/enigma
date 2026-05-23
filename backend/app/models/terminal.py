import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

TERMINAL_SESSION_STATUSES = ("active", "stopped", "error")


class TerminalSession(Base):
    __tablename__ = "terminal_sessions"
    __table_args__ = (
        CheckConstraint(
            "status in ('active', 'stopped', 'error')",
            name="ck_terminal_sessions_status",
        ),
        Index("ix_terminal_sessions_workspace_id", "workspace_id"),
        Index("ix_terminal_sessions_user_id", "user_id"),
        Index("ix_terminal_sessions_status", "status"),
        Index("ix_terminal_sessions_last_active_at", "last_active_at"),
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
    name: Mapped[str] = mapped_column(String(160), nullable=False, default="Terminal")
    tmux_session_name: Mapped[str] = mapped_column(String(160), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
