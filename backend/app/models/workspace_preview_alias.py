import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# A single DNS label: lowercase alphanumeric, hyphens allowed in the middle, 3-62 chars, at
# least one hyphen (this is what makes it provably disjoint from a 63-char base32 capability
# label -- see ``services/preview_alias.py``), never ``xn--`` (punycode) and never leading `_`.
ALIAS_FORMAT_CHECK = (
    "alias ~ '^[a-z0-9][a-z0-9-]{1,60}[a-z0-9]$' "
    "AND position('-' in alias) > 0 "
    "AND alias NOT LIKE 'xn--%' "
    "AND alias NOT LIKE '\\_%'"
)


class WorkspacePreviewAlias(Base):
    """An opt-in, stable hostname for one service, alongside the always-on rotating capability.

    Keyed on ``custom_command_id`` (nullable -- ``NULL`` means the built-in mise service), never
    on ``workspace_services.id``: ``WorkspaceServiceManager._sync_definitions`` deletes and
    recreates instance rows, so a FK to that table would be silently orphaned the moment a
    workspace's registry changes. The alias must outlive that churn -- it is the whole point.
    """

    __tablename__ = "workspace_preview_aliases"
    __table_args__ = (
        CheckConstraint(ALIAS_FORMAT_CHECK, name="ck_workspace_preview_aliases_alias_format"),
        UniqueConstraint("alias", name="uq_workspace_preview_aliases_alias"),
        UniqueConstraint(
            "workspace_id",
            "custom_command_id",
            name="uq_workspace_preview_aliases_workspace_command",
        ),
        # At most one alias for the built-in (custom_command_id IS NULL) row per workspace --
        # NULLs are distinct in a plain unique constraint, so this needs the partial-index form,
        # exactly like ``uq_workspace_services_builtin``.
        Index(
            "uq_workspace_preview_aliases_builtin",
            "workspace_id",
            unique=True,
            postgresql_where=text("custom_command_id IS NULL"),
        ),
        Index("ix_workspace_preview_aliases_workspace_id", "workspace_id"),
        Index("ix_workspace_preview_aliases_custom_command_id", "custom_command_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    custom_command_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("custom_commands.id", ondelete="CASCADE"),
        nullable=True,
    )
    alias: Mapped[str] = mapped_column(String(62), nullable=False)
    # Per-alias kill switch, independent of the workspace-wide ``preview_alias_enabled`` flag.
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
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
