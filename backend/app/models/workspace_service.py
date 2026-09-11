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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# ``installing``/``starting``/``running`` mirror the dev-server vocabulary; ``exited`` is a
# service that ran and stopped on its own (distinct from an explicit ``stop``, which is
# ``stopped``); ``error`` covers a launch failure surfaced via ``last_error``.
WORKSPACE_SERVICE_STATUSES = ("installing", "starting", "running", "stopped", "exited", "error")

# A row in any of these statuses is "alive" for cap/port-conflict/env-change-impact purposes.
# Lives here (rather than in ``services/workspace_services.py``, which imports from
# ``services/workspace_env.py``) so ``workspace_env.py`` can use it without a circular import.
WORKSPACE_SERVICE_ACTIVE_STATUSES = ("installing", "starting", "running")

# Epic 13: readiness, orthogonal to ``status``. A row can be ``status="running"`` (port bound)
# and ``health_status="unhealthy"`` (returning 500) at the same time -- deliberately never folded
# into ``status``, so the preview session (and the ability to see the failure) survives.
WORKSPACE_SERVICE_HEALTH_STATUSES = ("unknown", "starting", "healthy", "unhealthy")


class WorkspaceService(Base):
    """A running (or desired-to-run) instance of a workspace's `dev`/service command.

    ``custom_command_id`` is ``None`` for the built-in mise ``dev`` service — the path a
    workspace with a mise config and an empty command registry still uses (Epic 10 dropped
    seeding a registry entry for it). Only one built-in row exists per workspace.
    """

    __tablename__ = "workspace_services"
    __table_args__ = (
        CheckConstraint(
            "status in ('installing', 'starting', 'running', 'stopped', 'exited', 'error')",
            name="ck_workspace_services_status",
        ),
        CheckConstraint(
            "health_status in ('unknown', 'starting', 'healthy', 'unhealthy')",
            name="ck_workspace_services_health_status",
        ),
        UniqueConstraint(
            "workspace_id", "custom_command_id", name="uq_workspace_services_workspace_command"
        ),
        # At most one built-in (custom_command_id IS NULL) row per workspace.
        Index(
            "uq_workspace_services_builtin",
            "workspace_id",
            unique=True,
            postgresql_where=text("custom_command_id IS NULL"),
        ),
        Index("ix_workspace_services_workspace_id", "workspace_id"),
        Index("ix_workspace_services_workspace_status", "workspace_id", "status"),
        Index("ix_workspace_services_worktree_id", "worktree_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    custom_command_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("custom_commands.id", ondelete="CASCADE"),
        nullable=True,
    )
    # Which worktree the process serves; ``None`` = base clone.
    worktree_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspace_worktrees.id", ondelete="SET NULL"),
        nullable=True,
    )
    port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="stopped")
    # Whether the user wants this service running; auto-resume only ever acts on desired rows.
    desired: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Epic 13: runtime readiness signal, set by the health probe
    # (``service_scripts.build_probe_script``) when the owning command declares a
    # ``health_path``; otherwise stays ``unknown`` forever.
    health_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unknown", server_default="unknown"
    )
    health_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
