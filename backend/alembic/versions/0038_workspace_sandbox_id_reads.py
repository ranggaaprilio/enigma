"""workspace sandbox_id: reads move to the new column (expand, second half)

local-docker-runtime Epic 06. Backfills ``workspaces.sandbox_id`` once more: the release
before 0037 wrote only ``daytona_sandbox_id`` while it overlapped 0037's rollout. From this
release on, reads use ``sandbox_id``; both columns are still written, because the 0037
release -- still serving while this one rolls out -- reads ``daytona_sandbox_id``. The next
release (0039) drops the old column. Expand-only: one UPDATE.

Revision ID: 0038_workspace_sandbox_id_reads
Revises: 0037_workspace_sandbox_id
Create Date: 2026-10-30 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0038_workspace_sandbox_id_reads"
down_revision: str | None = "0037_workspace_sandbox_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE workspaces SET sandbox_id = daytona_sandbox_id "
        "WHERE daytona_sandbox_id IS NOT NULL "
        "AND sandbox_id IS DISTINCT FROM daytona_sandbox_id"
    )


def downgrade() -> None:
    # Nothing to undo: 0037's downgrade drops the column the backfill wrote.
    pass
