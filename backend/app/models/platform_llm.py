import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PlatformLlmSettings(Base):
    """Platform-wide OpenAI-compatible LLM credentials, configured through the UI.

    This table holds at most one row: ``singleton`` is UNIQUE and CHECK-constrained to TRUE, so a
    second row is rejected by the database rather than by convention. The absence of a row is the
    meaningful "not configured" state, in which case resolution falls back to the
    ``OPENHANDS_LLM_*`` environment variables.

    ``encrypted_api_key`` is Fernet-encrypted with ``WorkspaceSecretCipher`` (the same cipher
    and key as ``workspace_env_vars``) and is never returned to a client -- responses carry
    only a constant mask. It is nullable because local runtimes (Ollama, LM Studio, vLLM)
    need no key at all.
    """

    __tablename__ = "platform_llm_settings"
    __table_args__ = (
        UniqueConstraint("singleton", name="uq_platform_llm_settings_singleton"),
        CheckConstraint("singleton is true", name="ck_platform_llm_settings_singleton"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    singleton: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default=text("true"),
    )
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    # String(160) matches ``AgentRun.model``: ``normalize_model_name`` prepends ``openai/``
    # (7 chars) before the value is stamped onto a run, so the request schema caps at 150.
    model: Mapped[str] = mapped_column(String(160), nullable=False)
    encrypted_api_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "users.id",
            ondelete="SET NULL",
            name="fk_platform_llm_settings_updated_by_user_id",
        ),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
