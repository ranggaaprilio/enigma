"""Add terminal session table.

Revision ID: 0006_terminal_sessions
Revises: 0005_agent_runs
Create Date: 2026-05-23 00:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_terminal_sessions"
down_revision: str | None = "0005_agent_runs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "terminal_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("tmux_session_name", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('active', 'stopped', 'error')",
            name="ck_terminal_sessions_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_terminal_sessions_workspace_id",
        "terminal_sessions",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_terminal_sessions_user_id",
        "terminal_sessions",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_terminal_sessions_status",
        "terminal_sessions",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_terminal_sessions_last_active_at",
        "terminal_sessions",
        ["last_active_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_terminal_sessions_last_active_at", table_name="terminal_sessions")
    op.drop_index("ix_terminal_sessions_status", table_name="terminal_sessions")
    op.drop_index("ix_terminal_sessions_user_id", table_name="terminal_sessions")
    op.drop_index("ix_terminal_sessions_workspace_id", table_name="terminal_sessions")
    op.drop_table("terminal_sessions")
