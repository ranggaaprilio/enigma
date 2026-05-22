"""Add repository credentials.

Revision ID: 0004_repository_credentials
Revises: 0003_workspaces
Create Date: 2026-05-23 00:00:00.000000
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_repository_credentials"
down_revision: str | None = "0003_workspaces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "repository_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.String(length=40), nullable=False),
        sa.Column("auth_type", sa.String(length=80), nullable=False),
        sa.Column("encrypted_token", sa.Text(), nullable=False),
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
        sa.UniqueConstraint("workspace_id", name="uq_repository_credentials_workspace_id"),
    )
    op.create_index(
        "ix_repository_credentials_workspace_id",
        "repository_credentials",
        ["workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_repository_credentials_provider",
        "repository_credentials",
        ["provider"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_repository_credentials_provider", table_name="repository_credentials")
    op.drop_index("ix_repository_credentials_workspace_id", table_name="repository_credentials")
    op.drop_table("repository_credentials")
