"""worktree_aware_preview

Revision ID: 0020_worktree_aware_preview
Revises: 0019_run_usage_and_budgets
Create Date: 2026-07-02 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0020_worktree_aware_preview"
down_revision: str | None = "0019_run_usage_and_budgets"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Which worktree the current dev server was launched in (NULL = base clone).
    op.add_column(
        "workspaces",
        sa.Column(
            "dev_worktree_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_workspaces_dev_worktree_id",
        "workspaces",
        "workspace_worktrees",
        ["dev_worktree_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # Which worktree a detected preview belongs to (NULL = base clone).
    op.add_column(
        "preview_sessions",
        sa.Column(
            "worktree_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_preview_sessions_worktree_id",
        "preview_sessions",
        "workspace_worktrees",
        ["worktree_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_preview_sessions_worktree_id",
        "preview_sessions",
        ["worktree_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_preview_sessions_worktree_id", table_name="preview_sessions")
    op.drop_constraint(
        "fk_preview_sessions_worktree_id",
        "preview_sessions",
        type_="foreignkey",
    )
    op.drop_column("preview_sessions", "worktree_id")

    op.drop_constraint(
        "fk_workspaces_dev_worktree_id",
        "workspaces",
        type_="foreignkey",
    )
    op.drop_column("workspaces", "dev_worktree_id")
