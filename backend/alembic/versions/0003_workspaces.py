"""Add workspace management tables.

Revision ID: 0003_workspaces
Revises: 0002_auth_users
Create Date: 2026-05-23 00:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_workspaces"
down_revision: str | None = "0002_auth_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("slug", sa.String(length=180), nullable=False),
        sa.Column("repo_url", sa.Text(), nullable=False),
        sa.Column("default_branch", sa.String(length=120), nullable=False),
        sa.Column("local_path", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
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
        sa.CheckConstraint(
            "status in ('creating', 'ready', 'busy', 'error', 'stopped')",
            name="ck_workspaces_status",
        ),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", name="uq_workspaces_owner_id"),
    )
    op.create_index("ix_workspaces_owner_id", "workspaces", ["owner_id"], unique=False)
    op.create_index("ix_workspaces_slug", "workspaces", ["slug"], unique=False)
    op.create_index("ix_workspaces_status", "workspaces", ["status"], unique=False)

    op.create_table(
        "workspace_env_vars",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("encrypted_value", sa.Text(), nullable=False),
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
        sa.UniqueConstraint("workspace_id", "key", name="uq_workspace_env_vars_workspace_key"),
    )
    op.create_index(
        "ix_workspace_env_vars_workspace_id",
        "workspace_env_vars",
        ["workspace_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_env_vars_workspace_id", table_name="workspace_env_vars")
    op.drop_table("workspace_env_vars")
    op.drop_index("ix_workspaces_status", table_name="workspaces")
    op.drop_index("ix_workspaces_slug", table_name="workspaces")
    op.drop_index("ix_workspaces_owner_id", table_name="workspaces")
    op.drop_table("workspaces")
