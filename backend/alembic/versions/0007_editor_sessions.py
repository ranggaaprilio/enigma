"""Add editor session table.

Revision ID: 0007_editor_sessions
Revises: 0006_terminal_sessions
Create Date: 2026-05-23 00:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_editor_sessions"
down_revision: str | None = "0006_terminal_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "editor_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("url", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("process_id", sa.Integer(), nullable=True),
        sa.Column("bind_host", sa.String(length=128), nullable=False),
        sa.Column("bind_port", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "started_at",
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
            name="ck_editor_sessions_status",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_editor_sessions_workspace_id",
        "editor_sessions",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_editor_sessions_user_id",
        "editor_sessions",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_editor_sessions_status",
        "editor_sessions",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_editor_sessions_last_active_at",
        "editor_sessions",
        ["last_active_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_editor_sessions_last_active_at", table_name="editor_sessions")
    op.drop_index("ix_editor_sessions_status", table_name="editor_sessions")
    op.drop_index("ix_editor_sessions_user_id", table_name="editor_sessions")
    op.drop_index("ix_editor_sessions_workspace_id", table_name="editor_sessions")
    op.drop_table("editor_sessions")
