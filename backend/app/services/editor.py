import asyncio
import base64
import hashlib
import hmac
import json
import os
import shutil
import signal
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EditorSession, Workspace
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


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class EditorService:
    def __init__(
        self,
        session: AsyncSession,
        workspace_root: Path,
        runtime_root: Path,
        code_server_binary: str,
        bind_host: str,
        port_start: int,
        port_end: int,
        token_ttl_seconds: int,
        secret_key: str,
    ) -> None:
        self.session = session
        self.workspace_root = workspace_root
        self.runtime_root = runtime_root
        self.code_server_binary = code_server_binary
        self.bind_host = bind_host
        self.port_start = port_start
        self.port_end = port_end
        self.token_ttl_seconds = token_ttl_seconds
        self.secret_key = secret_key

    async def start_or_reuse(self, workspace_id: UUID, user_id: UUID) -> EditorSession:
        workspace = await self._get_workspace(workspace_id, user_id)
        workspace_path = self._workspace_path(workspace)
        result = await self.session.execute(
            select(EditorSession)
            .where(
                EditorSession.workspace_id == workspace.id,
                EditorSession.user_id == user_id,
                EditorSession.status == "active",
            )
            .order_by(EditorSession.last_active_at.desc())
            .limit(1)
        )
        editor_session = result.scalar_one_or_none()
        if editor_session is not None and self._session_is_usable(editor_session):
            editor_session.last_active_at = datetime.now(UTC)
            self.session.add(editor_session)
            await self.session.commit()
            await self.session.refresh(editor_session)
            return editor_session

        if editor_session is not None:
            await self._mark_stopped(editor_session)

        editor_session = EditorSession(
            id=uuid4(),
            workspace_id=workspace.id,
            user_id=user_id,
            url="",
            status="active",
            process_id=None,
            bind_host=self.bind_host,
            bind_port=await self._allocate_port(),
            token_hash="",
            token_expires_at=datetime.now(UTC) + timedelta(seconds=self.token_ttl_seconds),
            started_at=datetime.now(UTC),
            last_active_at=datetime.now(UTC),
        )
        token = self._session_token(editor_session)
        editor_session.token_hash = self._token_hash(token)
        editor_session.url = self.proxy_url(editor_session, token)
        self.session.add(editor_session)
        await self.session.flush()

        process = await self._start_code_server(editor_session, workspace_path, token)
        editor_session.process_id = process.pid
        await self._wait_until_ready(editor_session, process)
        self.session.add(editor_session)
        await self.session.commit()
        await self.session.refresh(editor_session)
        logger.info(
            "editor_session_start",
            editor_session_id=str(editor_session.id),
            workspace_id=str(workspace.id),
            user_id=str(user_id),
            bind_port=editor_session.bind_port,
        )
        return editor_session

    async def status(self, workspace_id: UUID, user_id: UUID) -> EditorSession | None:
        workspace = await self._get_workspace(workspace_id, user_id)
        result = await self.session.execute(
            select(EditorSession)
            .where(EditorSession.workspace_id == workspace.id, EditorSession.user_id == user_id)
            .order_by(EditorSession.last_active_at.desc())
            .limit(1)
        )
        editor_session = result.scalar_one_or_none()
        if editor_session is None:
            return None
        if editor_session.status == "active" and not self._session_is_usable(editor_session):
            editor_session.status = "error"
            editor_session.last_active_at = datetime.now(UTC)
            self.session.add(editor_session)
            await self.session.commit()
            await self.session.refresh(editor_session)
        return editor_session

    async def stop(self, workspace_id: UUID, user_id: UUID) -> EditorSession:
        workspace = await self._get_workspace(workspace_id, user_id)
        editor_session = await self._latest_for_workspace(workspace.id, user_id)
        if editor_session is None:
            raise EditorSessionNotFoundError
        await self._stop_process(editor_session)
        await self._mark_stopped(editor_session)
        await self.session.refresh(editor_session)
        logger.info(
            "editor_session_stop",
            editor_session_id=str(editor_session.id),
            workspace_id=str(workspace.id),
            user_id=str(user_id),
        )
        return editor_session

    async def get_proxy_session(
        self,
        workspace_id: UUID,
        session_id: UUID,
        user_id: UUID,
        token: str,
    ) -> EditorSession:
        workspace = await self._get_workspace(workspace_id, user_id)
        editor_session = await self.session.get(EditorSession, session_id)
        if (
            editor_session is None
            or editor_session.workspace_id != workspace.id
            or editor_session.user_id != user_id
        ):
            raise EditorSessionNotFoundError
        if editor_session.status != "active" or not self._session_is_usable(editor_session):
            raise EditorSessionNotFoundError
        if not self.verify_token(editor_session, token):
            raise EditorTokenError
        editor_session.last_active_at = datetime.now(UTC)
        self.session.add(editor_session)
        await self.session.commit()
        await self.session.refresh(editor_session)
        return editor_session

    def verify_token(self, editor_session: EditorSession, token: str) -> bool:
        if editor_session.token_expires_at < datetime.now(UTC):
            return False
        return hmac.compare_digest(self._token_hash(token), editor_session.token_hash)

    def proxy_url(self, editor_session: EditorSession, token: str) -> str:
        return (
            f"/workspaces/{editor_session.workspace_id}/editor/proxy/"
            f"{editor_session.id}/{token}/"
        )

    def upstream_http_url(self, editor_session: EditorSession, token: str, path: str) -> str:
        suffix = f"/{path}" if path else "/"
        return f"http://{editor_session.bind_host}:{editor_session.bind_port}{suffix}"

    def upstream_ws_url(self, editor_session: EditorSession, token: str, path: str) -> str:
        suffix = f"/{path}" if path else "/"
        return f"ws://{editor_session.bind_host}:{editor_session.bind_port}{suffix}"

    async def _get_workspace(self, workspace_id: UUID, user_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != user_id:
            raise WorkspaceNotFoundError
        return workspace

    def _workspace_path(self, workspace: Workspace) -> Path:
        if workspace.local_path is None:
            raise EditorWorkspacePathError
        root = self.workspace_root.resolve()
        path = Path(workspace.local_path).resolve()
        if path == root or root not in path.parents or not path.is_dir():
            raise EditorWorkspacePathError
        return path

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

    async def _allocate_port(self) -> int:
        used_ports_result = await self.session.execute(
            select(EditorSession.bind_port).where(EditorSession.status == "active")
        )
        used_ports = set(used_ports_result.scalars().all())
        for port in range(self.port_start, self.port_end + 1):
            if port in used_ports:
                continue
            if self._port_is_free(port):
                return port
        raise EditorRuntimeError("No editor ports are available")

    def _port_is_free(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            return sock.connect_ex((self.bind_host, port)) != 0

    async def _start_code_server(
        self,
        editor_session: EditorSession,
        workspace_path: Path,
        token: str,
    ) -> asyncio.subprocess.Process:
        if shutil.which(self.code_server_binary) is None:
            raise EditorRuntimeError("code-server is not installed")

        session_root = self.runtime_root / str(editor_session.id)
        user_data_dir = session_root / "user-data"
        extensions_dir = session_root / "extensions"
        log_path = session_root / "code-server.log"
        user_data_dir.mkdir(parents=True, exist_ok=True)
        extensions_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("ab")
        base_path = (
            f"/workspaces/{editor_session.workspace_id}/editor/proxy/"
            f"{editor_session.id}/{token}"
        )
        try:
            process = await asyncio.create_subprocess_exec(
                self.code_server_binary,
                "--bind-addr",
                f"{editor_session.bind_host}:{editor_session.bind_port}",
                "--auth",
                "none",
                "--disable-telemetry",
                "--disable-update-check",
                f"--user-data-dir={user_data_dir}",
                f"--extensions-dir={extensions_dir}",
                f"--abs-proxy-base-path={base_path}",
                str(workspace_path),
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
            )
        except Exception:
            log_file.close()
            raise
        await asyncio.sleep(0)
        if process.returncode is not None:
            log_file.close()
            raise EditorRuntimeError("code-server failed to start")
        return process

    async def _wait_until_ready(
        self,
        editor_session: EditorSession,
        process: asyncio.subprocess.Process,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + 10
        while asyncio.get_running_loop().time() < deadline:
            if process.returncode is not None:
                raise EditorRuntimeError("code-server exited during startup")
            try:
                reader, writer = await asyncio.open_connection(
                    editor_session.bind_host,
                    editor_session.bind_port,
                )
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(0.1)
        raise EditorRuntimeError("code-server did not become ready")

    def _session_is_usable(self, editor_session: EditorSession) -> bool:
        return (
            editor_session.status == "active"
            and editor_session.token_expires_at >= datetime.now(UTC)
            and self._process_is_alive(editor_session.process_id)
        )

    def _process_is_alive(self, process_id: int | None) -> bool:
        if process_id is None:
            return False
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    async def _stop_process(self, editor_session: EditorSession) -> None:
        if editor_session.process_id is None:
            return
        try:
            os.killpg(editor_session.process_id, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError:
            os.kill(editor_session.process_id, signal.SIGTERM)

    async def _mark_stopped(self, editor_session: EditorSession) -> None:
        editor_session.status = "stopped"
        editor_session.last_active_at = datetime.now(UTC)
        self.session.add(editor_session)
        await self.session.commit()

    def _session_token(self, editor_session: EditorSession) -> str:
        payload = {
            "exp": int(editor_session.token_expires_at.timestamp()),
            "sid": str(editor_session.id),
            "uid": str(editor_session.user_id),
        }
        payload_json = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        payload_segment = _b64_encode(payload_json)
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            payload_segment.encode("ascii"),
            hashlib.sha256,
        ).digest()
        return f"{payload_segment}.{_b64_encode(signature)}"

    def _token_hash(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
