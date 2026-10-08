"""run job queue: single-active-run index + queue bookkeeping columns

Revision ID: 0031_run_job_queue
Revises: 0030_run_interruption
Create Date: 2026-10-08 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0031_run_job_queue"
down_revision: str | None = "0030_run_interruption"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_queue_columns(table: str) -> None:
    op.add_column(table, sa.Column("enqueued_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(table, sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(table, sa.Column("worker_id", sa.String(length=128), nullable=True))
    op.add_column(
        table, sa.Column("attempt", sa.Integer(), nullable=False, server_default="0")
    )


def _drop_queue_columns(table: str) -> None:
    op.drop_column(table, "attempt")
    op.drop_column(table, "worker_id")
    op.drop_column(table, "claimed_at")
    op.drop_column(table, "enqueued_at")


def upgrade() -> None:
    _add_queue_columns("agent_runs")
    _add_queue_columns("command_runs")
    op.add_column("agent_runs", sa.Column("working_dir", sa.Text(), nullable=True))

    # The index cannot be built while a workspace already has more than one active run. The
    # application guard should have prevented that, but keep only the newest active run per
    # workspace and record the rest as errors rather than fail the migration. Irreversible.
    op.execute(
        """
        UPDATE agent_runs
        SET status = 'error',
            error_reason = 'duplicate_active_run',
            error_message = 'Superseded by a newer active run in the same workspace.',
            completed_at = now()
        WHERE id IN (
            SELECT id FROM (
                SELECT id, row_number() OVER (
                    PARTITION BY workspace_id ORDER BY started_at DESC, id DESC
                ) AS rn
                FROM agent_runs
                WHERE status IN ('thinking', 'executing')
            ) ranked
            WHERE ranked.rn > 1
        )
        """
    )
    op.create_index(
        "uq_agent_runs_one_active_per_workspace",
        "agent_runs",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('thinking', 'executing')"),
    )


def downgrade() -> None:
    op.drop_index("uq_agent_runs_one_active_per_workspace", table_name="agent_runs")
    op.drop_column("agent_runs", "working_dir")
    _drop_queue_columns("command_runs")
    _drop_queue_columns("agent_runs")
