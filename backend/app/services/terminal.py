import asyncio
import json
import os
import shutil
import struct
import termios
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import TerminalSession, Workspace
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
        workspace_root: Path,
        tmux_binary: str,
    ) -> None:
        self.session = session
        self.workspace_root = workspace_root
        self.tmux_binary = tmux_binary

    async def create_or_reuse(
        self,
        workspace_id: UUID,
        user_id: UUID,
        name: str,
    ) -> TerminalSession:
        workspace = await self._get_workspace(workspace_id, user_id)
        workspace_path = self._workspace_path(workspace)
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
        if terminal_session is None:
            terminal_session = TerminalSession(
                workspace_id=workspace.id,
                user_id=user_id,
                name=name,
                tmux_session_name=self._tmux_session_name(workspace.id),
                status="active",
                last_active_at=datetime.now(UTC),
            )
            self.session.add(terminal_session)
            await self.session.flush()
            logger.info(
                "terminal_session_start",
                terminal_session_id=str(terminal_session.id),
                workspace_id=str(workspace.id),
                user_id=str(user_id),
            )
        else:
            terminal_session.name = name
            terminal_session.last_active_at = datetime.now(UTC)
            self.session.add(terminal_session)

        await self._ensure_tmux_session(terminal_session, workspace_path)
        await self.session.commit()
        await self.session.refresh(terminal_session)
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
        _workspace, terminal_session = await self.get_for_workspace(
            workspace_id,
            terminal_session_id,
            user_id,
        )
        await self._kill_tmux_session(terminal_session.tmux_session_name)
        terminal_session.status = "stopped"
        terminal_session.last_active_at = datetime.now(UTC)
        self.session.add(terminal_session)
        await self.session.commit()
        logger.info(
            "terminal_session_stop",
            terminal_session_id=str(terminal_session.id),
            workspace_id=str(workspace_id),
            user_id=str(user_id),
        )

    async def touch(self, terminal_session: TerminalSession) -> None:
        terminal_session.last_active_at = datetime.now(UTC)
        self.session.add(terminal_session)
        await self.session.commit()

    async def _get_workspace(self, workspace_id: UUID, user_id: UUID) -> Workspace:
        workspace = await self.session.get(Workspace, workspace_id)
        if workspace is None or workspace.owner_id != user_id:
            raise WorkspaceNotFoundError
        return workspace

    def _workspace_path(self, workspace: Workspace) -> Path:
        if workspace.local_path is None:
            raise TerminalWorkspacePathError
        root = self.workspace_root.resolve()
        path = Path(workspace.local_path).resolve()
        if path == root or root not in path.parents or not path.is_dir():
            raise TerminalWorkspacePathError
        return path

    def _tmux_session_name(self, workspace_id: UUID) -> str:
        return f"execute-{workspace_id.hex}"

    async def _ensure_tmux_session(
        self,
        terminal_session: TerminalSession,
        workspace_path: Path,
    ) -> None:
        if await self._tmux_session_exists(terminal_session.tmux_session_name):
            await self._configure_tmux_session(terminal_session.tmux_session_name)
            return
        await self._run_tmux(
            "new-session",
            "-d",
            "-s",
            terminal_session.tmux_session_name,
            "-c",
            str(workspace_path),
        )
        await self._configure_tmux_session(terminal_session.tmux_session_name)

    async def _configure_tmux_session(self, tmux_session_name: str) -> None:
        await self._run_tmux("set-option", "-t", tmux_session_name, "mouse", "on")
        await self._run_tmux("set-option", "-t", tmux_session_name, "history-limit", "10000")

    async def _tmux_session_exists(self, tmux_session_name: str) -> bool:
        try:
            await self._run_tmux("has-session", "-t", tmux_session_name)
        except TerminalRuntimeError as error:
            message = str(error).lower()
            if (
                "can't find session" in message
                or "no server running" in message
                or "no such file or directory" in message
            ):
                return False
            raise
        return True

    async def _kill_tmux_session(self, tmux_session_name: str) -> None:
        try:
            await self._run_tmux("kill-session", "-t", tmux_session_name)
        except TerminalRuntimeError:
            return

    async def _run_tmux(self, *args: str) -> None:
        if shutil.which(self.tmux_binary) is None:
            raise TerminalRuntimeError("tmux is not installed")
        process = await asyncio.create_subprocess_exec(
            self.tmux_binary,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "TERM": os.environ.get("TERM") or "xterm-256color"},
        )
        _stdout, stderr = await process.communicate()
        if process.returncode != 0:
            message = stderr.decode("utf-8", errors="replace").strip() or "tmux command failed"
            raise TerminalRuntimeError(message)


