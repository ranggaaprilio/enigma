import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

COMMAND_RUN_STATUSES = ("running", "completed", "failed", "error")
# Lifecycle role of a registry entry. ``custom`` is the catch-all existing commands keep.
COMMAND_ROLES = ("install", "migrate", "dev", "test", "lint", "seed", "custom")
# ``service`` (a long-running process with a port) is implemented in Epic 11; the column
# exists here because that epic builds on this schema.
COMMAND_KINDS = ("task", "service")


class CustomCommand(Base):
    __tablename__ = "custom_commands"
    __table_args__ = (
        CheckConstraint(
            "role in ('install', 'migrate', 'dev', 'test', 'lint', 'seed', 'custom')",
            name="ck_custom_commands_role",
        ),
        CheckConstraint("kind in ('task', 'service')", name="ck_custom_commands_kind"),
        CheckConstraint(
            "port is null or (port between 1 and 65535)", name="ck_custom_commands_port"
        ),
        CheckConstraint(
            "timeout_seconds is null or (timeout_seconds between 1 and 86400)",
            name="ck_custom_commands_timeout",
        ),
        Index("ix_custom_commands_workspace_id", "workspace_id"),
        Index("ix_custom_commands_created_at", "created_at"),
        Index("ix_custom_commands_workspace_sort", "workspace_id", "sort_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon: Mapped[str | None] = mapped_column(String(80), nullable=True)
    hotkey: Mapped[str | None] = mapped_column(String(80), nullable=True)
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    role: Mapped[str] = mapped_column(
        String(16), nullable=False, default="custom", server_default="custom"
    )
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="task", server_default="task"
    )
    # Relative to the resolved app root; ``None`` means the app root itself.
    cwd: Mapped[str | None] = mapped_column(Text, nullable=True)
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    # ``None`` falls back to Settings.command_run_timeout_seconds.
    timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
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

    runs: Mapped[list["CommandRun"]] = relationship(
        back_populates="custom_command",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class CommandRun(Base):
    __tablename__ = "command_runs"
    __table_args__ = (
        CheckConstraint(
            "status in ('running', 'completed', 'failed', 'error')",
            name="ck_command_runs_status",
        ),
        Index("ix_command_runs_custom_command_id", "custom_command_id"),
        Index("ix_command_runs_workspace_id", "workspace_id"),
        Index("ix_command_runs_user_id", "user_id"),
        Index("ix_command_runs_status", "status"),
        Index("ix_command_runs_created_at", "started_at"),
        Index("ix_command_runs_worktree_id", "worktree_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    custom_command_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("custom_commands.id", ondelete="CASCADE"),
        nullable=False,
    )
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
    # Which worktree the run targeted; ``None`` => the base clone.
    worktree_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspace_worktrees.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The resolved working directory, recorded so run history explains where it ran.
    cwd: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    exit_code: Mapped[int | None] = mapped_column(nullable=True)
    output: Mapped[str] = mapped_column(Text, nullable=False, default="")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    custom_command: Mapped[CustomCommand] = relationship(back_populates="runs")
