import json
from datetime import UTC, datetime
from uuid import UUID

import structlog
from daytona import PtySize
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import TerminalSession, Workspace
from app.services.audit import AuditLogService
from app.services.daytona_runtime import (
    DaytonaRuntime,
    DaytonaRuntimeError,
    get_daytona_runtime,
)
from app.services.env_templates import TemplateProblem, enigma_exports, substitute
from app.services.workspace_env import build_template_context
from app.services.workspace_env import resolve as _resolve_env
from app.services.workspaces import WorkspaceNotFoundError

logger = structlog.get_logger(__name__)


class TerminalSessionNotFoundError(Exception):
    pass


class TerminalWorkspacePathError(Exception):
    pass


class TerminalRuntimeError(Exception):
    pass


class TerminalService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        runtime: DaytonaRuntime | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.runtime = runtime or get_daytona_runtime()

    async def create_or_reuse(
        self,
        workspace_id: UUID,
        user_id: UUID,
        name: str,
    ) -> TerminalSession:
        workspace = await self._get_workspace(workspace_id, user_id)
        if not workspace.daytona_sandbox_id:
            raise TerminalWorkspacePathError
        result = await self.session.execute(
            select(TerminalSession)
            .where(
                TerminalSession.workspace_id == workspace.id,
                TerminalSession.user_id == user_id,
                TerminalSession.status == "active",
            )
            .order_by(TerminalSession.last_active_at.desc())
            .limit(1)
        )
        terminal_session = result.scalar_one_or_none()
        is_new = terminal_session is None
        if terminal_session is None:
            terminal_session = TerminalSession(
                workspace_id=workspace.id,
                user_id=user_id,
                name=name,
                pty_session_id=f"execute-{workspace.id.hex}",
                status="active",
                last_active_at=datetime.now(UTC),
            )
        else:
            terminal_session.name = name
            terminal_session.last_active_at = datetime.now(UTC)
        self.session.add(terminal_session)
        await self.session.commit()
        await self.session.refresh(terminal_session)
        if is_new:
            await AuditLogService(self.session).record(
                user_id=user_id,
                workspace_id=workspace.id,
                action="terminal_session_start",
                target_type="terminal_session",
                target_id=terminal_session.id,
                metadata={"name": name},
            )
        return terminal_session

    async def list_for_workspace(self, workspace_id: UUID, user_id: UUID) -> list[TerminalSession]:
        workspace = await self._get_workspace(workspace_id, user_id)
        result = await self.session.execute(
            select(TerminalSession)
            .where(TerminalSession.workspace_id == workspace.id, TerminalSession.user_id == user_id)
            .order_by(TerminalSession.last_active_at.desc())
        )
        return list(result.scalars().all())

    async def get_for_workspace(
        self,
        workspace_id: UUID,
        terminal_session_id: UUID,
        user_id: UUID,
    ) -> tuple[Workspace, TerminalSession]:
        workspace = await self._get_workspace(workspace_id, user_id)
        terminal_session = await self.session.get(TerminalSession, terminal_session_id)
        if (
            terminal_session is None
            or terminal_session.workspace_id != workspace.id
            or terminal_session.user_id != user_id
        ):
            raise TerminalSessionNotFoundError
        return workspace, terminal_session

    async def stop(self, workspace_id: UUID, terminal_session_id: UUID, user_id: UUID) -> None:
        workspace, terminal_session = await self.get_for_workspace(
            workspace_id,
            terminal_session_id,
            user_id,
        )
        try:
            await self.runtime.kill_pty(workspace, terminal_session.pty_session_id)
        except DaytonaRuntimeError:
            pass
        terminal_session.status = "stopped"
        terminal_session.last_active_at = datetime.now(UTC)
        self.session.add(terminal_session)
        await self.session.commit()
        await AuditLogService(self.session).record(
            user_id=user_id,
            workspace_id=workspace_id,
            action="terminal_session_stop",
            target_type="terminal_session",
            target_id=terminal_session.id,
            metadata={"name": terminal_session.name},
        )

    async def touch(self, terminal_session: TerminalSession) -> None:
        terminal_session.last_active_at = datetime.now(UTC)
        self.session.add(terminal_session)
        await self.session.commit()

    async def environment(
        self, workspace_id: UUID, workspace: Workspace
    ) -> tuple[dict[str, str], list[TemplateProblem]]:
        """Unlike every other caller of the template engine, a terminal is *non-strict*: it is
        where you go to debug a bad template, so an unresolvable one is omitted from the
        environment (never emptied) rather than killing the session outright. The caller
        surfaces `problems` as a one-line notice per entry before the PTY attaches."""
        env = await _resolve_env(self.session, self.settings, workspace_id)
        context = await build_template_context(
            self.session,
            self.settings,
            workspace,
            branch=workspace.default_branch,
            self_service_id=None,
        )
        merged = {**enigma_exports(context), **env}
        return substitute(merged, context, strict=False)

    async def _get_workspace(self, workspace_id: UUID, user_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != user_id:
            raise WorkspaceNotFoundError
        return workspace


class TerminalPtyBridge:
    def __init__(
        self,
        runtime: DaytonaRuntime,
        default_cols: int,
        default_rows: int,
    ) -> None:
        self.runtime = runtime
        self.default_cols = default_cols
        self.default_rows = default_rows

    async def bridge(
        self,
        websocket,
        workspace: Workspace,
        terminal_session: TerminalSession,
        env: dict[str, str],
        *,
        cwd: str | None = None,
        pty_session_id: str | None = None,
    ) -> None:
        async def on_data(data: bytes) -> None:
            await websocket.send_bytes(data)

        try:
            handle = await self.runtime.create_or_connect_pty(
                workspace,
                pty_session_id or terminal_session.pty_session_id,
                on_data,
                env=env,
                cols=self.default_cols,
                rows=self.default_rows,
                cwd=cwd,
            )
        except DaytonaRuntimeError as error:
            await websocket.send_json({"type": "error", "message": str(error)})
            await websocket.close()
            return

        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                if data := message.get("bytes"):
                    await handle.send_input(data)
                    continue
                if text := message.get("text"):
                    try:
                        payload = json.loads(text)
                    except json.JSONDecodeError:
                        await handle.send_input(text)
                        continue
                    if payload.get("type") == "resize":
                        cols = max(20, min(int(payload.get("cols", 80)), 400))
                        rows = max(5, min(int(payload.get("rows", 24)), 200))
                        await handle.resize(PtySize(cols=cols, rows=rows))
        finally:
            await handle.disconnect()
            logger.info(
                "terminal_session_disconnect",
                terminal_session_id=str(terminal_session.id),
                workspace_id=str(workspace.id),
                user_id=str(terminal_session.user_id),
            )
