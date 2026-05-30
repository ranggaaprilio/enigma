import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse
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
        public_url: str,
        container_workspace_root: Path,
        token_ttl_seconds: int,
        secret_key: str,
    ) -> None:
        self.session = session
        self.workspace_root = workspace_root
        self.public_url = public_url.rstrip("/")
        self.container_workspace_root = container_workspace_root
        self.token_ttl_seconds = token_ttl_seconds
        self.secret_key = secret_key

    async def start_or_reuse(self, workspace_id: UUID, user_id: UUID) -> EditorSession:
        workspace = await self._get_workspace(workspace_id, user_id)
        workspace_path = self._workspace_path(workspace)
        editor_url = self.editor_url(workspace_path)
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
        if editor_session is not None:
            editor_session.url = editor_url
            editor_session.last_active_at = datetime.now(UTC)
            self.session.add(editor_session)
            await self.session.commit()
            await self.session.refresh(editor_session)
            return editor_session

        public_url = urlparse(self.public_url)
        editor_session = EditorSession(
            id=uuid4(),
            workspace_id=workspace.id,
            user_id=user_id,
            url=editor_url,
            status="active",
            process_id=None,
            bind_host=public_url.hostname or "code-server",
            bind_port=public_url.port or 8080,
            token_hash="",
            token_expires_at=datetime.now(UTC) + timedelta(seconds=self.token_ttl_seconds),
            started_at=datetime.now(UTC),
            last_active_at=datetime.now(UTC),
        )
        self.session.add(editor_session)
        await self.session.commit()
        await self.session.refresh(editor_session)
        logger.info(
            "editor_session_start",
            editor_session_id=str(editor_session.id),
            workspace_id=str(workspace.id),
            user_id=str(user_id),
            url=editor_session.url,
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
        return editor_session

    async def stop(self, workspace_id: UUID, user_id: UUID) -> EditorSession:
        workspace = await self._get_workspace(workspace_id, user_id)
        editor_session = await self._latest_for_workspace(workspace.id, user_id)
        if editor_session is None:
            raise EditorSessionNotFoundError
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
        if editor_session.status != "active":
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

    def editor_url(self, workspace_path: Path) -> str:
        relative_path = workspace_path.relative_to(self.workspace_root.resolve())
        container_path = self.container_workspace_root / relative_path
        return f"{self.public_url}/?folder={quote(str(container_path), safe='/')}"

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

    def _session_is_usable(self, editor_session: EditorSession) -> bool:
        return editor_session.status == "active"

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
