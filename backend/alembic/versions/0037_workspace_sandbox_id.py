"""workspace sandbox_id: the provider-neutral name for daytona_sandbox_id (expand)

local-docker-runtime Epic 06. Adds ``workspaces.sandbox_id`` with its index and backfills it.
This release writes both columns and still reads ``daytona_sandbox_id``, which the previous
release keeps writing on its own while old and new replicas overlap. The contract release
backfills once more, switches reads to ``sandbox_id`` and drops ``daytona_sandbox_id`` and
``ix_workspaces_daytona_sandbox_id``. Expand-only: a nullable column, an index, an UPDATE.

Revision ID: 0037_workspace_sandbox_id
Revises: 0036_sandbox_activity
Create Date: 2026-10-30 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0037_workspace_sandbox_id"
down_revision: str | None = "0036_sandbox_activity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("workspaces", sa.Column("sandbox_id", sa.String(length=160), nullable=True))
    op.create_index("ix_workspaces_sandbox_id", "workspaces", ["sandbox_id"])
    op.execute(
        "UPDATE workspaces SET sandbox_id = daytona_sandbox_id "
        "WHERE sandbox_id IS NULL AND daytona_sandbox_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_index("ix_workspaces_sandbox_id", table_name="workspaces")
    op.drop_column("workspaces", "sandbox_id")
