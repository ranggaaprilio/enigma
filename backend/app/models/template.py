import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class WorkspaceTemplate(Base):
    """A starter repository users can create a workspace from.

    ``source_repo_url`` is cloned (shallow, unauthenticated) into a fresh sandbox, its history is
    dropped, and the result becomes the workspace's first commit. ``mise_config`` is written as
    ``mise.toml`` when the template ships none so the dev-server preview has a ``dev`` task.
    """

    __tablename__ = "workspace_templates"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_workspace_templates_slug"),
        Index("ix_workspace_templates_active_sort", "is_active", "sort_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    source_repo_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    default_branch: Mapped[str] = mapped_column(
        String(120), nullable=False, default="main", server_default="main"
    )
    icon: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(40)),
        nullable=False,
        default=list,
        server_default=text("'{}'::varchar[]"),
    )
    mise_config: Mapped[str | None] = mapped_column(Text, nullable=True)
    preview_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    is_active: Mapped[bool] = mapped_column(
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
