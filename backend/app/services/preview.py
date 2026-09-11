from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import PreviewSession, Workspace, WorkspaceWorktree
from app.services.audit import AuditLogService
from app.services.daytona_runtime import (
    DaytonaRuntime,
    DaytonaRuntimeError,
    get_daytona_runtime,
)
from app.services.preview_capability import (
    InvalidPreviewCapabilityError,
    capability_from_host,
    create_preview_proxy_url,
    decode_preview_capability,
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
                preview.public_url = self._proxy_url(preview)
                self.session.add(preview)
        await self.session.commit()
        for preview in sessions:
            await self._attach_branch(workspace, preview)
        return sessions

    async def upsert_for_service(
        self,
        workspace: Workspace,
        *,
        service_id: UUID,
        port: int,
        worktree_id: UUID | None,
    ) -> PreviewSession:
        """One active preview session per (workspace, port), owned by a running
        `workspace_services` row (Epic 11). A partial unique index on active
        (workspace_id, port) guards the race between two services claiming the same port
        concurrently; losing that race just means re-reading the winner's row."""
        now = datetime.now(UTC)
        preview = await self._active_session_for_port(workspace, port)
        if preview is None:
            preview = PreviewSession(
                workspace_id=workspace.id,
                port=port,
                public_url="",
                status="active",
                created_at=now,
                last_seen_at=now,
                service_id=service_id,
                worktree_id=worktree_id,
            )
            self.session.add(preview)
            try:
                await self.session.flush()
            except IntegrityError:
                await self.session.rollback()
                preview = await self._active_session_for_port(workspace, port)
                if preview is None:
                    raise
        preview.service_id = service_id
        preview.worktree_id = worktree_id
        preview.last_seen_at = now
        preview.public_url = self._proxy_url(preview)
        self.session.add(preview)
        await self.session.commit()
        await self.session.refresh(preview)
        return preview

    async def stop_for_service(self, workspace: Workspace, service_id: UUID) -> None:
        """Mark every active preview session owned by a service as stopped, so its URL stops
        resolving the moment the service stops — no separate proxy-side change needed since
        the proxy already rejects a non-active session (`preview_proxy.py`)."""
        result = await self.session.execute(
            select(PreviewSession).where(
                PreviewSession.workspace_id == workspace.id,
                PreviewSession.service_id == service_id,
                PreviewSession.status == "active",
            )
        )
        now = datetime.now(UTC)
        sessions = list(result.scalars().all())
        for preview in sessions:
            preview.status = "stopped"
            preview.public_url = ""
            preview.last_seen_at = now
            self.session.add(preview)
        if sessions:
            await self.session.commit()

    async def _active_session_for_port(
        self, workspace: Workspace, port: int
    ) -> PreviewSession | None:
        result = await self.session.execute(
            select(PreviewSession)
            .where(
                PreviewSession.workspace_id == workspace.id,
                PreviewSession.port == port,
                PreviewSession.status == "active",
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def detect(self, workspace_id: UUID, user_id: UUID) -> PreviewSession:
        workspace = await self._get_workspace(workspace_id, user_id)
        port = await self._first_open_port(workspace)
        if port is None:
            raise PreviewNotFoundError
        now = datetime.now(UTC)
        preview = await self._active_session_for_port(workspace, port)
        is_new = preview is None
        if preview is None:
            preview = PreviewSession(
                workspace_id=workspace.id,
                port=port,
                public_url="",
                status="active",
                created_at=now,
                last_seen_at=now,
            )
            self.session.add(preview)
            try:
                await self.session.flush()
            except IntegrityError:
                # A workspace_services-driven upsert_for_service claimed this port
                # concurrently — the partial unique index on active (workspace_id, port)
                # rejects our insert; fall back to whichever row won.
                await self.session.rollback()
                preview = await self._active_session_for_port(workspace, port)
                is_new = False
                if preview is None:
                    raise
        else:
            preview.last_seen_at = now
        # Tag the preview with whichever worktree the dev server is running in, so the
        # UI can show which branch is being previewed (None = base clone).
        preview.worktree_id = workspace.dev_worktree_id
        preview.public_url = self._proxy_url(preview)
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
                metadata={
                    "port": port,
                    "worktree_id": str(preview.worktree_id) if preview.worktree_id else None,
                },
            )
        return await self._attach_branch(workspace, preview)

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
            preview.public_url = self._proxy_url(preview)
        else:
            preview.status = "error"
        preview.last_seen_at = datetime.now(UTC)
        self.session.add(preview)
        await self.session.commit()
        await self.session.refresh(preview)
        return await self._attach_branch(workspace, preview)

    def _proxy_url(self, preview: PreviewSession) -> str:
        ttl_seconds = min(
            self.settings.daytona_preview_proxy_ttl_seconds,
            self.settings.daytona_preview_url_ttl_seconds,
        )
        renewal_window = timedelta(seconds=min(60, max(1, ttl_seconds // 10)))
        try:
            parsed = urlsplit(preview.public_url)
            capability_value = capability_from_host(
                parsed.netloc,
                base_url=self.settings.daytona_preview_proxy_base_url,
            )
            capability = decode_preview_capability(
                capability_value,
                secret_key=self.settings.auth_secret_key,
            )
            if (
                capability.preview_session_id == preview.id
                and capability.port == preview.port
                and capability.expires_at - datetime.now(UTC) > renewal_window
            ):
                return preview.public_url
        except (InvalidPreviewCapabilityError, ValueError):
            pass
        return create_preview_proxy_url(
            preview.id,
            preview.port,
            base_url=self.settings.daytona_preview_proxy_base_url,
            secret_key=self.settings.auth_secret_key,
            ttl_seconds=ttl_seconds,
        )

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
        return await self._attach_branch(_workspace, preview)

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

    async def _attach_branch(
        self, workspace: Workspace, preview: PreviewSession
    ) -> PreviewSession:
        """Attach the (non-persisted) branch name for the preview's worktree so the
        response tells the client which branch is being previewed."""
        preview.branch = await self._branch_for(workspace, preview.worktree_id)
        return preview

    async def _branch_for(self, workspace: Workspace, worktree_id: UUID | None) -> str:
        if worktree_id is None:
            return workspace.default_branch
        row = await self.session.get(WorkspaceWorktree, worktree_id)
        if row is None or row.workspace_id != workspace.id:
            return workspace.default_branch
        return row.branch

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
