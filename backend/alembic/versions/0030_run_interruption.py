"""run interruption: machine-readable error reason + reconciliation indexes

Revision ID: 0030_run_interruption
Revises: 0029_editor_alias
Create Date: 2026-10-07 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030_run_interruption"
down_revision: str | None = "0029_editor_alias"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Why a run ended in `error`, as a stable code the UI can branch on (e.g.
    # "interrupted_by_restart"). Nullable: ordinary failures keep only their free-text detail.
    op.add_column("agent_runs", sa.Column("error_reason", sa.String(length=64), nullable=True))
    op.add_column("command_runs", sa.Column("error_reason", sa.String(length=64), nullable=True))

    # Startup reconciliation and the per-workspace active-run guard both filter on
    # (status, workspace_id).
    op.create_index(
        "ix_agent_runs_status_workspace_id", "agent_runs", ["status", "workspace_id"]
    )
    op.create_index(
        "ix_command_runs_status_workspace_id", "command_runs", ["status", "workspace_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_command_runs_status_workspace_id", table_name="command_runs")
    op.drop_index("ix_agent_runs_status_workspace_id", table_name="agent_runs")
    op.drop_column("command_runs", "error_reason")
    op.drop_column("agent_runs", "error_reason")
