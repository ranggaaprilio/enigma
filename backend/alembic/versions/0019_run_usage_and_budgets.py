"""run usage columns and usage_budgets

Revision ID: 0019_run_usage_and_budgets
Revises: 0018_agent_sessions
Create Date: 2026-07-02 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0019_run_usage_and_budgets"
down_revision: str | None = "0018_agent_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BUDGET_SCOPES = "'workspace', 'team', 'project'"
_BUDGET_PERIODS = "'daily', 'weekly', 'monthly', 'total'"


def upgrade() -> None:
    # 1. Split token counts and derived cost on agent_runs.
    op.add_column("agent_runs", sa.Column("input_tokens", sa.Integer(), nullable=True))
    op.add_column("agent_runs", sa.Column("output_tokens", sa.Integer(), nullable=True))
    op.add_column(
        "agent_runs",
        sa.Column("estimated_cost", sa.Numeric(precision=12, scale=6), nullable=True),
    )

    # 2. usage_budgets
    op.create_table(
        "usage_budgets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("period", sa.String(length=20), nullable=False),
        sa.Column("limit_cost", sa.Numeric(precision=12, scale=4), nullable=True),
        sa.Column("limit_tokens", sa.BigInteger(), nullable=True),
        sa.Column("threshold_percent", sa.Integer(), nullable=False, server_default="80"),
        sa.Column("enforce", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
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
        sa.CheckConstraint(f"scope in ({_BUDGET_SCOPES})", name="ck_usage_budgets_scope"),
        sa.CheckConstraint(f"period in ({_BUDGET_PERIODS})", name="ck_usage_budgets_period"),
        sa.CheckConstraint(
            "limit_cost is not null or limit_tokens is not null",
            name="ck_usage_budgets_limit_present",
        ),
        sa.CheckConstraint(
            "threshold_percent >= 0 and threshold_percent <= 100",
            name="ck_usage_budgets_threshold_range",
        ),
        sa.UniqueConstraint("scope", "scope_id", "period", name="uq_usage_budgets_scope_period"),
    )
    op.create_index("ix_usage_budgets_scope_scope_id", "usage_budgets", ["scope", "scope_id"])


def downgrade() -> None:
    op.drop_index("ix_usage_budgets_scope_scope_id", table_name="usage_budgets")
    op.drop_table("usage_budgets")

    op.drop_column("agent_runs", "estimated_cost")
    op.drop_column("agent_runs", "output_tokens")
    op.drop_column("agent_runs", "input_tokens")
