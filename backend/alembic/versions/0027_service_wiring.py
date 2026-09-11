"""service_wiring: preview aliases, service diagnostics, health, entry point (Epic 13)

Revision ID: 0027_service_wiring
Revises: 0026_env_var_scoping
Create Date: 2026-09-11 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0027_service_wiring"
down_revision: str | None = "0026_env_var_scoping"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_HEALTH_STATUSES = "'unknown', 'starting', 'healthy', 'unhealthy'"
_DIAGNOSTIC_KINDS = "'cors_missing_allow_origin', 'cors_preflight_rejected'"

# A single DNS label: lowercase alphanumeric with hyphens in the middle, 3-62 chars, at least
# one hyphen (the property that makes it provably disjoint from a 63-char base32 capability
# label -- see services/preview_alias.py), never punycode, never leading underscore.
_ALIAS_FORMAT_CHECK = (
    "alias ~ '^[a-z0-9][a-z0-9-]{1,60}[a-z0-9]$' "
    "AND position('-' in alias) > 0 "
    "AND alias NOT LIKE 'xn--%' "
    "AND alias NOT LIKE '\\_%'"
)


def upgrade() -> None:
    # 1. workspaces.preview_alias_enabled -- opt-in, per-workspace kill switch for the whole
    #    alias feature. The capability-host scheme is untouched and stays default-on.
    op.add_column(
        "workspaces",
        sa.Column(
            "preview_alias_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    # 2. custom_commands.is_entry_point + health_path. Configuration lives on the durable
    #    command row, not the churny workspace_services instance row (see model docstrings).
    op.add_column(
        "custom_commands",
        sa.Column(
            "is_entry_point",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "custom_commands",
        sa.Column("health_path", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_custom_commands_entry_point_kind",
        "custom_commands",
        "is_entry_point = false or kind = 'service'",
    )
    op.create_check_constraint(
        "ck_custom_commands_health_path",
        "custom_commands",
        "health_path is null or health_path like '/%'",
    )
    op.create_index(
        "uq_custom_commands_entry_point",
        "custom_commands",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("is_entry_point"),
    )

    # 3. workspace_services health columns -- runtime readiness, orthogonal to `status`.
    op.add_column(
        "workspace_services",
        sa.Column(
            "health_status",
            sa.String(length=16),
            nullable=False,
            server_default="unknown",
        ),
    )
    op.add_column(
        "workspace_services",
        sa.Column("health_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_workspace_services_health_status",
        "workspace_services",
        f"health_status in ({_HEALTH_STATUSES})",
    )

    # 4. workspace_preview_aliases -- one opt-in stable hostname per (workspace, service).
    op.create_table(
        "workspace_preview_aliases",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "custom_command_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("custom_commands.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("alias", sa.String(length=62), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
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
        sa.CheckConstraint(
            _ALIAS_FORMAT_CHECK, name="ck_workspace_preview_aliases_alias_format"
        ),
        sa.UniqueConstraint("alias", name="uq_workspace_preview_aliases_alias"),
        sa.UniqueConstraint(
            "workspace_id",
            "custom_command_id",
            name="uq_workspace_preview_aliases_workspace_command",
        ),
    )
    op.create_index(
        "ix_workspace_preview_aliases_workspace_id",
        "workspace_preview_aliases",
        ["workspace_id"],
    )
    op.create_index(
        "ix_workspace_preview_aliases_custom_command_id",
        "workspace_preview_aliases",
        ["custom_command_id"],
    )
    # NULLs are distinct in a plain unique constraint, so the "one alias for the built-in row"
    # rule needs a partial index -- same reason uq_workspace_services_builtin needed one.
    op.create_index(
        "uq_workspace_preview_aliases_builtin",
        "workspace_preview_aliases",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("custom_command_id IS NULL"),
    )

    # 5. service_diagnostics -- observed CORS events, recorded by the preview proxy.
    op.create_table(
        "service_diagnostics",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "service_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_services.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "preview_session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("preview_sessions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("origin", sa.String(length=255), nullable=False),
        sa.Column("request_method", sa.String(length=16), nullable=True),
        sa.Column("required_header", sa.String(length=128), nullable=True),
        sa.Column("request_path", sa.String(length=512), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"kind in ({_DIAGNOSTIC_KINDS})", name="ck_service_diagnostics_kind"
        ),
        sa.CheckConstraint("occurrences >= 1", name="ck_service_diagnostics_occurrences"),
        sa.CheckConstraint(
            "status_code is null or (status_code between 100 and 599)",
            name="ck_service_diagnostics_status_code",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "service_id",
            "kind",
            "origin",
            name="uq_service_diagnostics_service_kind_origin",
        ),
    )
    op.create_index(
        "ix_service_diagnostics_workspace_id", "service_diagnostics", ["workspace_id"]
    )
    op.create_index("ix_service_diagnostics_service_id", "service_diagnostics", ["service_id"])
    op.create_index(
        "ix_service_diagnostics_last_seen_at", "service_diagnostics", ["last_seen_at"]
    )
    op.create_index(
        "ix_service_diagnostics_unresolved",
        "service_diagnostics",
        ["workspace_id", "last_seen_at"],
        postgresql_where=sa.text("resolved_at IS NULL"),
    )


def downgrade() -> None:
    # 5. service_diagnostics
    op.drop_index("ix_service_diagnostics_unresolved", table_name="service_diagnostics")
    op.drop_index("ix_service_diagnostics_last_seen_at", table_name="service_diagnostics")
    op.drop_index("ix_service_diagnostics_service_id", table_name="service_diagnostics")
    op.drop_index("ix_service_diagnostics_workspace_id", table_name="service_diagnostics")
    op.drop_table("service_diagnostics")

    # 4. workspace_preview_aliases
    op.drop_index(
        "uq_workspace_preview_aliases_builtin", table_name="workspace_preview_aliases"
    )
    op.drop_index(
        "ix_workspace_preview_aliases_custom_command_id",
        table_name="workspace_preview_aliases",
    )
    op.drop_index(
        "ix_workspace_preview_aliases_workspace_id", table_name="workspace_preview_aliases"
    )
    op.drop_table("workspace_preview_aliases")

    # 3. workspace_services health columns
    op.drop_constraint(
        "ck_workspace_services_health_status", "workspace_services", type_="check"
    )
    op.drop_column("workspace_services", "health_checked_at")
    op.drop_column("workspace_services", "health_status")

    # 2. custom_commands entry point + health path
    op.drop_index("uq_custom_commands_entry_point", table_name="custom_commands")
    op.drop_constraint("ck_custom_commands_health_path", "custom_commands", type_="check")
    op.drop_constraint(
        "ck_custom_commands_entry_point_kind", "custom_commands", type_="check"
    )
    op.drop_column("custom_commands", "health_path")
    op.drop_column("custom_commands", "is_entry_point")

    # 1. workspaces.preview_alias_enabled
    op.drop_column("workspaces", "preview_alias_enabled")
