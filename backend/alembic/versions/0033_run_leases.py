"""run leases: heartbeat/lease columns on agent and command runs

Deliberately unindexed: the reaper's scan is already narrow (only active runs, at most one per
workspace for agents), and indexing ``lease_expires_at`` would turn every heartbeat into a
non-HOT update that rewrites all of the table's indexes.

Revision ID: 0033_run_leases
Revises: 0032_agent_events_stream_index
Create Date: 2026-10-08 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0033_run_leases"
down_revision: str | None = "0032_agent_events_stream_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("agent_runs", "command_runs")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(table, sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column(
            table, sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "lease_expires_at")
        op.drop_column(table, "heartbeat_at")
