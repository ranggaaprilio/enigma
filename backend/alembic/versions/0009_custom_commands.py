"""Add custom commands and command runs.

Revision ID: 0009_custom_commands
Revises: 0008_preview_sessions
Create Date: 2026-05-25 00:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009_custom_commands"
down_revision: str | None = "0008_preview_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "custom_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("icon", sa.String(length=80), nullable=True),
        sa.Column("hotkey", sa.String(length=80), nullable=True),
        sa.Column("requires_confirmation", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_custom_commands_workspace_id",
        "custom_commands",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_custom_commands_created_at",
        "custom_commands",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "command_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("custom_command_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("output", sa.Text(), nullable=False),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status in ('running', 'completed', 'failed', 'error')",
            name="ck_command_runs_status",
        ),
        sa.ForeignKeyConstraint(
            ["custom_command_id"],
            ["custom_commands.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_command_runs_custom_command_id",
        "command_runs",
        ["custom_command_id"],
        unique=False,
    )
    op.create_index(
        "ix_command_runs_workspace_id",
        "command_runs",
        ["workspace_id"],
        unique=False,
    )
    op.create_index("ix_command_runs_user_id", "command_runs", ["user_id"], unique=False)
    op.create_index("ix_command_runs_status", "command_runs", ["status"], unique=False)
    op.create_index(
        "ix_command_runs_created_at",
        "command_runs",
        ["started_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_command_runs_created_at", table_name="command_runs")
    op.drop_index("ix_command_runs_status", table_name="command_runs")
    op.drop_index("ix_command_runs_user_id", table_name="command_runs")
    op.drop_index("ix_command_runs_workspace_id", table_name="command_runs")
    op.drop_index("ix_command_runs_custom_command_id", table_name="command_runs")
    op.drop_table("command_runs")
    op.drop_index("ix_custom_commands_created_at", table_name="custom_commands")
    op.drop_index("ix_custom_commands_workspace_id", table_name="custom_commands")
    op.drop_table("custom_commands")
