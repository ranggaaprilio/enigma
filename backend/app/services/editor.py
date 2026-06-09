import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from uuid import UUID, uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import EditorSession, Workspace
from app.services.audit import AuditLogService
from app.services.daytona_runtime import (
    DaytonaRuntime,
    DaytonaRuntimeError,
    get_daytona_runtime,
)
from app.services.workspaces import WorkspaceNotFoundError

logger = structlog.get_logger(__name__)


class EditorSessionNotFoundError(Exception):
    pass


class EditorWorkspacePathError(Exception):
    pass


class EditorRuntimeError(Exception):
    pass


class EditorTokenError(Exception):
    pass


class EditorService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        runtime: DaytonaRuntime | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.runtime = runtime or get_daytona_runtime()

    async def start_or_reuse(self, workspace_id: UUID, user_id: UUID) -> EditorSession:
        workspace = await self._get_workspace(workspace_id, user_id)
        if not workspace.daytona_sandbox_id:
            raise EditorWorkspacePathError
        existing = await self._latest_for_workspace(workspace.id, user_id)
        try:
            await self.runtime.exec(
                workspace,
                (
                    f"command -v {self.settings.editor_code_server_binary} >/dev/null && "
                    f"(pgrep -f '{self.settings.editor_code_server_binary}.*"
                    f"{self.settings.editor_port}' >/dev/null || "
                    f"nohup {self.settings.editor_code_server_binary} --auth none "
                    f"--bind-addr 0.0.0.0:{self.settings.editor_port} "
                    f"{quote(workspace.workspace_path, safe='/')} "
                    ">/tmp/execute-code-server.log 2>&1 &)"
                ),
                cwd=workspace.workspace_path,
                timeout=20,
            )
            signed_url = await self.runtime.signed_preview_url(
                workspace,
                self.settings.editor_port,
            )
        except DaytonaRuntimeError as error:
            raise EditorRuntimeError(str(error)) from error
        editor_url = self._editor_url(signed_url, workspace.workspace_path)
        now = datetime.now(UTC)
        if existing and existing.status == "active":
            existing.url = editor_url
            existing.last_active_at = now
            existing.token_expires_at = now + timedelta(
                seconds=self.settings.daytona_preview_url_ttl_seconds
            )
            self.session.add(existing)
            await self.session.commit()
            await self.session.refresh(existing)
            return existing

        editor_session = EditorSession(
            id=uuid4(),
            workspace_id=workspace.id,
            user_id=user_id,
            url=editor_url,
            status="active",
            process_id=None,
            bind_host="daytona",
            bind_port=self.settings.editor_port,
            token_hash=hashlib.sha256(editor_url.encode()).hexdigest(),
            token_expires_at=now
            + timedelta(seconds=self.settings.daytona_preview_url_ttl_seconds),
            started_at=now,
            last_active_at=now,
        )
        self.session.add(editor_session)
        await self.session.commit()
        await self.session.refresh(editor_session)
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace.id,
            action="editor_session_start",
            target_type="editor_session",
            target_id=editor_session.id,
            metadata={"port": self.settings.editor_port},
        )
        return editor_session

    async def status(self, workspace_id: UUID, user_id: UUID) -> EditorSession | None:
        workspace = await self._get_workspace(workspace_id, user_id)
        editor_session = await self._latest_for_workspace(workspace.id, user_id)
        if editor_session and editor_session.status == "active":
            if editor_session.token_expires_at <= datetime.now(UTC):
                try:
                    signed_url = await self.runtime.signed_preview_url(
                        workspace,
                        editor_session.bind_port,
                    )
                except DaytonaRuntimeError as error:
                    raise EditorRuntimeError(str(error)) from error
                editor_session.url = self._editor_url(signed_url, workspace.workspace_path)
                editor_session.token_expires_at = datetime.now(UTC) + timedelta(
                    seconds=self.settings.daytona_preview_url_ttl_seconds
                )
                self.session.add(editor_session)
                await self.session.commit()
                await self.session.refresh(editor_session)
        return editor_session

    async def stop(self, workspace_id: UUID, user_id: UUID) -> EditorSession:
        workspace = await self._get_workspace(workspace_id, user_id)
        editor_session = await self._latest_for_workspace(workspace.id, user_id)
        if editor_session is None:
            raise EditorSessionNotFoundError
        try:
            await self.runtime.exec(
                workspace,
                (
                    f"pkill -f '{self.settings.editor_code_server_binary}.*"
                    f"{editor_session.bind_port}' || true"
                ),
                cwd=workspace.workspace_path,
                timeout=10,
            )
        except DaytonaRuntimeError:
            pass
        editor_session.status = "stopped"
        editor_session.last_active_at = datetime.now(UTC)
        self.session.add(editor_session)
        await self.session.commit()
        await self.session.refresh(editor_session)
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace.id,
            action="editor_session_stop",
            target_type="editor_session",
            target_id=editor_session.id,
            metadata={},
        )
        return editor_session

    async def _get_workspace(self, workspace_id: UUID, user_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != user_id:
            raise WorkspaceNotFoundError
        return workspace

    async def _latest_for_workspace(
        self,
        workspace_id: UUID,
        user_id: UUID,
    ) -> EditorSession | None:
        result = await self.session.execute(
            select(EditorSession)
            .where(EditorSession.workspace_id == workspace_id, EditorSession.user_id == user_id)
            .order_by(EditorSession.last_active_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    def _editor_url(self, signed_url: str, workspace_path: str) -> str:
        separator = "&" if "?" in signed_url else "?"
        return f"{signed_url}{separator}folder={quote(workspace_path, safe='/')}"
