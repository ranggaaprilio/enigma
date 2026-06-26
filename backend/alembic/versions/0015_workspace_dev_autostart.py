"""workspace_dev_autostart

Revision ID: 0015_workspace_dev_autostart
Revises: 0014_projects_and_teams
Create Date: 2026-06-25 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_workspace_dev_autostart"
down_revision: str | None = "0014_projects_and_teams"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "dev_autostart",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "dev_autostart")
