"""data retention: pruned markers, retention scan indexes, notifications (user_id, created_at)

The partial indexes cover only finished, not-yet-pruned runs, so they stay small and a
retention pass never re-scans history it already pruned. ``(user_id, created_at)`` serves the
notification bell's newest-first read and replaces the single-column ``user_id`` index.
``ix_agent_events_agent_run_id`` is dropped for the same reason: it is a prefix of
``ix_agent_events_run_created_id`` (0032), and every event insert paid for both.

Revision ID: 0034_data_retention
Revises: 0033_run_leases
Create Date: 2026-10-08 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0034_data_retention"
down_revision: str | None = "0033_run_leases"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_runs", sa.Column("events_pruned_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "command_runs", sa.Column("output_pruned_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_agent_runs_events_retention",
        "agent_runs",
        ["completed_at"],
        postgresql_where=sa.text("events_pruned_at IS NULL AND completed_at IS NOT NULL"),
    )
    op.create_index(
        "ix_command_runs_output_retention",
        "command_runs",
        ["completed_at"],
        postgresql_where=sa.text("output_pruned_at IS NULL AND completed_at IS NOT NULL"),
    )
    op.create_index("ix_notifications_user_created", "notifications", ["user_id", "created_at"])
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_index("ix_agent_events_agent_run_id", table_name="agent_events")


def downgrade() -> None:
    op.create_index("ix_agent_events_agent_run_id", "agent_events", ["agent_run_id"])
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
    op.drop_index("ix_notifications_user_created", table_name="notifications")
    op.drop_index("ix_command_runs_output_retention", table_name="command_runs")
    op.drop_index("ix_agent_runs_events_retention", table_name="agent_runs")
    op.drop_column("command_runs", "output_pruned_at")
    op.drop_column("agent_runs", "events_pruned_at")
