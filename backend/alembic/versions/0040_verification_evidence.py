"""verification evidence: runs and their evidence for the verifiable agent loop

Verifiable Agent Loop Task 2. Two new tables. ``verification_runs`` is one row per
verification loop over a workspace, keyed by a ``plan_ref``; ``verification_evidence`` is
the test / observation / screenshot results attached to a run. Both are written by the
verification loop and cascade-deleted with their workspace (runs) and run (evidence).
Expand-only: new tables.

Revision ID: 0040_verification_evidence
Revises: 0039_drop_daytona_sandbox_id
Create Date: 2026-11-26 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0040_verification_evidence"
down_revision: str | None = "0039_drop_daytona_sandbox_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "verification_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_ref", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status in ('planning', 'testing', 'observing', 'review', 'approved', "
            "'rejected', 'error')",
            name="ck_verification_runs_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_verification_runs_workspace_id", "verification_runs", ["workspace_id"], unique=False
    )
    op.create_index("ix_verification_runs_status", "verification_runs", ["status"], unique=False)

    op.create_table(
        "verification_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("path", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind in ('test', 'observation', 'screenshot')",
            name="ck_verification_evidence_kind",
        ),
        sa.CheckConstraint(
            "status in ('passed', 'failed', 'observation_failed', 'error')",
            name="ck_verification_evidence_status",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["verification_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_verification_evidence_run_id", "verification_evidence", ["run_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_verification_evidence_run_id", table_name="verification_evidence")
    op.drop_table("verification_evidence")
    op.drop_index("ix_verification_runs_status", table_name="verification_runs")
    op.drop_index("ix_verification_runs_workspace_id", table_name="verification_runs")
    op.drop_table("verification_runs")
