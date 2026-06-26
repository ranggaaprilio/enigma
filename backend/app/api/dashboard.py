from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.db.session import get_db_session
from app.models import User
from app.schemas.dashboard import DashboardSummaryResponse
from app.services.dashboard import SqlAlchemyDashboardService

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

DB_SESSION_DEP = Depends(get_db_session)


async def get_dashboard_service(
    session: AsyncSession = DB_SESSION_DEP,
) -> SqlAlchemyDashboardService:
    return SqlAlchemyDashboardService(session)


DASHBOARD_SERVICE_DEP = Depends(get_dashboard_service)


@router.get("/summary", response_model=DashboardSummaryResponse)
async def get_dashboard_summary(
    current_user: User = CURRENT_USER_DEP,
    svc: SqlAlchemyDashboardService = DASHBOARD_SERVICE_DEP,
) -> DashboardSummaryResponse:
    return await svc.summary(current_user.id)
