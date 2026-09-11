import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Both kinds are detected purely by observing proxied HTTP traffic in ``preview_proxy.py`` --
# never by injecting a CORS header ourselves, which would mask a real misconfiguration.
SERVICE_DIAGNOSTIC_KINDS = ("cors_missing_allow_origin", "cors_preflight_rejected")


class ServiceDiagnostic(Base):
    """An observed, recurring problem with one service's traffic (currently: CORS).

    Rows are events, not derivations -- contrast with a workspace advisory (e.g. "no dev proxy
    configured"), which is computed fresh on every read because it has no natural invalidation
    point. A CORS rejection is the opposite: it happens inside a proxied request that has no
    response object to attach a diagnosis to, so it must be recorded somewhere to be surfaced
    later. The upsert key (workspace_id, service_id, kind, origin) deliberately excludes the
    request path -- an SPA hitting many paths from one origin must not mint one row per path.
    """

    __tablename__ = "service_diagnostics"
    __table_args__ = (
        CheckConstraint(
            "kind in ('cors_missing_allow_origin', 'cors_preflight_rejected')",
            name="ck_service_diagnostics_kind",
        ),
        CheckConstraint("occurrences >= 1", name="ck_service_diagnostics_occurrences"),
        CheckConstraint(
            "status_code is null or (status_code between 100 and 599)",
            name="ck_service_diagnostics_status_code",
        ),
        UniqueConstraint(
            "workspace_id",
            "service_id",
            "kind",
            "origin",
            name="uq_service_diagnostics_service_kind_origin",
        ),
        Index("ix_service_diagnostics_workspace_id", "workspace_id"),
        Index("ix_service_diagnostics_service_id", "service_id"),
        Index("ix_service_diagnostics_last_seen_at", "last_seen_at"),
        # The composition endpoint only ever reads unresolved rows for a workspace.
        Index(
            "ix_service_diagnostics_unresolved",
            "workspace_id",
            "last_seen_at",
            postgresql_where=text("resolved_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Denormalised from service_id for the composition query's workspace-scoped read; also
    # participates in the cascade so a deleted workspace takes its diagnoses with it.
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
    )
    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspace_services.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Which preview session was active when this was observed. SET NULL (not CASCADE) so the
    # diagnosis -- which describes the *service*, not the session -- outlives a session rotation.
    preview_session_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("preview_sessions.id", ondelete="SET NULL"),
        nullable=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    origin: Mapped[str] = mapped_column(String(255), nullable=False)
    request_method: Mapped[str | None] = mapped_column(String(16), nullable=True)
    required_header: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Path only -- never the query string, which can carry a preview capability.
    request_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurrences: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # Cleared back to NULL the moment a later cross-origin response for the same key carries a
    # satisfying header, so a fixed app clears its own diagnosis rather than staying red forever.
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
