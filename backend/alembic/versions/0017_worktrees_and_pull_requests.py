"""worktrees and pull_requests

Revision ID: 0017_worktrees_and_pull_requests
Revises: 0016_spec_workflows
Create Date: 2026-06-27 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0017_worktrees_and_pull_requests"
down_revision: str | None = "0016_spec_workflows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WORKTREE_STATUSES = "'active', 'removing'"


def upgrade() -> None:
    # 1. workspace_worktrees
    op.create_table(
        "workspace_worktrees",
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
        sa.Column("branch", sa.String(length=220), nullable=False),
        sa.Column("path", sa.String(length=255), nullable=False),
        sa.Column("base_ref", sa.String(length=220), nullable=False),
        sa.Column("is_base", sa.Boolean(), nullable=False, server_default=sa.text("false")),
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
            f"status in ({_WORKTREE_STATUSES})",
            name="ck_workspace_worktrees_status",
        ),
        sa.UniqueConstraint(
            "workspace_id", "branch", name="uq_workspace_worktrees_workspace_branch"
        ),
        sa.UniqueConstraint(
            "workspace_id", "path", name="uq_workspace_worktrees_workspace_path"
        ),
    )
    op.create_index(
        "ix_workspace_worktrees_workspace_id", "workspace_worktrees", ["workspace_id"]
    )

    # 2. pull_requests
    op.create_table(
        "pull_requests",
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
            "worktree_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_worktrees.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("head", sa.String(length=220), nullable=False),
        sa.Column("base", sa.String(length=220), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
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
        sa.UniqueConstraint("workspace_id", "number", name="uq_pull_requests_workspace_number"),
    )
    op.create_index("ix_pull_requests_workspace_id", "pull_requests", ["workspace_id"])

    # 3. agent_runs.worktree_id
    op.add_column(
        "agent_runs",
        sa.Column(
            "worktree_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_worktrees.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "worktree_id")

    op.drop_index("ix_pull_requests_workspace_id", table_name="pull_requests")
    op.drop_table("pull_requests")

    op.drop_index("ix_workspace_worktrees_workspace_id", table_name="workspace_worktrees")
    op.drop_table("workspace_worktrees")
