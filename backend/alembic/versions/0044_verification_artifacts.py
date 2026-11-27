"""verification artifacts: object-store metadata for evidence screenshots

Verifiable Agent Loop Plan 6 Task 1. One row per stored artifact (a screenshot PNG) of an
evidence row; the bytes live in the S3-compatible artifact store under ``storage_key``.
Expand-only: a new table.

Revision ID: 0044_verification_artifacts
Revises: 0043_spec_workflow_pull_request
Create Date: 2026-11-27 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0044_verification_artifacts"
down_revision: str | None = "0043_spec_workflow_pull_request"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "verification_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "evidence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "verification_evidence.id",
                name="fk_verification_artifacts_evidence_id_verification_evidence",
                ondelete="CASCADE",
            ),
            nullable=False,
        ),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("evidence_id", name="uq_verification_artifacts_evidence_id"),
    )


def downgrade() -> None:
    op.drop_table("verification_artifacts")
