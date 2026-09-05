"""workspace_services: multi-service preview (Epic 11)

Revision ID: 0025_workspace_services
Revises: 0024_run_configuration
Create Date: 2026-09-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0025_workspace_services"
down_revision: str | None = "0024_run_configuration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WORKSPACE_SERVICE_STATUSES = "'installing', 'starting', 'running', 'stopped', 'exited', 'error'"

# Seeds one workspace_services row per workspace that was auto-starting a dev server before
# this migration. custom_command_id is the workspace's first enabled `role='dev'` registry
# command (mirrors DevServerService._launch_run's own precedence, so ensure_running does not
# relaunch `mise run dev` next to a still-running registry-driven server); NULL when no such
# command exists, which becomes the built-in mise service row.
_SEED_WORKSPACE_SERVICES = """
INSERT INTO workspace_services (
    id, workspace_id, custom_command_id, worktree_id, port, status, desired,
    created_at, updated_at
)
SELECT
    gen_random_uuid(), w.id, cmd.id, w.dev_worktree_id, cmd.port, 'stopped', true, now(), now()
FROM workspaces AS w
LEFT JOIN LATERAL (
    SELECT c.id, c.port
    FROM custom_commands AS c
    WHERE c.workspace_id = w.id AND c.role = 'dev' AND c.enabled = true
    ORDER BY c.sort_order, c.created_at
    LIMIT 1
) AS cmd ON true
WHERE w.dev_autostart = true
"""

# Keeps the most-recently-seen active session per (workspace, port); the rest are marked
# stopped so the new partial unique index below has something to enforce against.
_DEDUPE_ACTIVE_PREVIEW_SESSIONS = """
WITH ranked AS (
    SELECT id,
           row_number() OVER (
               PARTITION BY workspace_id, port
               ORDER BY last_seen_at DESC, created_at DESC
           ) AS rn
    FROM preview_sessions
    WHERE status = 'active'
)
UPDATE preview_sessions AS p
SET status = 'stopped', public_url = ''
FROM ranked AS r
WHERE p.id = r.id AND r.rn > 1
"""

# Best-effort downgrade backfill: pick the desired row that names a registry command over the
# built-in one (mirrors the seed's own preference), so a rollback restores the single-slot
# columns close to how they looked before the upgrade.
_BACKFILL_DEV_AUTOSTART = """
UPDATE workspaces AS w
SET dev_autostart = EXISTS (
        SELECT 1 FROM workspace_services AS s WHERE s.workspace_id = w.id AND s.desired = true
    ),
    dev_worktree_id = (
        SELECT s.worktree_id
        FROM workspace_services AS s
        WHERE s.workspace_id = w.id AND s.desired = true
        ORDER BY s.custom_command_id NULLS LAST
        LIMIT 1
    )
"""


def upgrade() -> None:
    # 1. workspace_services
    op.create_table(
        "workspace_services",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "custom_command_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("custom_commands.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "worktree_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_worktrees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("port", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="stopped"),
        sa.Column("desired", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            f"status in ({_WORKSPACE_SERVICE_STATUSES})",
            name="ck_workspace_services_status",
        ),
        sa.UniqueConstraint(
            "workspace_id", "custom_command_id", name="uq_workspace_services_workspace_command"
        ),
    )
    op.create_index(
        "ix_workspace_services_workspace_id", "workspace_services", ["workspace_id"]
    )
    op.create_index(
        "ix_workspace_services_workspace_status", "workspace_services", ["workspace_id", "status"]
    )
    op.create_index(
        "ix_workspace_services_worktree_id", "workspace_services", ["worktree_id"]
    )
    # At most one built-in (custom_command_id IS NULL) row per workspace.
    op.create_index(
        "uq_workspace_services_builtin",
        "workspace_services",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("custom_command_id IS NULL"),
    )

    # 2. custom_commands.kind: a `dev`-role command becomes a service definition. Nothing read
    #    `kind` before this epic, so this is lossless.
    op.execute(sa.text("UPDATE custom_commands SET kind = 'service' WHERE role = 'dev'"))

    # 3. seed one workspace_services row per workspace that was already auto-starting a server.
    op.execute(sa.text(_SEED_WORKSPACE_SERVICES))

    # 4. preview_sessions.service_id -- which service a preview belongs to.
    op.add_column(
        "preview_sessions",
        sa.Column("service_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_preview_sessions_service_id",
        "preview_sessions",
        "workspace_services",
        ["service_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_preview_sessions_service_id", "preview_sessions", ["service_id"])

    # 5. at most one active preview session per (workspace, port), so two services can never
    #    race each other onto the same port.
    op.execute(sa.text(_DEDUPE_ACTIVE_PREVIEW_SESSIONS))
    op.create_index(
        "uq_preview_sessions_active_port",
        "preview_sessions",
        ["workspace_id", "port"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    # 5. active-port uniqueness
    op.drop_index("uq_preview_sessions_active_port", table_name="preview_sessions")

    # 4. preview_sessions.service_id
    op.drop_index("ix_preview_sessions_service_id", table_name="preview_sessions")
    op.drop_constraint(
        "fk_preview_sessions_service_id", "preview_sessions", type_="foreignkey"
    )
    op.drop_column("preview_sessions", "service_id")

    # 3 & 1. restore the single-slot columns from workspace_services before dropping it.
    op.execute(sa.text(_BACKFILL_DEV_AUTOSTART))
    op.drop_index("uq_workspace_services_builtin", table_name="workspace_services")
    op.drop_index("ix_workspace_services_worktree_id", table_name="workspace_services")
    op.drop_index("ix_workspace_services_workspace_status", table_name="workspace_services")
    op.drop_index("ix_workspace_services_workspace_id", table_name="workspace_services")
    op.drop_table("workspace_services")

    # 2. custom_commands.kind
    op.execute(sa.text("UPDATE custom_commands SET kind = 'task' WHERE role = 'dev'"))
