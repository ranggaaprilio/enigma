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
            result = await self.runtime.exec(
                workspace,
                self._ensure_command(workspace.workspace_path),
                cwd=workspace.workspace_path,
                timeout=self.settings.editor_install_timeout_seconds,
            )
            if result.exit_code != 0:
                logger.warning(
                    "editor_code_server_unavailable",
                    workspace_id=str(workspace.id),
                    exit_code=result.exit_code,
                    output=result.stdout[-500:],
                )
                raise EditorRuntimeError(
                    "code-server could not be started in the sandbox. "
                    "Install it in the Daytona snapshot or enable editor auto-install."
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

    def _ensure_command(self, workspace_path: str) -> str:
        binary = self.settings.editor_code_server_binary
        port = self.settings.editor_port
        folder = quote(self._absolute_workspace_path(workspace_path), safe="/")
        run_log = "/tmp/execute-code-server.log"
        install_log = "/tmp/execute-code-server-install.log"
        # Make a user-local install discoverable without needing sudo/PATH reloads.
        lines = ['export PATH="$HOME/.local/bin:$PATH"']
        if self.settings.editor_auto_install:
            lines.append(
                f"if ! command -v {binary} >/dev/null 2>&1; then "
                f"curl -fsSL https://code-server.dev/install.sh "
                f"| sh -s -- --method standalone --prefix \"$HOME/.local\" "
                f">{install_log} 2>&1; fi"
            )
        lines.extend(
            [
                # Fail loudly if the binary still isn't there so the API never hands back
                # a preview URL that points at a port nothing is listening on.
                f"command -v {binary} >/dev/null 2>&1 || exit 127",
                (
                    f"pgrep -f '{binary}.*{port}' >/dev/null 2>&1 || "
                    f"nohup {binary} --auth none --bind-addr 0.0.0.0:{port} "
                    f"{folder} >{run_log} 2>&1 &"
                ),
            ]
        )
        return "\n".join(lines)

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
        folder = quote(self._absolute_workspace_path(workspace_path), safe="/")
        return f"{signed_url}{separator}folder={folder}"

    def _absolute_workspace_path(self, workspace_path: str) -> str:
        # code-server resolves the `folder=` query param as an absolute path inside the
        # sandbox, so a relative workspace path (e.g. "workspace") must be anchored to the
        # sandbox home (e.g. "/home/daytona/workspace").
        if workspace_path.startswith("/"):
            return workspace_path
        home = self.settings.daytona_sandbox_home.rstrip("/")
        return f"{home}/{workspace_path}"
