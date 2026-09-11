"""env_var_scoping: per-service env var overrides + .env materialisation (Epic 12)

Revision ID: 0026_env_var_scoping
Revises: 0025_workspace_services
Create Date: 2026-09-11 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0026_env_var_scoping"
down_revision: str | None = "0025_workspace_services"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Downgrade dedupe: the old single UNIQUE(workspace_id, key) cannot represent two rows sharing
# a key across scopes. Step (a) removes a service-scoped row wherever a workspace-wide row of
# the same key already exists (the workspace-wide row wins, matching resolve()'s own
# precedence); step (b) keeps only the most-recently-updated row among any remaining
# (workspace_id, key) duplicates across different services. Both are lossy and documented here
# rather than silently discarded. Two statements -- asyncpg does not accept multiple commands
# in a single ``execute()``.
_DEDUPE_SHADOWED_SERVICE_ROWS = """
DELETE FROM workspace_env_vars AS s
USING workspace_env_vars AS w
WHERE s.custom_command_id IS NOT NULL
  AND w.custom_command_id IS NULL
  AND s.workspace_id = w.workspace_id
  AND s.key = w.key
"""

_DEDUPE_REMAINING_SERVICE_DUPLICATES = """
DELETE FROM workspace_env_vars
WHERE id IN (
    SELECT id FROM (
        SELECT id,
               row_number() OVER (
                   PARTITION BY workspace_id, key ORDER BY updated_at DESC, id
               ) AS rn
        FROM workspace_env_vars
        WHERE custom_command_id IS NOT NULL
    ) AS ranked
    WHERE rn > 1
)
"""


def upgrade() -> None:
    # 1. scope + custom_command_id: a row is either workspace-wide (NULL) or bound to one
    #    service (a custom_commands row), never both and never neither.
    op.add_column(
        "workspace_env_vars",
        sa.Column("scope", sa.String(length=16), nullable=False, server_default="workspace"),
    )
    op.add_column(
        "workspace_env_vars",
        sa.Column("custom_command_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_workspace_env_vars_custom_command_id",
        "workspace_env_vars",
        "custom_commands",
        ["custom_command_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_workspace_env_vars_scope",
        "workspace_env_vars",
        "scope in ('workspace', 'service')",
    )
    op.create_check_constraint(
        "ck_workspace_env_vars_scope_target",
        "workspace_env_vars",
        "(scope = 'workspace' AND custom_command_id IS NULL) OR "
        "(scope = 'service' AND custom_command_id IS NOT NULL)",
    )
    # Every existing row becomes scope='workspace', custom_command_id=NULL -- already true via
    # the server_default and the column being freshly added as NULL, but spelled out for clarity.
    op.execute(
        sa.text(
            "UPDATE workspace_env_vars SET scope = 'workspace' WHERE custom_command_id IS NULL"
        )
    )

    # 2. relax uniqueness: at most one workspace-wide row per (workspace, key), and at most one
    #    row per (workspace, key, custom_command_id) among service-scoped rows. Postgres treats
    #    NULLs as distinct in a plain unique index, so the workspace-wide half needs a partial
    #    index instead of relying on the composite one.
    op.drop_constraint(
        "uq_workspace_env_vars_workspace_key", "workspace_env_vars", type_="unique"
    )
    op.create_index(
        "uq_workspace_env_vars_workspace_key_global",
        "workspace_env_vars",
        ["workspace_id", "key"],
        unique=True,
        postgresql_where=sa.text("custom_command_id IS NULL"),
    )
    op.create_index(
        "uq_workspace_env_vars_workspace_key_command",
        "workspace_env_vars",
        ["workspace_id", "key", "custom_command_id"],
        unique=True,
    )
    op.create_index(
        "ix_workspace_env_vars_custom_command_id", "workspace_env_vars", ["custom_command_id"]
    )

    # 3. workspaces.env_file_path -- opt-in .env materialisation target, repo-relative, below
    #    app_root; validated server-side (never absolute, never inside .git).
    op.add_column("workspaces", sa.Column("env_file_path", sa.Text(), nullable=True))


def downgrade() -> None:
    # 3. workspaces.env_file_path
    op.drop_column("workspaces", "env_file_path")

    # 2 & 1. restore the single unique constraint -- lossy for genuinely scoped data, see the
    #    dedupe SQL's own docstring above.
    op.execute(sa.text(_DEDUPE_SHADOWED_SERVICE_ROWS))
    op.execute(sa.text(_DEDUPE_REMAINING_SERVICE_DUPLICATES))
    op.drop_index("ix_workspace_env_vars_custom_command_id", table_name="workspace_env_vars")
    op.drop_index("uq_workspace_env_vars_workspace_key_command", table_name="workspace_env_vars")
    op.drop_index("uq_workspace_env_vars_workspace_key_global", table_name="workspace_env_vars")
    op.create_unique_constraint(
        "uq_workspace_env_vars_workspace_key", "workspace_env_vars", ["workspace_id", "key"]
    )
    op.drop_constraint(
        "ck_workspace_env_vars_scope_target", "workspace_env_vars", type_="check"
    )
    op.drop_constraint("ck_workspace_env_vars_scope", "workspace_env_vars", type_="check")
    op.drop_constraint(
        "fk_workspace_env_vars_custom_command_id", "workspace_env_vars", type_="foreignkey"
    )
    op.drop_column("workspace_env_vars", "custom_command_id")
    op.drop_column("workspace_env_vars", "scope")
