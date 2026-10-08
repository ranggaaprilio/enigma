import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

AGENT_RUN_STATUSES = ("thinking", "executing", "completed", "error", "stopped")
AGENT_EVENT_TYPES = ("status", "output_text", "tool_call", "tool_result", "error")
AGENT_FILE_CHANGE_STATUSES = ("created", "modified", "deleted", "renamed")
AGENT_SESSION_STATUSES = ("active", "archived")


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    __table_args__ = (
        CheckConstraint(
            "status in ('active', 'archived')",
            name="ck_agent_sessions_status",
        ),
        Index("ix_agent_sessions_workspace_id", "workspace_id"),
        Index("ix_agent_sessions_user_id", "user_id"),
        Index("ix_agent_sessions_status", "status"),
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
    title: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
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

    # No cascade delete: session_id uses ON DELETE SET NULL, so turns (runs) and their
    # events/file-changes survive deletion of the grouping session.
    runs: Mapped[list["AgentRun"]] = relationship(back_populates="session")


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        CheckConstraint(
            "status in ('thinking', 'executing', 'completed', 'error', 'stopped')",
            name="ck_agent_runs_status",
        ),
        Index("ix_agent_runs_workspace_id", "workspace_id"),
        Index("ix_agent_runs_user_id", "user_id"),
        Index("ix_agent_runs_status", "status"),
        Index("ix_agent_runs_created_at", "started_at"),
        Index("ix_agent_runs_session_id", "session_id"),
        Index("ix_agent_runs_status_workspace_id", "status", "workspace_id"),
        # One active run per workspace, enforced by Postgres so concurrent API replicas cannot
        # both start one (the in-process RunLock only serialises a single process).
        Index(
            "uq_agent_runs_one_active_per_workspace",
            "workspace_id",
            unique=True,
            postgresql_where=text("status IN ('thinking', 'executing')"),
        ),
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
    worktree_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspace_worktrees.id", ondelete="SET NULL"),
        nullable=True,
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="thinking")
    provider: Mapped[str] = mapped_column(String(80), nullable=False)
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    tokens_used: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Stable code for why the run ended in `error` (e.g. "interrupted_by_restart").
    error_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Where the agent's tools run (a worktree or spec-worktree path); persisted so a queued job,
    # which only carries the run id, can recover it.
    working_dir: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Queue bookkeeping (Epic 02). `claimed_at` is set exactly once, by the worker that wins the
    # conditional claim; `started_at` remains the row's creation time.
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    session: Mapped["AgentSession | None"] = relationship(back_populates="runs")
    events: Mapped[list["AgentEvent"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    file_changes: Mapped[list["AgentFileChange"]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class AgentEvent(Base):
    __tablename__ = "agent_events"
    __table_args__ = (
        CheckConstraint(
            "event_type in ('status', 'output_text', 'tool_call', 'tool_result', 'error')",
            name="ck_agent_events_event_type",
        ),
        Index("ix_agent_events_agent_run_id", "agent_run_id"),
        Index("ix_agent_events_created_at", "created_at"),
        Index("ix_agent_events_tool_call_id", "tool_call_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_metadata: Mapped[dict] = mapped_column("metadata", JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    run: Mapped[AgentRun] = relationship(back_populates="events")


class AgentFileChange(Base):
    __tablename__ = "agent_file_changes"
    __table_args__ = (
        CheckConstraint(
            "status in ('created', 'modified', 'deleted', 'renamed')",
            name="ck_agent_file_changes_status",
        ),
        Index("ix_agent_file_changes_agent_run_id", "agent_run_id"),
        Index("ix_agent_file_changes_tool_call_id", "tool_call_id"),
        Index("ix_agent_file_changes_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    tool_call_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    run: Mapped[AgentRun] = relationship(back_populates="file_changes")
