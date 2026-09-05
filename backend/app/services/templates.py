from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WorkspaceTemplate


class TemplateNotFoundError(Exception):
    pass


class TemplateService:
    """Read access to the built-in starter catalog (``workspace_templates``)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def list_active(self) -> list[WorkspaceTemplate]:
        result = await self.session.execute(
            select(WorkspaceTemplate)
            .where(WorkspaceTemplate.is_active.is_(True))
            .order_by(WorkspaceTemplate.sort_order, WorkspaceTemplate.name)
        )
        return list(result.scalars().all())

    async def get(self, template_id: UUID) -> WorkspaceTemplate:
        template = await self.session.get(WorkspaceTemplate, template_id)
        if template is None or not template.is_active:
            raise TemplateNotFoundError
        return template
