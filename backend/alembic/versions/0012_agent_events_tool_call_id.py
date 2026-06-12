"""Add tool_call_id to agent_events.

Revision ID: 0012_agent_events_tool_call_id
Revises: 0011_daytona_runtime
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_agent_events_tool_call_id"
down_revision: str | None = "0011_daytona_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_events",
        sa.Column("tool_call_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_agent_events_tool_call_id",
        "agent_events",
        ["tool_call_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_agent_events_tool_call_id", table_name="agent_events")
    op.drop_column("agent_events", "tool_call_id")