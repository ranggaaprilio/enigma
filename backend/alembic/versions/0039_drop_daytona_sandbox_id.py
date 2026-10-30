"""workspace sandbox_id: drop daytona_sandbox_id (contract)

local-docker-runtime Epic 06, the contract half of 0037/0038. Ships one release after 0038:
from 0038 on every replica reads ``sandbox_id``, so nothing deployed reads the old column.
Uniqueness moves with the data: ``ix_workspaces_daytona_sandbox_id`` was unique, so the
``sandbox_id`` index becomes unique before the old column and its index go.

Revision ID: 0039_drop_daytona_sandbox_id
Revises: 0038_workspace_sandbox_id_reads
Create Date: 2026-10-30 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0039_drop_daytona_sandbox_id"
down_revision: str | None = "0038_workspace_sandbox_id_reads"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # contract: since 0038 no deployed replica reads daytona_sandbox_id, and every one writes
    # sandbox_id, so dropping the old column and making the new index unique (the old one was)
    # breaks nothing that is still running. Ship only once 0038 is live everywhere.
    op.execute(
        "UPDATE workspaces SET sandbox_id = daytona_sandbox_id "
        "WHERE daytona_sandbox_id IS NOT NULL "
        "AND sandbox_id IS DISTINCT FROM daytona_sandbox_id"
    )
    op.drop_index("ix_workspaces_sandbox_id", table_name="workspaces")
    op.create_index("ix_workspaces_sandbox_id", "workspaces", ["sandbox_id"], unique=True)
    op.drop_index("ix_workspaces_daytona_sandbox_id", table_name="workspaces")
    op.drop_column("workspaces", "daytona_sandbox_id")


def downgrade() -> None:
    op.add_column(
        "workspaces", sa.Column("daytona_sandbox_id", sa.String(length=160), nullable=True)
    )
    op.execute("UPDATE workspaces SET daytona_sandbox_id = sandbox_id")
    op.create_index(
        "ix_workspaces_daytona_sandbox_id", "workspaces", ["daytona_sandbox_id"], unique=True
    )
    op.drop_index("ix_workspaces_sandbox_id", table_name="workspaces")
    op.create_index("ix_workspaces_sandbox_id", "workspaces", ["sandbox_id"])
