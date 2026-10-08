"""agent_events stream index: (agent_run_id, created_at, id) for ordered backfill

Revision ID: 0032_agent_events_stream_index
Revises: 0031_run_job_queue
Create Date: 2026-10-08 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0032_agent_events_stream_index"
down_revision: str | None = "0031_run_job_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_agent_events_run_created_id",
        "agent_events",
        ["agent_run_id", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_events_run_created_id", table_name="agent_events")
