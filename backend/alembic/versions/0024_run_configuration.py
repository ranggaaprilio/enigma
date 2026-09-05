"""workspace app root and the unified run-command registry

Revision ID: 0024_run_configuration
Revises: 0023_spec_workflow_slug_unique
Create Date: 2026-09-05 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0024_run_configuration"
down_revision: str | None = "0023_spec_workflow_slug_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COMMAND_ROLES = "'install', 'migrate', 'dev', 'test', 'lint', 'seed', 'custom'"
_COMMAND_KINDS = "'task', 'service'"

# Existing rows are spaced by 10 so a later insert can be slotted between two commands
# without rewriting the whole workspace's ordering.
_BACKFILL_SORT_ORDER = """
UPDATE custom_commands AS c
SET sort_order = s.rn
FROM (
    SELECT id,
           (row_number() OVER (PARTITION BY workspace_id ORDER BY created_at, id)) * 10 AS rn
    FROM custom_commands
) AS s
WHERE c.id = s.id
"""


def upgrade() -> None:
    # 1. workspaces.app_root -- the repo-relative subdirectory a workspace's commands run in.
    op.add_column("workspaces", sa.Column("app_root", sa.Text(), nullable=True))

    # 2. custom_commands becomes the single registry of runnable things. Every column carries a
    #    server_default so existing rows keep behaving exactly as they did before this migration.
    op.add_column(
        "custom_commands",
        sa.Column("role", sa.String(length=16), nullable=False, server_default="custom"),
    )
    op.add_column(
        "custom_commands",
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="task"),
    )
    op.add_column("custom_commands", sa.Column("cwd", sa.Text(), nullable=True))
    op.add_column("custom_commands", sa.Column("port", sa.Integer(), nullable=True))
    op.add_column(
        "custom_commands",
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "custom_commands",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )
    op.add_column("custom_commands", sa.Column("timeout_seconds", sa.Integer(), nullable=True))

    op.create_check_constraint(
        "ck_custom_commands_role", "custom_commands", f"role in ({_COMMAND_ROLES})"
    )
    op.create_check_constraint(
        "ck_custom_commands_kind", "custom_commands", f"kind in ({_COMMAND_KINDS})"
    )
    op.create_check_constraint(
        "ck_custom_commands_port",
        "custom_commands",
        "port is null or (port between 1 and 65535)",
    )
    op.create_check_constraint(
        "ck_custom_commands_timeout",
        "custom_commands",
        "timeout_seconds is null or (timeout_seconds between 1 and 86400)",
    )

    # 3. preserve today's ordering (list_commands ordered by created_at) as explicit sort_order
    op.execute(sa.text(_BACKFILL_SORT_ORDER))
    op.create_index(
        "ix_custom_commands_workspace_sort", "custom_commands", ["workspace_id", "sort_order"]
    )

    # 4. command_runs records which worktree and directory a run actually targeted
    op.add_column(
        "command_runs",
        sa.Column(
            "worktree_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_worktrees.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column("command_runs", sa.Column("cwd", sa.Text(), nullable=True))
    op.create_index("ix_command_runs_worktree_id", "command_runs", ["worktree_id"])


def downgrade() -> None:
    # 4. command_runs
    op.drop_index("ix_command_runs_worktree_id", table_name="command_runs")
    op.drop_column("command_runs", "cwd")
    op.drop_column("command_runs", "worktree_id")

    # 3. ordering index
    op.drop_index("ix_custom_commands_workspace_sort", table_name="custom_commands")

    # 2. custom_commands registry columns
    op.drop_constraint("ck_custom_commands_timeout", "custom_commands", type_="check")
    op.drop_constraint("ck_custom_commands_port", "custom_commands", type_="check")
    op.drop_constraint("ck_custom_commands_kind", "custom_commands", type_="check")
    op.drop_constraint("ck_custom_commands_role", "custom_commands", type_="check")
    op.drop_column("custom_commands", "timeout_seconds")
    op.drop_column("custom_commands", "enabled")
    op.drop_column("custom_commands", "sort_order")
    op.drop_column("custom_commands", "port")
    op.drop_column("custom_commands", "cwd")
    op.drop_column("custom_commands", "kind")
    op.drop_column("custom_commands", "role")

    # 1. workspaces
    op.drop_column("workspaces", "app_root")
