"""platform_llm_settings: global OpenAI-compatible LLM credentials

Revision ID: 0028_platform_llm_settings
Revises: 0027_service_wiring
Create Date: 2026-09-11 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0028_platform_llm_settings"
down_revision: str | None = "0027_service_wiring"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A singleton table: `singleton` is UNIQUE and CHECK-constrained to TRUE, so the database
    # itself rejects a second row. No row is seeded -- "no row" is the meaningful
    # not-configured state, which makes resolution fall back to the OPENHANDS_LLM_* env vars.
    op.create_table(
        "platform_llm_settings",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("singleton", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        # Nullable: local runtimes (Ollama, LM Studio, vLLM) need no API key. Fernet-encrypted
        # with WorkspaceSecretCipher, the same cipher/key as workspace_env_vars.
        sa.Column("encrypted_api_key", sa.Text(), nullable=True),
        sa.Column("updated_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.UniqueConstraint("singleton", name="uq_platform_llm_settings_singleton"),
        sa.CheckConstraint("singleton is true", name="ck_platform_llm_settings_singleton"),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            ondelete="SET NULL",
            name="fk_platform_llm_settings_updated_by_user_id",
        ),
    )


def downgrade() -> None:
    # Lossy by nature: dropping the table discards the configured endpoint and the encrypted
    # API key. Deployments fall back to the OPENHANDS_LLM_* environment variables.
    op.drop_table("platform_llm_settings")
