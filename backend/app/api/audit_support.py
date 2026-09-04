"""Shared audit-log wiring for the API layer.

Every router that writes audit entries needs the same three things: a service provider to
depend on, the `Depends(...)` marker, and a best-effort writer. Keeping one copy here means
the "an audit failure must never break the primary action" rule is implemented once.
"""

import structlog
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session
from app.services.audit import AuditLogService

logger = structlog.get_logger("app.audit")

_DB_SESSION_DEP = Depends(get_db_session)


async def get_audit_log_service(session: AsyncSession = _DB_SESSION_DEP) -> AuditLogService:
    return AuditLogService(session)


AUDIT_LOG_SERVICE_DEP = Depends(get_audit_log_service)


async def record_audit_safely(audit_log_service: AuditLogService, **values) -> None:
    """Best-effort audit write — an audit failure must never break the primary action."""
    try:
        await audit_log_service.record(**values)
    except Exception:
        logger.exception("audit.record_failed", action=values.get("action"))
