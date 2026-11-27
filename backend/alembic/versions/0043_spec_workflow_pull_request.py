"""spec workflow pull request: the PR an approved workflow was handed over as

Verifiable Agent Loop Plan 4 Task 1. ``spec_workflows.pull_request_id`` links a completed
workflow to its cached ``pull_requests`` row. Expand-only: one nullable column and an index.

Revision ID: 0043_spec_workflow_pull_request
Revises: 0042_verification_spec_link
Create Date: 2026-11-27 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0043_spec_workflow_pull_request"
down_revision: str | None = "0042_verification_spec_link"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The FK is created with the new column: no existing row or older writer touches it, so
    # it cannot reject anything the previous release writes (expand-safe).
    op.add_column(
        "spec_workflows",
        sa.Column(
            "pull_request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "pull_requests.id",
                name="fk_spec_workflows_pull_request_id_pull_requests",
                ondelete="SET NULL",
            ),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_spec_workflows_pull_request_id",
        "spec_workflows",
        ["pull_request_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_spec_workflows_pull_request_id", table_name="spec_workflows")
    op.drop_constraint(
        "fk_spec_workflows_pull_request_id_pull_requests",
        "spec_workflows",
        type_="foreignkey",
    )
    op.drop_column("spec_workflows", "pull_request_id")
