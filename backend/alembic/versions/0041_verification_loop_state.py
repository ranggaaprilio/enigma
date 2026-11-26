"""verification loop state: persisted criteria, author, fix iteration and agent run

Verifiable Agent Loop Plan 2 Task 1. ``verification_runs`` gains the columns a process needs
to resume a run from its row (``criteria``, ``created_by``, ``iteration``, ``agent_run_id``),
the ``fixing`` status, and ``verification_evidence`` the ``fix`` kind.

Revision ID: 0041_verification_loop_state
Revises: 0040_verification_evidence
Create Date: 2026-11-26 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0041_verification_loop_state"
down_revision: str | None = "0040_verification_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_RUN_STATUSES = "'planning', 'testing', 'observing', 'review', 'approved', 'rejected', 'error'"
NEW_RUN_STATUSES = (
    "'planning', 'testing', 'fixing', 'observing', 'review', 'approved', 'rejected', 'error'"
)
OLD_EVIDENCE_KINDS = "'test', 'observation', 'screenshot'"
NEW_EVIDENCE_KINDS = "'test', 'observation', 'screenshot', 'fix'"


def upgrade() -> None:
    op.add_column(
        "verification_runs",
        sa.Column(
            "criteria",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )
    op.add_column(
        "verification_runs",
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "verification_runs",
        sa.Column("iteration", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "verification_runs",
        sa.Column("agent_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_verification_runs_created_by_users",
        "verification_runs",
        "users",
        ["created_by"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_verification_runs_agent_run_id_agent_runs",
        "verification_runs",
        "agent_runs",
        ["agent_run_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # contract: widening only -- the new CHECKs accept a strict superset of the old values, so
    # rows and writers of the previous release stay valid; nothing old is rejected.
    op.drop_constraint("ck_verification_runs_status", "verification_runs", type_="check")
    op.create_check_constraint(
        "ck_verification_runs_status", "verification_runs", f"status in ({NEW_RUN_STATUSES})"
    )
    op.drop_constraint("ck_verification_evidence_kind", "verification_evidence", type_="check")
    op.create_check_constraint(
        "ck_verification_evidence_kind", "verification_evidence", f"kind in ({NEW_EVIDENCE_KINDS})"
    )


def downgrade() -> None:
    op.execute("DELETE FROM verification_evidence WHERE kind = 'fix'")
    op.execute("UPDATE verification_runs SET status = 'error' WHERE status = 'fixing'")
    op.drop_constraint("ck_verification_evidence_kind", "verification_evidence", type_="check")
    op.create_check_constraint(
        "ck_verification_evidence_kind", "verification_evidence", f"kind in ({OLD_EVIDENCE_KINDS})"
    )
    op.drop_constraint("ck_verification_runs_status", "verification_runs", type_="check")
    op.create_check_constraint(
        "ck_verification_runs_status", "verification_runs", f"status in ({OLD_RUN_STATUSES})"
    )
    op.drop_constraint(
        "fk_verification_runs_agent_run_id_agent_runs", "verification_runs", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_verification_runs_created_by_users", "verification_runs", type_="foreignkey"
    )
    op.drop_column("verification_runs", "agent_run_id")
    op.drop_column("verification_runs", "iteration")
    op.drop_column("verification_runs", "created_by")
    op.drop_column("verification_runs", "criteria")
