"""run correlation: request_id and traceparent on agent_runs and command_runs

Both columns are nullable and written once at creation. ``request_id`` is the ``X-Request-ID``
of the HTTP request (or WebSocket) that created the run, so a run's whole lifecycle — API,
queue, worker, stream frames — can be found by one id. ``traceparent`` is the W3C trace
context captured at creation; the worker starts its span as a child of it. The job payload
still carries only the run id: a worker reads both off the row after claiming it.

Revision ID: 0035_run_correlation
Revises: 0034_data_retention
Create Date: 2026-10-12 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0035_run_correlation"
down_revision: str | None = "0034_data_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("agent_runs", "command_runs"):
        op.add_column(table, sa.Column("request_id", sa.String(length=64), nullable=True))
        op.add_column(table, sa.Column("traceparent", sa.String(length=64), nullable=True))


def downgrade() -> None:
    for table in ("command_runs", "agent_runs"):
        op.drop_column(table, "traceparent")
        op.drop_column(table, "request_id")
