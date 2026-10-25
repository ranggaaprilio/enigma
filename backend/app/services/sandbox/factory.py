"""Select the sandbox provider for a workspace from ``Workspace.runtime``.

Providers are per-process singletons (their SDK clients hold connection pools), created
on first use and closed together by ``close_all()`` from the API, worker and preview-proxy
lifespans. Services are constructed before they know which workspace they will serve
(FastAPI dependencies, the worker context), so they hold the ``SandboxRuntimeDispatcher``
from ``get_sandbox_runtime()`` and every call routes by the workspace it is given.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from functools import lru_cache

import structlog

from app.core.config import Settings, get_settings
from app.models import Workspace
from app.services.sandbox.base import (
    PtyHandle,
    RuntimeCommandResult,
    RuntimeFileSystem,
    RuntimePreviewLink,
    RuntimeUnavailableError,
    SandboxRuntime,
)
from app.services.sandbox.daytona import DaytonaRuntime
from app.services.sandbox.docker import DockerRuntime

logger = structlog.get_logger(__name__)

DEFAULT_PROVIDER = "daytona"

_FACTORIES: dict[str, Callable[[Settings], SandboxRuntime]] = {
    "daytona": DaytonaRuntime,
    "docker": DockerRuntime,
}
_instances: dict[str, SandboxRuntime] = {}


def get_runtime_by_name(name: str) -> SandboxRuntime:
    runtime = _instances.get(name)
    if runtime is None:
        factory = _FACTORIES.get(name)
        if factory is None:
            raise RuntimeUnavailableError(f"Unknown sandbox runtime {name!r}")
        runtime = _instances[name] = factory(get_settings())
    return runtime


def get_runtime(workspace: Workspace) -> SandboxRuntime:
    return get_runtime_by_name(workspace.runtime or DEFAULT_PROVIDER)


async def close_all() -> None:
    for name, runtime in list(_instances.items()):
        try:
            await runtime.close()
        except Exception:
            logger.exception("sandbox_runtime_close_failed", provider=name)
    _instances.clear()


class SandboxRuntimeDispatcher:
    """A ``SandboxRuntime`` that forwards each call to the workspace's provider.

    Workspace-less members (``configured``, ``health``, ``settings``) answer for the
    default provider, which is what the ``/health`` probes report on.
    """

    provider = "dispatch"

    def __init__(self, default: str = DEFAULT_PROVIDER) -> None:
        self._default = default

    @property
    def settings(self) -> Settings:
        return get_settings()

    @property
    def configured(self) -> bool:
        return get_runtime_by_name(self._default).configured

    async def health(self) -> str:
        return await get_runtime_by_name(self._default).health()

    async def provision(self, workspace: Workspace) -> str:
        return await get_runtime(workspace).provision(workspace)

    async def status(self, workspace: Workspace) -> str:
        return await get_runtime(workspace).status(workspace)

    async def start(self, workspace: Workspace) -> None:
        await get_runtime(workspace).start(workspace)

    async def delete(self, workspace: Workspace) -> None:
        await get_runtime(workspace).delete(workspace)

    async def exec(
        self,
        workspace: Workspace,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> RuntimeCommandResult:
        return await get_runtime(workspace).exec(
            workspace, command, cwd=cwd, env=env, timeout=timeout
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
        return await get_runtime(workspace).stream_command(
            workspace,
            session_id,
            command,
            cwd=cwd,
            env=env,
            timeout=timeout,
            on_stdout=on_stdout,
            on_stderr=on_stderr,
        )

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
    ) -> PtyHandle:
        return await get_runtime(workspace).create_or_connect_pty(
            workspace, session_id, on_data, env=env, cols=cols, rows=rows, cwd=cwd
        )

    async def kill_pty(self, workspace: Workspace, session_id: str) -> None:
        await get_runtime(workspace).kill_pty(workspace, session_id)

    async def preview_link(self, workspace: Workspace, port: int) -> RuntimePreviewLink:
        return await get_runtime(workspace).preview_link(workspace, port)

    async def fs(self, workspace: Workspace) -> RuntimeFileSystem:
        return await get_runtime(workspace).fs(workspace)

    async def close(self) -> None:
        await close_all()


@lru_cache
def get_sandbox_runtime() -> SandboxRuntime:
    return SandboxRuntimeDispatcher()
