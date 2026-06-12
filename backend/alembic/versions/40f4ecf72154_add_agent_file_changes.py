"""add_agent_file_changes

Revision ID: 40f4ecf72154
Revises: 0012_agent_events_tool_call_id
Create Date: 2026-06-12 05:28:11.978194
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "40f4ecf72154"
down_revision: str | None = "0012_agent_events_tool_call_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_file_changes",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "agent_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("tool_call_id", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status in ('created', 'modified', 'deleted', 'renamed')",
            name="ck_agent_file_changes_status",
        ),
    )
    op.create_index("ix_agent_file_changes_agent_run_id", "agent_file_changes", ["agent_run_id"])
    op.create_index("ix_agent_file_changes_tool_call_id", "agent_file_changes", ["tool_call_id"])
    op.create_index("ix_agent_file_changes_created_at", "agent_file_changes", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_agent_file_changes_created_at", table_name="agent_file_changes")
    op.drop_index("ix_agent_file_changes_tool_call_id", table_name="agent_file_changes")
    op.drop_index("ix_agent_file_changes_agent_run_id", table_name="agent_file_changes")
    op.drop_table("agent_file_changes")
