"""agent_sessions

Revision ID: 0018_agent_sessions
Revises: 0017_worktrees_and_pull_requests
Create Date: 2026-07-02 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0018_agent_sessions"
down_revision: str | None = "0017_worktrees_and_pull_requests"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SESSION_STATUSES = "'active', 'archived'"


def upgrade() -> None:
    # 1. agent_sessions
    op.create_table(
        "agent_sessions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=200), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            f"status in ({_SESSION_STATUSES})",
            name="ck_agent_sessions_status",
        ),
    )
    op.create_index("ix_agent_sessions_workspace_id", "agent_sessions", ["workspace_id"])
    op.create_index("ix_agent_sessions_user_id", "agent_sessions", ["user_id"])
    op.create_index("ix_agent_sessions_status", "agent_sessions", ["status"])

    # 2. agent_runs.session_id
    op.add_column(
        "agent_runs",
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_agent_runs_session_id", "agent_runs", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_runs_session_id", table_name="agent_runs")
    op.drop_column("agent_runs", "session_id")

    op.drop_index("ix_agent_sessions_status", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_user_id", table_name="agent_sessions")
    op.drop_index("ix_agent_sessions_workspace_id", table_name="agent_sessions")
    op.drop_table("agent_sessions")
