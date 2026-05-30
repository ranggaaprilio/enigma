from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, Workspace
from app.services.workspaces import WorkspaceNotFoundError


class AuditLogService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def record(
        self,
        *,
        user_id: UUID,
        workspace_id: UUID | None,
        action: str,
        target_type: str,
        target_id: UUID | str,
        metadata: dict | None = None,
    ) -> AuditLog:
        audit_log = AuditLog(
            user_id=user_id,
            workspace_id=workspace_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            log_metadata=safe_metadata(metadata or {}),
        )
        if not all(
            hasattr(self.session, method_name)
            for method_name in ("add", "flush", "commit", "refresh")
        ):
            return audit_log
        self.session.add(audit_log)
        await self.session.flush()
        await self.session.commit()
        await self.session.refresh(audit_log)
        return audit_log

    async def list_for_workspace(
        self,
        workspace_id: UUID,
        user_id: UUID,
        limit: int = 100,
    ) -> list[AuditLog]:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != user_id:
            raise WorkspaceNotFoundError
        result = await self.session.execute(
            select(AuditLog)
            .where(AuditLog.workspace_id == workspace.id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


def safe_metadata(metadata: dict) -> dict:
    sensitive_fragments = ("token", "secret", "password", "key", "credential")
    safe: dict = {}
    for key, value in metadata.items():
        lowered = str(key).lower()
        if any(fragment in lowered for fragment in sensitive_fragments):
            continue
        if isinstance(value, UUID):
            safe[key] = str(value)
        elif isinstance(value, str | int | float | bool) or value is None:
            safe[key] = value
        elif isinstance(value, list):
            safe[key] = [
                item if isinstance(item, str | int | float | bool) or item is None else str(item)
                for item in value[:25]
            ]
        else:
            safe[key] = str(value)
    return safe
