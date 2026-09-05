from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import CURRENT_USER_DEP
from app.db.session import get_db_session
from app.models import User
from app.schemas.templates import WorkspaceTemplateResponse
from app.services.templates import TemplateService

router = APIRouter(prefix="/workspace-templates", tags=["workspace-templates"])

DB_SESSION_DEP = Depends(get_db_session)


async def get_template_service(session: AsyncSession = DB_SESSION_DEP) -> TemplateService:
    return TemplateService(session)


TEMPLATE_SERVICE_DEP = Depends(get_template_service)


@router.get("", response_model=list[WorkspaceTemplateResponse])
async def list_workspace_templates(
    _current_user: User = CURRENT_USER_DEP,
    service: TemplateService = TEMPLATE_SERVICE_DEP,
) -> list[WorkspaceTemplateResponse]:
    templates = await service.list_active()
    return [WorkspaceTemplateResponse.model_validate(template) for template in templates]
