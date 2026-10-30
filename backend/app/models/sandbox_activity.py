import uuid
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SandboxActivity(Base):
    """When a workspace's sandbox was last used (local-docker-runtime Epic 05).

    Written, debounced, by whichever process touched the sandbox (API, worker or preview
    proxy), so the idle clock survives restarts and is shared across processes. Read by
    the Docker idle reaper; Daytona enforces auto-stop itself and never writes here.
    """

    __tablename__ = "sandbox_activity"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SandboxInstallation(Base):
    """This database's identity on a shared container daemon (Epic 05): one row, one id.

    Docker sandboxes and networks carry it as a label, and the upkeep loop only touches
    those, so another Enigma stack on the same daemon is never mistaken for orphans."""

    __tablename__ = "sandbox_installation"
    __table_args__ = (
        UniqueConstraint("singleton", name="uq_sandbox_installation_singleton"),
        CheckConstraint("singleton", name="ck_sandbox_installation_singleton"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    singleton: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
