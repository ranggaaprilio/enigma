import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import PreviewSession, Workspace
from app.services.audit import AuditLogService
from app.services.workspaces import WorkspaceNotFoundError

COMMON_PREVIEW_PORTS = (3000, 5173, 8000, 8080)
PREVIEW_BIND_HOST = "127.0.0.1"


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
        workspace_root: Path,
        bind_host: str = PREVIEW_BIND_HOST,
        common_ports: tuple[int, ...] = COMMON_PREVIEW_PORTS,
    ) -> None:
        self.session = session
        self.workspace_root = workspace_root
        self.bind_host = bind_host
        self.common_ports = common_ports

    async def list_for_workspace(self, workspace_id: UUID, user_id: UUID) -> list[PreviewSession]:
        workspace = await self._get_workspace(workspace_id, user_id)
        result = await self.session.execute(
            select(PreviewSession)
            .where(PreviewSession.workspace_id == workspace.id)
            .order_by(PreviewSession.last_seen_at.desc())
        )
        return list(result.scalars().all())

    async def detect(self, workspace_id: UUID, user_id: UUID) -> PreviewSession:
        workspace = await self._get_workspace(workspace_id, user_id)
        self._workspace_path(workspace)
        port = await self._first_open_port()
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
            .order_by(PreviewSession.last_seen_at.desc())
            .limit(1)
        )
        preview_session = result.scalar_one_or_none()
        is_new_session = preview_session is None
        if preview_session is None:
            preview_session = PreviewSession(
                workspace_id=workspace.id,
                port=port,
                public_url="",
                status="active",
                created_at=now,
                last_seen_at=now,
            )
            self.session.add(preview_session)
            await self.session.flush()
            preview_session.public_url = self.proxy_url(preview_session)
        else:
            preview_session.last_seen_at = now
            preview_session.public_url = self.proxy_url(preview_session)
            self.session.add(preview_session)

        await self.session.commit()
        await self.session.refresh(preview_session)
        if is_new_session:
            await AuditLogService(self.session).record(
                user_id=user_id,
                workspace_id=workspace.id,
                action="preview_session_detect",
                target_type="preview_session",
                target_id=preview_session.id,
                metadata={"port": preview_session.port},
            )
        return preview_session

    async def refresh(
        self,
        workspace_id: UUID,
        preview_session_id: UUID,
        user_id: UUID,
    ) -> PreviewSession:
        _workspace, preview_session = await self.get_for_workspace(
            workspace_id,
            preview_session_id,
            user_id,
        )
        preview_session.last_seen_at = datetime.now(UTC)
        preview_session.status = (
            "active" if await self._port_is_open(preview_session.port) else "error"
        )
        self.session.add(preview_session)
        await self.session.commit()
        await self.session.refresh(preview_session)
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace_id,
            action="preview_session_refresh",
            target_type="preview_session",
            target_id=preview_session.id,
            metadata={"port": preview_session.port, "status": preview_session.status},
        )
        return preview_session

    async def stop(
        self,
        workspace_id: UUID,
        preview_session_id: UUID,
        user_id: UUID,
    ) -> PreviewSession:
        _workspace, preview_session = await self.get_for_workspace(
            workspace_id,
            preview_session_id,
            user_id,
        )
        preview_session.status = "stopped"
        preview_session.last_seen_at = datetime.now(UTC)
        self.session.add(preview_session)
        await self.session.commit()
        await self.session.refresh(preview_session)
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace_id,
            action="preview_session_stop",
            target_type="preview_session",
            target_id=preview_session.id,
            metadata={"port": preview_session.port},
        )
        return preview_session

    async def get_proxy_session(
        self,
        workspace_id: UUID,
        preview_session_id: UUID,
        user_id: UUID,
    ) -> PreviewSession:
        _workspace, preview_session = await self.get_for_workspace(
            workspace_id,
            preview_session_id,
            user_id,
        )
        if preview_session.status != "active":
            raise PreviewSessionNotFoundError
        preview_session.last_seen_at = datetime.now(UTC)
        self.session.add(preview_session)
        await self.session.commit()
        await self.session.refresh(preview_session)
        return preview_session

    async def get_for_workspace(
        self,
        workspace_id: UUID,
        preview_session_id: UUID,
        user_id: UUID,
    ) -> tuple[Workspace, PreviewSession]:
        workspace = await self._get_workspace(workspace_id, user_id)
        preview_session = await self.session.get(PreviewSession, preview_session_id)
        if preview_session is None or preview_session.workspace_id != workspace.id:
            raise PreviewSessionNotFoundError
        return workspace, preview_session

    def proxy_url(self, preview_session: PreviewSession) -> str:
        return (
            f"/workspaces/{preview_session.workspace_id}/preview/proxy/"
            f"{preview_session.id}/"
        )

    def upstream_http_url(self, preview_session: PreviewSession, path: str) -> str:
        suffix = f"/{path}" if path else "/"
        return f"http://{self.bind_host}:{preview_session.port}{suffix}"

    def upstream_ws_url(self, preview_session: PreviewSession, path: str) -> str:
        suffix = f"/{path}" if path else "/"
        return f"ws://{self.bind_host}:{preview_session.port}{suffix}"

    async def _get_workspace(self, workspace_id: UUID, user_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != user_id:
            raise WorkspaceNotFoundError
        return workspace

    def _workspace_path(self, workspace: Workspace) -> Path:
        if workspace.local_path is None:
            raise PreviewWorkspacePathError
        root = self.workspace_root.resolve()
        path = Path(workspace.local_path).resolve()
        if path == root or root not in path.parents or not path.is_dir():
            raise PreviewWorkspacePathError
        return path

    async def _first_open_port(self) -> int | None:
        for port in self.common_ports:
            if await self._port_is_open(port):
                return port
        return None

    async def _port_is_open(self, port: int) -> bool:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.bind_host, port),
                timeout=0.3,
            )
        except OSError:
            return False
        except TimeoutError:
            return False
        writer.close()
        await writer.wait_closed()
        return True
