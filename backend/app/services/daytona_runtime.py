import asyncio
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from uuid import UUID

from daytona import (
    AsyncDaytona,
    CreateSandboxFromSnapshotParams,
    DaytonaConfig,
    DaytonaConnectionError,
    DaytonaError,
    DaytonaNotFoundError,
    DaytonaTimeoutError,
    ListSandboxesQuery,
    PtySize,
    SessionExecuteRequest,
)

from app.core.config import Settings, get_settings
from app.models import Workspace


class DaytonaRuntimeError(Exception):
    pass


class DaytonaUnavailableError(DaytonaRuntimeError):
    pass


class DaytonaSandboxNotFoundError(DaytonaRuntimeError):
    pass


class DaytonaProvisioningError(DaytonaRuntimeError):
    pass


class DaytonaCommandTimeoutError(DaytonaRuntimeError):
    pass


@dataclass(frozen=True)
class RuntimeCommandResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class RuntimePreviewLink:
    url: str
    token: str


# Local to avoid a circular import with `app.services.workspace_env` (which itself depends on
# this module for the `DaytonaRuntime` type). Env var keys are already validated on write; this
# is defense in depth against building a shell string with a malformed key.
_ENV_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def map_daytona_error(error: Exception) -> DaytonaRuntimeError:
    if isinstance(error, DaytonaNotFoundError):
        return DaytonaSandboxNotFoundError("Daytona sandbox was not found")
    if isinstance(error, (DaytonaConnectionError, DaytonaTimeoutError)):
        return DaytonaUnavailableError("Daytona is unavailable")
    if isinstance(error, DaytonaError):
        return DaytonaRuntimeError(str(error))
    return DaytonaRuntimeError(str(error))


class DaytonaRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: AsyncDaytona | None = None

    @property
    def configured(self) -> bool:
        return bool(self.settings.daytona_api_key)

    def _get_client(self) -> AsyncDaytona:
        if not self.configured:
            raise DaytonaUnavailableError("DAYTONA_API_KEY is not configured")
        if self._client is None:
            self._client = AsyncDaytona(
                DaytonaConfig(
                    api_key=self.settings.daytona_api_key,
                    api_url=self.settings.daytona_api_url,
                    target=self.settings.daytona_target,
                )
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def health(self) -> str:
        try:
            client = self._get_client()
            async for _sandbox in client.list(ListSandboxesQuery(limit=1)):
                break
        except Exception as error:
            raise map_daytona_error(error) from error
        return "ok"

    async def provision(
        self,
        workspace_id: UUID,
        owner_id: UUID,
        snapshot: str | None,
    ) -> Any:
        client = self._get_client()
        name = f"execute-{workspace_id.hex}"
        try:
            async for existing in client.list(ListSandboxesQuery(name=name, limit=1)):
                if existing.name == name:
                    return await self._ensure_started(existing)
            block_all, allow_list = self._desired_network_settings()
            params = CreateSandboxFromSnapshotParams(
                name=name,
                snapshot=snapshot or self.settings.daytona_default_snapshot,
                language=self.settings.daytona_default_language,
                labels={
                    "execute-workspace-id": str(workspace_id),
                    "execute-owner-id": str(owner_id),
                },
                public=False,
                auto_stop_interval=self.settings.daytona_auto_stop_minutes,
                auto_archive_interval=0,
                auto_delete_interval=-1,
                ephemeral=False,
                network_block_all=block_all,
                network_allow_list=allow_list,
            )
            sandbox = await client.create(
                params,
                timeout=self.settings.daytona_provision_timeout_seconds,
            )
            try:
                await sandbox.fs.get_file_info(self.settings.daytona_workspace_path)
            except Exception:
                await sandbox.fs.create_folder(self.settings.daytona_workspace_path, "755")
            return sandbox
        except Exception as error:
            mapped = map_daytona_error(error)
            raise DaytonaProvisioningError(str(mapped)) from error

    async def get_sandbox(self, workspace: Workspace, *, start: bool = True) -> Any:
        if not workspace.daytona_sandbox_id:
            raise DaytonaSandboxNotFoundError("Workspace has no Daytona sandbox")
        try:
            sandbox = await self._get_client().get(workspace.daytona_sandbox_id)
            if start:
                sandbox = await self._ensure_started(sandbox)
            return sandbox
        except Exception as error:
            raise map_daytona_error(error) from error

    async def status(self, workspace: Workspace) -> str:
        sandbox = await self.get_sandbox(workspace, start=False)
        await sandbox.refresh_data()
        state = str(sandbox.state or "").lower()
        if state == "started":
            return "ready"
        if state in {"stopped", "archived"}:
            return "stopped"
        if state in {"starting", "creating", "restoring", "building"}:
            return "creating"
        return "error"

    async def delete(self, workspace: Workspace) -> None:
        if not workspace.daytona_sandbox_id:
            return
        try:
            sandbox = await self._get_client().get(workspace.daytona_sandbox_id)
            await sandbox.delete(timeout=self.settings.daytona_provision_timeout_seconds)
        except DaytonaNotFoundError:
            return
        except Exception as error:
            raise map_daytona_error(error) from error

    async def exec(
        self,
        workspace: Workspace,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> RuntimeCommandResult:
        sandbox = await self.get_sandbox(workspace)
        try:
            response = await sandbox.process.exec(
                command,
                cwd=cwd or workspace.workspace_path,
                env=env,
                timeout=timeout,
            )
        except DaytonaTimeoutError as error:
            raise DaytonaCommandTimeoutError("Command timed out") from error
        except Exception as error:
            raise map_daytona_error(error) from error
        return RuntimeCommandResult(
            exit_code=response.exit_code if response.exit_code is not None else 1,
            stdout=response.result or "",
            stderr="",
        )

    async def stream_command(
        self,
        workspace: Workspace,
        session_id: str,
        command: str,
        *,
        cwd: str | None,
        env: dict[str, str] | None,
        timeout: int | None,
        on_stdout: Callable[[str], Awaitable[None]],
        on_stderr: Callable[[str], Awaitable[None]],
    ) -> int:
        sandbox = await self.get_sandbox(workspace)
        process = sandbox.process
        try:
            await process.create_session(session_id)
        except Exception:
            pass
        working_directory = cwd or workspace.workspace_path
        for key in env or {}:
            if not _ENV_KEY_PATTERN.fullmatch(key):
                raise ValueError(f"invalid environment variable name: {key!r}")
        env_prefix = " ".join(
            f"{key}={shlex.quote(value)}" for key, value in sorted((env or {}).items())
        )
        shell_command = f"cd {shlex.quote(working_directory)} && "
        if env_prefix:
            shell_command += f"env {env_prefix} "
        shell_command += command
        request = SessionExecuteRequest(command=shell_command, run_async=True)
        try:
            response = await process.execute_session_command(session_id, request)
            if not response.cmd_id:
                return response.exit_code if response.exit_code is not None else 1
            await asyncio.wait_for(
                process.get_session_command_logs_async(
                    session_id,
                    response.cmd_id,
                    on_stdout,
                    on_stderr,
                ),
                timeout=timeout,
            )
            command_result = await process.get_session_command(session_id, response.cmd_id)
            return command_result.exit_code if command_result.exit_code is not None else 1
        except TimeoutError as error:
            raise DaytonaCommandTimeoutError("Command timed out") from error
        except Exception as error:
            raise map_daytona_error(error) from error
        finally:
            try:
                await process.delete_session(session_id)
            except Exception:
                pass

    async def create_or_connect_pty(
        self,
        workspace: Workspace,
        session_id: str,
        on_data: Callable[[bytes], Awaitable[None]],
        *,
        env: dict[str, str] | None,
        cols: int,
        rows: int,
        cwd: str | None = None,
    ) -> Any:
        sandbox = await self.get_sandbox(workspace)
        try:
            sessions = await sandbox.process.list_pty_sessions()
            if any(item.id == session_id for item in sessions):
                return await sandbox.process.connect_pty_session(session_id, on_data)
            return await sandbox.process.create_pty_session(
                session_id,
                on_data,
                cwd=cwd or workspace.workspace_path,
                envs=env,
                pty_size=PtySize(cols=cols, rows=rows),
            )
        except Exception as error:
            raise map_daytona_error(error) from error

    async def kill_pty(self, workspace: Workspace, session_id: str) -> None:
        sandbox = await self.get_sandbox(workspace)
        try:
            await sandbox.process.kill_pty_session(session_id)
        except DaytonaNotFoundError:
            return
        except Exception as error:
            raise map_daytona_error(error) from error

    async def signed_preview_url(self, workspace: Workspace, port: int) -> str:
        sandbox = await self.get_sandbox(workspace)
        try:
            result = await sandbox.create_signed_preview_url(
                port,
                expires_in_seconds=self.settings.daytona_preview_url_ttl_seconds,
            )
            return result.url
        except Exception as error:
            raise map_daytona_error(error) from error

    async def preview_link(self, workspace: Workspace, port: int) -> RuntimePreviewLink:
        sandbox = await self.get_sandbox(workspace)
        try:
            result = await sandbox.get_preview_link(port)
            return RuntimePreviewLink(url=result.url, token=result.token)
        except Exception as error:
            raise map_daytona_error(error) from error

    async def _ensure_started(self, sandbox: Any) -> Any:
        await sandbox.refresh_data()
        if str(sandbox.state or "").lower() != "started":
            await sandbox.start(timeout=self.settings.daytona_provision_timeout_seconds)
        await self._apply_network_settings(sandbox)
        return sandbox

    def _desired_network_settings(self) -> tuple[bool, str | None]:
        """The egress policy every sandbox should carry, derived from settings.

        Returns ``(block_all, allow_list)`` where ``allow_list`` is the SDK's
        comma-separated CIDR string (``None`` when unrestricted). Defaults open
        (``block_all=False``, no allow list) so sandboxes can reach external
        services; set ``DAYTONA_NETWORK_BLOCK_ALL`` / ``DAYTONA_NETWORK_ALLOW_LIST``
        to isolate or narrow.
        """
        return (
            self.settings.daytona_network_block_all,
            ",".join(self.settings.daytona_network_allow_list) or None,
        )

    async def _apply_network_settings(self, sandbox: Any) -> None:
        """Reconcile a reused/restarted sandbox to the desired egress policy.

        New sandboxes get the policy via create params; sandboxes returned from
        the ``client.list`` early-return in ``provision`` and from ``get_sandbox``
        bypass that, so apply it here. Skip the API call when already in the
        desired state (``refresh_data`` in ``_ensure_started`` populates the
        current ``network_*`` attributes)."""
        block_all, allow_list = self._desired_network_settings()
        current_block_all = getattr(sandbox, "network_block_all", None)
        current_allow_list = getattr(sandbox, "network_allow_list", None) or None
        if current_block_all == block_all and current_allow_list == allow_list:
            return
        await sandbox.update_network_settings(
            network_block_all=block_all,
            network_allow_list=allow_list,
        )


@lru_cache
def get_daytona_runtime() -> DaytonaRuntime:
    return DaytonaRuntime(get_settings())
