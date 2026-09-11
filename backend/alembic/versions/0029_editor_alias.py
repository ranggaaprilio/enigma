"""editor alias: stable proxy hostname for the embedded code-server editor

Revision ID: 0029_editor_alias
Revises: 0028_platform_llm_settings
Create Date: 2026-09-11 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0029_editor_alias"
down_revision: str | None = "0028_platform_llm_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The editor's stable proxy hostname label, e.g. "editor-3f9a1c0b7d2e". Minted lazily on the
    # first editor start, never rotated -- code-server keeps its workbench state in localStorage
    # keyed by origin, so a rotating host would wipe the open tabs and layout on every renewal.
    # Nullable because every pre-existing workspace has none until its editor is next started.
    op.add_column("workspaces", sa.Column("editor_alias", sa.String(length=63), nullable=True))
    op.create_unique_constraint("uq_workspaces_editor_alias", "workspaces", ["editor_alias"])

    # Which worktree the editor was opened on (NULL = base clone), plus the resolved absolute
    # sandbox path handed to code-server as `?folder=`. Persisted so `status` can rebuild the
    # URL without re-resolving the worktree.
    op.add_column(
        "editor_sessions",
        sa.Column("worktree_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_editor_sessions_worktree_id",
        "editor_sessions",
        "workspace_worktrees",
        ["worktree_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("editor_sessions", sa.Column("folder", sa.Text(), nullable=True))

    # The editor URL no longer embeds a credential: it is a stable alias host, authorised by the
    # proxy's grant cookie. `token_hash`/`token_expires_at` described the old Daytona signed URL
    # and have no successor, so they become nullable and stop being written.
    op.alter_column(
        "editor_sessions",
        "token_hash",
        existing_type=sa.String(length=128),
        nullable=True,
    )
    op.alter_column(
        "editor_sessions",
        "token_expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=True,
    )


def downgrade() -> None:
    # Rows written after the upgrade have NULL token columns; backfill them so the NOT NULL
    # constraints can be restored rather than failing the downgrade outright.
    op.execute("update editor_sessions set token_hash = '' where token_hash is null")
    op.execute("update editor_sessions set token_expires_at = now() where token_expires_at is null")
    op.alter_column(
        "editor_sessions",
        "token_expires_at",
        existing_type=sa.DateTime(timezone=True),
        nullable=False,
    )
    op.alter_column(
        "editor_sessions",
        "token_hash",
        existing_type=sa.String(length=128),
        nullable=False,
    )
    op.drop_column("editor_sessions", "folder")
    op.drop_constraint("fk_editor_sessions_worktree_id", "editor_sessions", type_="foreignkey")
    op.drop_column("editor_sessions", "worktree_id")
    op.drop_constraint("uq_workspaces_editor_alias", "workspaces", type_="unique")
    op.drop_column("workspaces", "editor_alias")
