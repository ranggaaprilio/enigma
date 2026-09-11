import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

WORKSPACE_STATUSES = ("creating", "ready", "busy", "error", "stopped")
# How the workspace's repository came to be: cloned from an existing remote, scaffolded from a
# ``workspace_templates`` row, or ``git init``-ed empty. Template/blank workspaces have no
# ``repo_url`` until the user connects a remote.
WORKSPACE_SOURCES = ("repository", "template", "blank")


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint(
            "status in ('creating', 'ready', 'busy', 'error', 'stopped')",
            name="ck_workspaces_status",
        ),
        CheckConstraint(
            "source in ('repository', 'template', 'blank')",
            name="ck_workspaces_source",
        ),
        Index("ix_workspaces_owner_id", "owner_id"),
        Index("ix_workspaces_template_id", "template_id"),
        Index("ix_workspaces_slug", "slug"),
        Index("ix_workspaces_status", "status"),
        Index("ix_workspaces_team_id", "team_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("teams.id", ondelete="SET NULL"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(180), nullable=False)
    repo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(
        String(20), nullable=False, default="repository", server_default="repository"
    )
    template_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspace_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    default_branch: Mapped[str] = mapped_column(String(120), nullable=False, default="main")
    runtime: Mapped[str] = mapped_column(String(32), nullable=False, default="daytona")
    daytona_sandbox_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    workspace_path: Mapped[str] = mapped_column(Text, nullable=False, default="workspace")
    # Repo-relative subdirectory the workspace's commands and dev server run in (e.g.
    # "apps/api"); ``None`` => the checkout root. Validated server-side, never absolute.
    app_root: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Repo-relative path (below app_root) where the resolved env vars are materialised as a
    # .env file before each service launch. ``None`` => no file is written.
    env_file_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Epic 13: opt-in stable preview aliases (workspace_preview_aliases). The capability-host
    # scheme stays the default and always works; enabling this adds a second, weaker-posture
    # hostname per service that survives capability rotation.
    preview_alias_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="creating")
    dev_autostart: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
    # Which worktree the current dev server was launched in (NULL = base clone).
    dev_worktree_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspace_worktrees.id", ondelete="SET NULL"),
        nullable=True,
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

    @property
    def sandbox_id(self) -> str | None:
        return self.daytona_sandbox_id


# "workspace" => applies to every service in the workspace; "service" => only the service
# launched from ``custom_command_id``, overriding a workspace-wide value of the same key.
WORKSPACE_ENV_VAR_SCOPES = ("workspace", "service")


class WorkspaceEnvVar(Base):
    __tablename__ = "workspace_env_vars"
    __table_args__ = (
        CheckConstraint(
            "scope in ('workspace', 'service')",
            name="ck_workspace_env_vars_scope",
        ),
        CheckConstraint(
            "(scope = 'workspace' AND custom_command_id IS NULL) OR "
            "(scope = 'service' AND custom_command_id IS NOT NULL)",
            name="ck_workspace_env_vars_scope_target",
        ),
        # At most one workspace-wide row per (workspace, key).
        Index(
            "uq_workspace_env_vars_workspace_key_global",
            "workspace_id",
            "key",
            unique=True,
            postgresql_where=text("custom_command_id IS NULL"),
        ),
        # At most one service-scoped row per (workspace, key, custom_command_id).
        Index(
            "uq_workspace_env_vars_workspace_key_command",
            "workspace_id",
            "key",
            "custom_command_id",
            unique=True,
        ),
        Index("ix_workspace_env_vars_workspace_id", "workspace_id"),
        Index("ix_workspace_env_vars_custom_command_id", "custom_command_id"),
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
    scope: Mapped[str] = mapped_column(
        String(16), nullable=False, default="workspace", server_default="workspace"
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