class TerminalPtyBridge:
    def __init__(
        self,
        tmux_binary: str,
        default_cols: int,
        default_rows: int,
    ) -> None:
        self.tmux_binary = tmux_binary
        self.default_cols = default_cols
        self.default_rows = default_rows

    async def bridge(self, websocket, terminal_session: TerminalSession) -> None:
        if shutil.which(self.tmux_binary) is None:
            await websocket.send_json({"type": "error", "message": "tmux is not installed"})
            await websocket.close()
            return

        master_fd, slave_fd = os.openpty()
        process = None
        reader_task = None
        try:
            self._resize(master_fd, self.default_cols, self.default_rows)
            os.set_blocking(master_fd, False)
            process = await asyncio.create_subprocess_exec(
                self.tmux_binary,
                "attach-session",
                "-t",
                terminal_session.tmux_session_name,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env={**os.environ, "TERM": "xterm-256color"},
                start_new_session=True,
            )
            os.close(slave_fd)
            slave_fd = -1

            logger.info(
                "terminal_session_connect",
                terminal_session_id=str(terminal_session.id),
                workspace_id=str(terminal_session.workspace_id),
                user_id=str(terminal_session.user_id),
            )

            reader_task = asyncio.create_task(self._stream_pty_output(websocket, master_fd))
            await self._receive_browser_input(websocket, master_fd)
        finally:
            if reader_task:
                reader_task.cancel()
            if process and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            for fd in (master_fd, slave_fd):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            logger.info(
                "terminal_session_disconnect",
                terminal_session_id=str(terminal_session.id),
                workspace_id=str(terminal_session.workspace_id),
                user_id=str(terminal_session.user_id),
            )

    async def _stream_pty_output(self, websocket, master_fd: int) -> None:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[bytes | None] = asyncio.Queue()

        def read_ready() -> None:
            try:
                chunk = os.read(master_fd, 8192)
            except BlockingIOError:
                return
            except OSError:
                loop.remove_reader(master_fd)
                queue.put_nowait(None)
                return
            if chunk:
                queue.put_nowait(chunk)
            else:
                loop.remove_reader(master_fd)
                queue.put_nowait(None)

        loop.add_reader(master_fd, read_ready)
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    return
                await websocket.send_bytes(chunk)
        finally:
            loop.remove_reader(master_fd)

    async def _receive_browser_input(self, websocket, master_fd: int) -> None:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            if data := message.get("bytes"):
                os.write(master_fd, data)
                continue
            if text := message.get("text"):
                self._handle_text_message(master_fd, text)

    def _handle_text_message(self, master_fd: int, text: str) -> None:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return
        if payload.get("type") != "resize":
            return
        try:
            cols = int(payload["cols"])
            rows = int(payload["rows"])
        except (KeyError, TypeError, ValueError):
            return
        self._resize(master_fd, cols, rows)

    def _resize(self, fd: int, cols: int, rows: int) -> None:
        safe_cols = max(20, min(cols, 400))
        safe_rows = max(5, min(rows, 200))
        packed = struct.pack("HHHH", safe_rows, safe_cols, 0, 0)
        termios.tcsetwinsize(fd, (safe_rows, safe_cols))
        # Some Python builds lack a complete tcsetwinsize implementation for PTYs.
        if not hasattr(termios, "TIOCSWINSZ"):
            return
        import fcntl

        fcntl.ioctl(fd, termios.TIOCSWINSZ, packed)
