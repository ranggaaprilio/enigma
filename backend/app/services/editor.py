from datetime import UTC, datetime
from urllib.parse import quote
from uuid import UUID, uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models import EditorSession, Workspace
from app.services.audit import AuditLogService
from app.services.editor_alias import (
    editor_alias_url,
    editor_prime_url,
    ensure_editor_alias,
)
from app.services.preview_alias import encode_alias_grant
from app.services.sandbox import (
    SandboxRuntime,
    SandboxRuntimeError,
    get_sandbox_runtime,
)
from app.services.workspaces import WorkspaceNotFoundError
from app.services.worktrees import WorktreeNotFoundError, WorktreeService

logger = structlog.get_logger(__name__)


class EditorSessionNotFoundError(Exception):
    pass


class EditorWorkspacePathError(Exception):
    pass


class EditorRuntimeError(Exception):
    pass


class EditorWorktreeError(Exception):
    pass


class EditorService:
    """Runs code-server inside the sandbox and exposes it on the preview proxy's stable editor
    host, so the dashboard can embed it in an iframe.

    The launch command is unchanged from the new-tab era -- code-server still binds
    ``0.0.0.0:13337`` with ``--auth none`` inside the sandbox, which is never reachable directly.
    On the Docker provider every sandbox shares one bridge network, so there it binds
    ``127.0.0.1`` and the proxy-only port relay (``sandbox/port_relay.py``) carries it.
    What changed is the URL handed to the browser: it used to be a Daytona signed preview URL
    (a bearer credential in the address bar, rotating hourly); it is now
    ``https://editor-<alias>.<proxy-host>/?folder=...``, a stable origin with no credential,
    authorised per-browser by the proxy's grant cookie. Stability is the point: code-server keeps
    its whole workbench state in ``localStorage``, which is keyed by origin.
    """

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        runtime: SandboxRuntime | None = None,
    ) -> None:
        self.session = session
        self.settings = settings
        self.runtime = runtime or get_sandbox_runtime()

    async def start_or_reuse(
        self,
        workspace_id: UUID,
        user_id: UUID,
        worktree_id: UUID | None = None,
    ) -> EditorSession:
        workspace = await self._get_workspace(workspace_id, user_id)
        if not workspace.sandbox_id:
            raise EditorWorkspacePathError
        folder = await self._resolve_folder(workspace, worktree_id)
        existing = await self._latest_for_workspace(workspace.id, user_id)
        # Docker sandboxes share one bridge network, so code-server (`--auth none`) listens on
        # loopback there and only the proxy-only port relay reaches it (Epic 04). Daytona's
        # preview edge is per-sandbox: unchanged.
        loopback_only = workspace.runtime == "docker"
        try:
            result = await self.runtime.exec(
                workspace,
                self._ensure_command(folder, loopback_only=loopback_only),
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
                    "Install it in the sandbox image or enable editor auto-install."
                )
            if loopback_only:
                await self.runtime.expose_port(workspace, self.settings.editor_port)
        except SandboxRuntimeError as error:
            raise EditorRuntimeError(str(error)) from error

        alias = await ensure_editor_alias(self.session, workspace)
        editor_url = editor_alias_url(
            alias,
            base_url=self.settings.preview_proxy_base_url,
            folder=folder,
        )
        now = datetime.now(UTC)
        if existing and existing.status == "active":
            existing.url = editor_url
            existing.folder = folder
            existing.worktree_id = worktree_id
            existing.last_active_at = now
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
            bind_host=workspace.runtime,
            bind_port=self.settings.editor_port,
            worktree_id=worktree_id,
            folder=folder,
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
            metadata={"port": self.settings.editor_port, "folder": folder},
        )
        return editor_session

    async def status(self, workspace_id: UUID, user_id: UUID) -> EditorSession | None:
        # No URL renewal: the alias host never expires, and the per-browser grant is minted
        # separately by `prime_url` on every response.
        workspace = await self._get_workspace(workspace_id, user_id)
        return await self._latest_for_workspace(workspace.id, user_id)

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
        except SandboxRuntimeError:
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

    async def prime_url(self, editor_session: EditorSession) -> str:
        """The credential-bearing URL the iframe loads first.

        The grant is the same token family the proxy stores as a cookie (domain-separated from
        the platform session token by `preview_alias`), minted fresh per response with a short
        TTL and never persisted -- so it is only ever in flight, and only long enough for the
        browser to exchange it for the host-scoped cookie.
        """
        workspace = await self.session.get(Workspace, editor_session.workspace_id)
        if workspace is None or not workspace.editor_alias:
            raise EditorSessionNotFoundError
        grant = encode_alias_grant(
            alias=workspace.editor_alias,
            workspace_id=workspace.id,
            secret_key=self.settings.auth_secret_key,
            ttl_seconds=self.settings.editor_prime_ttl_seconds,
        )
        return editor_prime_url(
            workspace.editor_alias,
            base_url=self.settings.preview_proxy_base_url,
            grant=grant,
            capability_query_param=self.settings.preview_alias_capability_query_param,
            folder=editor_session.folder,
        )

    async def _resolve_folder(self, workspace: Workspace, worktree_id: UUID | None) -> str:
        try:
            path = await WorktreeService(
                self.session, self.runtime, self.settings
            ).resolve_path(workspace, worktree_id)
        except WorktreeNotFoundError as error:
            raise EditorWorktreeError("Worktree not found") from error
        return self._absolute_workspace_path(path)

    def _ensure_command(self, folder: str, *, loopback_only: bool = False) -> str:
        binary = self.settings.editor_code_server_binary
        port = self.settings.editor_port
        quoted_folder = quote(folder, safe="/")
        run_log = "/tmp/execute-code-server.log"
        bind = "127.0.0.1" if loopback_only else "0.0.0.0"
        listening = f"grep -qi ':{port:04X} [0-9A-F:]* 0A' /proc/net/tcp /proc/net/tcp6 2>/dev/null"
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
                # a URL that points at a port nothing is listening on.
                f"command -v {binary} >/dev/null 2>&1 || exit 127",
                # Already serving? Ask the kernel's listen table, not `pgrep -f`: a process
                # pattern also matches the shell running this script, whose argv holds the
                # launch line below (local-docker-runtime Epic 04).
                (
                    f"{listening} || "
                    f"nohup {binary} --auth none --bind-addr {bind}:{port} "
                    f"{quoted_folder} >{run_log} 2>&1 &"
                ),
            ]
        )
        if loopback_only:
            # The relay can only take over a port that is already listening.
            lines.append(
                f"for i in $(seq 1 60); do {listening} && exit 0; sleep 0.5; done; exit 1"
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

    def _absolute_workspace_path(self, workspace_path: str) -> str:
        # code-server resolves the `folder=` query param as an absolute path inside the
        # sandbox, so a relative workspace path (e.g. "workspace") must be anchored to the
        # sandbox home (e.g. "/home/daytona/workspace").
        if workspace_path.startswith("/"):
            return workspace_path
        home = self.settings.daytona_sandbox_home.rstrip("/")
        return f"{home}/{workspace_path}"
