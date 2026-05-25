"""Add preview session table.

Revision ID: 0008_preview_sessions
Revises: 0007_editor_sessions
Create Date: 2026-05-25 00:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_preview_sessions"
down_revision: str | None = "0007_editor_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "preview_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("public_url", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('active', 'stopped', 'error')",
            name="ck_preview_sessions_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_preview_sessions_workspace_id",
        "preview_sessions",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_preview_sessions_status",
        "preview_sessions",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_preview_sessions_last_seen_at",
        "preview_sessions",
        ["last_seen_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_preview_sessions_last_seen_at", table_name="preview_sessions")
    op.drop_index("ix_preview_sessions_status", table_name="preview_sessions")
    op.drop_index("ix_preview_sessions_workspace_id", table_name="preview_sessions")
    op.drop_table("preview_sessions")
