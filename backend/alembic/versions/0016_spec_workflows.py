"""spec_workflows

Revision ID: 0016_spec_workflows
Revises: 0015_workspace_dev_autostart
Create Date: 2026-06-27 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0016_spec_workflows"
down_revision: str | None = "0015_workspace_dev_autostart"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WORKFLOW_STATUSES = "'pending', 'running', 'awaiting_review', 'completed', 'error'"
_PHASES = "'constitution', 'specify', 'clarify', 'plan', 'analyze', 'tasks', 'implement'"
_PHASE_STATUSES = (
    "'pending', 'running', 'awaiting_review', 'approved', "
    "'changes_requested', 'completed', 'error'"
)


def upgrade() -> None:
    # 1. spec_workflows
    op.create_table(
        "spec_workflows",
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
        sa.Column("feature_name", sa.String(length=200), nullable=False),
        sa.Column("feature_slug", sa.String(length=220), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("branch", sa.String(length=220), nullable=False),
        sa.Column("spec_dir", sa.String(length=255), nullable=False),
        sa.Column("worktree_path", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("current_phase", sa.String(length=32), nullable=False, server_default="specify"),
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
            f"status in ({_WORKFLOW_STATUSES})",
            name="ck_spec_workflows_status",
        ),
    )
    op.create_index("ix_spec_workflows_workspace_id", "spec_workflows", ["workspace_id"])
    op.create_index("ix_spec_workflows_status", "spec_workflows", ["status"])
    op.create_index("ix_spec_workflows_current_phase", "spec_workflows", ["current_phase"])

    # 2. spec_phases
    op.create_table(
        "spec_phases",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "workflow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("spec_workflows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("artifact_path", sa.String(length=255), nullable=True),
        sa.Column(
            "agent_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_runs.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "reviewed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_notes", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
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
        sa.UniqueConstraint("workflow_id", "phase", name="uq_spec_phases_workflow_phase"),
        sa.CheckConstraint(f"phase in ({_PHASES})", name="ck_spec_phases_phase"),
        sa.CheckConstraint(
            f"status in ({_PHASE_STATUSES})",
            name="ck_spec_phases_status",
        ),
    )
    op.create_index("ix_spec_phases_workflow_id", "spec_phases", ["workflow_id"])
    op.create_index("ix_spec_phases_status", "spec_phases", ["status"])


def downgrade() -> None:
    op.drop_index("ix_spec_phases_status", table_name="spec_phases")
    op.drop_index("ix_spec_phases_workflow_id", table_name="spec_phases")
    op.drop_table("spec_phases")

    op.drop_index("ix_spec_workflows_current_phase", table_name="spec_workflows")
    op.drop_index("ix_spec_workflows_status", table_name="spec_workflows")
    op.drop_index("ix_spec_workflows_workspace_id", table_name="spec_workflows")
    op.drop_table("spec_workflows")
