"""Move workspace runtime metadata to Daytona.

Revision ID: 0011_daytona_runtime
Revises: 0010_notifications_and_audit
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011_daytona_runtime"
down_revision: str | None = "0010_notifications_and_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("runtime", sa.String(length=32), nullable=False, server_default="daytona"),
    )
    op.add_column(
        "workspaces",
        sa.Column("daytona_sandbox_id", sa.String(length=160), nullable=True),
    )
    op.add_column("workspaces", sa.Column("snapshot", sa.String(length=255), nullable=True))
    op.add_column(
        "workspaces",
        sa.Column("workspace_path", sa.Text(), nullable=False, server_default="/workspace"),
    )
    op.create_index(
        "ix_workspaces_daytona_sandbox_id",
        "workspaces",
        ["daytona_sandbox_id"],
        unique=True,
    )
    op.alter_column(
        "terminal_sessions",
        "tmux_session_name",
        new_column_name="pty_session_id",
        existing_type=sa.String(length=160),
    )
    op.drop_column("workspaces", "local_path")


def downgrade() -> None:
    op.add_column("workspaces", sa.Column("local_path", sa.Text(), nullable=True))
    op.alter_column(
        "terminal_sessions",
        "pty_session_id",
        new_column_name="tmux_session_name",
        existing_type=sa.String(length=160),
    )
    op.drop_index("ix_workspaces_daytona_sandbox_id", table_name="workspaces")
    op.drop_column("workspaces", "workspace_path")
    op.drop_column("workspaces", "snapshot")
    op.drop_column("workspaces", "daytona_sandbox_id")
    op.drop_column("workspaces", "runtime")
