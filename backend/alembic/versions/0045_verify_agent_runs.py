"""verify agent runs: link a verification run to its agent run, and opt-in auto-verify

Verifiable Agent Loop Plan 9 Task 1. ``verification_runs.source_agent_run_id`` names the agent run
whose code a verification run checks (its fix agent continues that run's worktree and session).
``workspaces.auto_verify_agent_runs`` turns on verifying every completed agent run (Task 3).
Expand-only: two nullable/defaulted columns and an index.

Revision ID: 0045_verify_agent_runs
Revises: 0044_verification_artifacts
Create Date: 2026-11-28 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0045_verify_agent_runs"
down_revision: str | None = "0044_verification_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "verification_runs",
        sa.Column(
            "source_agent_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "agent_runs.id",
                name="fk_verification_runs_source_agent_run_id_agent_runs",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_verification_runs_source_agent_run_id",
        "verification_runs",
        ["source_agent_run_id"],
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "auto_verify_agent_runs",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "auto_verify_agent_runs")
    op.drop_index("ix_verification_runs_source_agent_run_id", table_name="verification_runs")
    op.drop_column("verification_runs", "source_agent_run_id")
