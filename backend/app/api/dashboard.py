from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.db.session import get_db_session
from app.models import User
from app.schemas.dashboard import DashboardSummaryResponse
from app.schemas.usage import UsageScope, UsageSummaryResponse
from app.services.dashboard import SqlAlchemyDashboardService
from app.services.usage import SqlAlchemyUsageService, UsageAccessError, UsageScopeError

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

DB_SESSION_DEP = Depends(get_db_session)

DEFAULT_USAGE_WINDOW_DAYS = 30


async def get_dashboard_service(
    session: AsyncSession = DB_SESSION_DEP,
) -> SqlAlchemyDashboardService:
    return SqlAlchemyDashboardService(session)


DASHBOARD_SERVICE_DEP = Depends(get_dashboard_service)


async def get_usage_service(session: AsyncSession = DB_SESSION_DEP) -> SqlAlchemyUsageService:
    return SqlAlchemyUsageService(session)


USAGE_SERVICE_DEP = Depends(get_usage_service)


def _ensure_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@router.get("/summary", response_model=DashboardSummaryResponse)
async def get_dashboard_summary(
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyDashboardService = DASHBOARD_SERVICE_DEP,
) -> DashboardSummaryResponse:
    return await svc.summary(current_user.id)


@router.get("/usage", response_model=UsageSummaryResponse)
async def get_usage_summary(
    scope: Annotated[UsageScope | None, Query()] = None,
    scope_id: Annotated[UUID | None, Query(alias="id")] = None,
    date_from: Annotated[datetime | None, Query(alias="from")] = None,
    date_to: Annotated[datetime | None, Query(alias="to")] = None,
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyUsageService = USAGE_SERVICE_DEP,
) -> UsageSummaryResponse:
    resolved_to = _ensure_aware(date_to) if date_to else datetime.now(UTC)
    resolved_from = (
        _ensure_aware(date_from)
        if date_from
        else resolved_to - timedelta(days=DEFAULT_USAGE_WINDOW_DAYS)
    )
    try:
        return await svc.usage_summary(
            current_user.id, scope, scope_id, resolved_from, resolved_to
        )
    except UsageAccessError as error:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(error)
        ) from None
    except UsageScopeError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(error)
        ) from None
