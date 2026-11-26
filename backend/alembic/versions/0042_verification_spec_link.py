"""verification spec link: workflow verification criteria and the run's workflow

Verifiable Agent Loop Plan 3 Task 1. ``spec_workflows.verification`` holds the criteria for
verifying a workflow's ``implement`` phase; ``verification_runs.spec_workflow_id`` links a run
to the workflow it verifies. Expand-only: two nullable columns and an index.

Revision ID: 0042_verification_spec_link
Revises: 0041_verification_loop_state
Create Date: 2026-11-27 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0042_verification_spec_link"
down_revision: str | None = "0041_verification_loop_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "spec_workflows",
        sa.Column("verification", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    # The FK is created with the new column: no existing row or older writer touches it, so
    # it cannot reject anything the previous release writes (expand-safe).
    op.add_column(
        "verification_runs",
        sa.Column(
            "spec_workflow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "spec_workflows.id",
                name="fk_verification_runs_spec_workflow_id_spec_workflows",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_verification_runs_spec_workflow_id",
        "verification_runs",
        ["spec_workflow_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_verification_runs_spec_workflow_id", table_name="verification_runs")
    op.drop_constraint(
        "fk_verification_runs_spec_workflow_id_spec_workflows",
        "verification_runs",
        type_="foreignkey",
    )
    op.drop_column("verification_runs", "spec_workflow_id")
    op.drop_column("spec_workflows", "verification")
