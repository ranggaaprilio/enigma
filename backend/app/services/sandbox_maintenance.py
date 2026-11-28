"""Sandbox upkeep for OpenSandbox (Plan 7a Task 5).

Daytona enforces auto-stop and cleans up after itself; an OpenSandbox server keeps sandboxes
until they are deleted and sweeps nothing, so this module does both for OpenSandbox
workspaces: idle sandboxes are paused (the next use resumes them, files kept), and this
installation's sandboxes whose workspace row is gone are destroyed. (The Docker provider's
upkeep lived here too until it was removed in Plan 8.)

"Idle" is the ``sandbox_activity`` row (or, before first use, the workspace's ``updated_at``)
older than ``SANDBOX_AUTO_STOP_MINUTES``. Concurrent passes (API and workers, ``--reload``) are
serialised with a transaction-scoped advisory lock -- never a session lock, which PgBouncer
would break (AGENTS.md gotcha 22).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.metrics import sandbox_maintenance_actions_total
from app.models import SandboxActivity, Workspace
from app.services.sandbox import SandboxRuntimeError

logger = structlog.get_logger(__name__)


async def _try_lock(session: AsyncSession, name: str) -> bool:
    return bool(await session.scalar(select(func.pg_try_advisory_xact_lock(func.hashtext(name)))))


def _count(action: str, amount: int, provider: str) -> None:
    if amount:
        sandbox_maintenance_actions_total.labels(provider=provider, action=action).inc(amount)


def _idle(minutes: int):
    last_used = func.coalesce(SandboxActivity.last_activity_at, Workspace.updated_at)
    return last_used < func.clock_timestamp() - timedelta(minutes=minutes)


def _idle_workspaces(minutes: int, provider: str):
    return (
        select(Workspace)
        .outerjoin(SandboxActivity, SandboxActivity.workspace_id == Workspace.id)
        .where(
            Workspace.runtime == provider,
            Workspace.sandbox_id.is_not(None),
            Workspace.status != "error",
            _idle(minutes),
        )
    )


def _workspace_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


# --- OpenSandbox (Plan 7a Task 5) ------------------------------------------------------------
# The server keeps sandboxes until deleted (Enigma creates them without a TTL) and has no
# orphan sweeper, so this does both: idle sandboxes are paused (the next use resumes them,
# files kept) and this installation's sandboxes whose workspace row is gone are destroyed.
# "Busy" is the activity heartbeat the runtime keeps while it runs something in a sandbox.
OPENSANDBOX = "opensandbox"


async def pause_idle_opensandboxes(
    session_maker: async_sessionmaker[AsyncSession],
    settings: Settings,
    runtime,
    *,
    only: set[UUID] | None = None,
) -> int:
    minutes = settings.sandbox_auto_stop_minutes
    if minutes <= 0:
        return 0
    async with session_maker() as session, session.begin():
        if not await _try_lock(session, "opensandbox_idle_reaper"):
            return 0
        query = _idle_workspaces(minutes, OPENSANDBOX)
        if only is not None:
            query = query.where(Workspace.id.in_(only))
        candidates = list((await session.scalars(query)).all())
    paused = 0
    for workspace in candidates:
        async with session_maker() as session:  # re-check: it may have been used meanwhile
            recheck = _idle_workspaces(minutes, OPENSANDBOX).where(Workspace.id == workspace.id)
            still_idle = await session.scalar(select(func.count()).select_from(recheck.subquery()))
        if not still_idle:
            continue
        try:
            if await runtime.pause_if_running(workspace):
                paused += 1
                logger.info("sandbox_idle_paused", workspace_id=str(workspace.id))
        except SandboxRuntimeError as error:
            logger.warning(
                "sandbox_idle_pause_failed",
                workspace_id=str(workspace.id),
                error_type=type(error).__name__,
            )
    _count("idle_paused", paused, OPENSANDBOX)
    return paused


async def reconcile_opensandbox_orphans(
    session_maker: async_sessionmaker[AsyncSession],
    runtime,
    *,
    only: set[UUID] | None = None,
) -> int:
    """Destroy this installation's sandboxes whose workspace row is gone. A sandbox without
    a workspace label (or one created before labels) is never touched: never guess."""
    async with session_maker() as session, session.begin():
        if not await _try_lock(session, "opensandbox_orphan_reconcile"):
            return 0
        managed = await runtime.managed_sandboxes()
        labelled = [(sid, _workspace_uuid(label)) for sid, label in managed if label]
        labelled = [(sid, ws) for sid, ws in labelled if ws is not None]
        if only is not None:
            labelled = [(sid, ws) for sid, ws in labelled if ws in only]
        existing = set(
            await session.scalars(
                select(Workspace.id).where(Workspace.id.in_({ws for _, ws in labelled}))
            )
        )
        removed = 0
        for sandbox_id, workspace_id in labelled:
            if workspace_id in existing:
                continue
            await runtime.remove_sandbox(sandbox_id)
            removed += 1
            logger.info("sandbox_orphan_removed", workspace_id=str(workspace_id))
    _count("orphan_removed", removed, OPENSANDBOX)
    return removed


async def run_opensandbox_maintenance(
    session_maker: async_sessionmaker[AsyncSession], settings: Settings, runtime
) -> None:
    while True:
        try:
            await reconcile_opensandbox_orphans(session_maker, runtime)
            await pause_idle_opensandboxes(session_maker, settings, runtime)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("opensandbox_maintenance_pass_failed")
        await asyncio.sleep(settings.sandbox_upkeep_interval_seconds)


def start_opensandbox_maintenance(
    session_maker: async_sessionmaker[AsyncSession], settings: Settings
) -> asyncio.Task | None:
    """Start OpenSandbox upkeep where it is wanted: ``OPENSANDBOX_UPKEEP`` (unset: when
    ``SANDBOX_RUNTIME_DEFAULT=opensandbox``) and the server is configured. The orphan sweep
    destroys sandboxes, so a process that never asked for it must not run it."""
    from app.services.sandbox.factory import get_runtime_by_name
    from app.services.sandbox.opensandbox import OpenSandboxRuntime

    if not settings.opensandbox_upkeep_enabled:
        return None
    runtime = get_runtime_by_name(OPENSANDBOX)
    if not isinstance(runtime, OpenSandboxRuntime) or not runtime.configured:
        return None
    return asyncio.create_task(run_opensandbox_maintenance(session_maker, settings, runtime))
