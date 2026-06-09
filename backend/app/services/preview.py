from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import PreviewSession, Workspace
from app.services.audit import AuditLogService
from app.services.daytona_runtime import (
    DaytonaRuntime,
    DaytonaRuntimeError,
    get_daytona_runtime,
)
from app.services.workspaces import WorkspaceNotFoundError

COMMON_PREVIEW_PORTS = (3000, 5173, 8000, 8080)


class PreviewSessionNotFoundError(Exception):
    pass


class PreviewWorkspacePathError(Exception):
    pass


class PreviewNotFoundError(Exception):
    pass


class PreviewService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        runtime: DaytonaRuntime | None = None,
        common_ports: tuple[int, ...] = COMMON_PREVIEW_PORTS,
    ) -> None:
        self.session = session
        self.settings = settings
        self.runtime = runtime or get_daytona_runtime()
        self.common_ports = common_ports

    async def list_for_workspace(self, workspace_id: UUID, user_id: UUID) -> list[PreviewSession]:
        workspace = await self._get_workspace(workspace_id, user_id)
        result = await self.session.execute(
            select(PreviewSession)
            .where(PreviewSession.workspace_id == workspace.id)
            .order_by(PreviewSession.last_seen_at.desc())
        )
        sessions = list(result.scalars().all())
        for preview in sessions:
            if preview.status == "active":
                try:
                    preview.public_url = await self.runtime.signed_preview_url(
                        workspace,
                        preview.port,
                    )
                except DaytonaRuntimeError:
                    preview.status = "error"
                self.session.add(preview)
        await self.session.commit()
        return sessions

    async def detect(self, workspace_id: UUID, user_id: UUID) -> PreviewSession:
        workspace = await self._get_workspace(workspace_id, user_id)
        port = await self._first_open_port(workspace)
        if port is None:
            raise PreviewNotFoundError
        now = datetime.now(UTC)
        result = await self.session.execute(
            select(PreviewSession)
            .where(
                PreviewSession.workspace_id == workspace.id,
                PreviewSession.port == port,
                PreviewSession.status == "active",
            )
            .limit(1)
        )
        preview = result.scalar_one_or_none()
        is_new = preview is None
        public_url = await self.runtime.signed_preview_url(workspace, port)
        if preview is None:
            preview = PreviewSession(
                workspace_id=workspace.id,
                port=port,
                public_url=public_url,
                status="active",
                created_at=now,
                last_seen_at=now,
            )
        else:
            preview.public_url = public_url
            preview.last_seen_at = now
        self.session.add(preview)
        await self.session.commit()
        await self.session.refresh(preview)
        if is_new:
            await AuditLogService(self.session).record(
                user_id=user_id,
                workspace_id=workspace.id,
                action="preview_session_detect",
                target_type="preview_session",
                target_id=preview.id,
                metadata={"port": port},
            )
        return preview

    async def refresh(
        self,
        workspace_id: UUID,
        preview_session_id: UUID,
        user_id: UUID,
    ) -> PreviewSession:
        workspace, preview = await self.get_for_workspace(
            workspace_id,
            preview_session_id,
            user_id,
        )
        if await self._port_is_open(workspace, preview.port):
            preview.status = "active"
            preview.public_url = await self.runtime.signed_preview_url(workspace, preview.port)
        else:
            preview.status = "error"
        preview.last_seen_at = datetime.now(UTC)
        self.session.add(preview)
        await self.session.commit()
        await self.session.refresh(preview)
        return preview

    async def stop(
        self,
        workspace_id: UUID,
        preview_session_id: UUID,
        user_id: UUID,
    ) -> PreviewSession:
        _workspace, preview = await self.get_for_workspace(
            workspace_id,
            preview_session_id,
            user_id,
        )
        preview.status = "stopped"
        preview.last_seen_at = datetime.now(UTC)
        preview.public_url = ""
        self.session.add(preview)
        await self.session.commit()
        await self.session.refresh(preview)
        return preview

    async def get_for_workspace(
        self,
        workspace_id: UUID,
        preview_session_id: UUID,
        user_id: UUID,
    ) -> tuple[Workspace, PreviewSession]:
        workspace = await self._get_workspace(workspace_id, user_id)
        preview = await self.session.get(PreviewSession, preview_session_id)
        if preview is None or preview.workspace_id != workspace.id:
            raise PreviewSessionNotFoundError
        return workspace, preview

    async def _get_workspace(self, workspace_id: UUID, user_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != user_id:
            raise WorkspaceNotFoundError
        if not workspace.daytona_sandbox_id:
            raise PreviewWorkspacePathError
        return workspace

    async def _first_open_port(self, workspace: Workspace) -> int | None:
        for port in self.common_ports:
            if await self._port_is_open(workspace, port):
                return port
        return None

    async def _port_is_open(self, workspace: Workspace, port: int) -> bool:
        code = (
            "import socket; "
            "s=socket.socket(); s.settimeout(.3); "
            f"r=s.connect_ex(('127.0.0.1',{port})); s.close(); "
            "raise SystemExit(0 if r == 0 else 1)"
        )
        try:
            result = await self.runtime.exec(
                workspace,
                f"python3 -c \"{code}\"",
                cwd=workspace.workspace_path,
                timeout=3,
            )
        except DaytonaRuntimeError:
            return False
        return result.exit_code == 0
