"""sandbox activity: the shared idle clock for local Docker sandboxes

One row per workspace, upserted (debounced) on every exec, command stream, terminal byte
and preview request that reaches a Docker sandbox, from the API, the worker or the preview
proxy. The idle reaper stops containers whose row is older than SANDBOX_AUTO_STOP_MINUTES.

``sandbox_installation`` holds one random id per database. Every Docker sandbox and network
is labelled with it, and the upkeep loop only ever touches its own: two Enigma stacks on one
daemon (a second checkout, a test stack, a restored database) never remove each other's.
Expand-only: new tables.

Revision ID: 0036_sandbox_activity
Revises: 0035_run_correlation
Create Date: 2026-10-30 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0036_sandbox_activity"
down_revision: str | None = "0035_run_correlation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sandbox_activity",
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "last_activity_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


    op.create_table(
        "sandbox_installation",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("singleton", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("singleton", name="uq_sandbox_installation_singleton"),
        sa.CheckConstraint("singleton", name="ck_sandbox_installation_singleton"),
    )
    op.execute("INSERT INTO sandbox_installation DEFAULT VALUES")


def downgrade() -> None:
    op.drop_table("sandbox_installation")
    op.drop_table("sandbox_activity")
