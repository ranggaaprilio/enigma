"""Upkeep for local Docker sandboxes (local-docker-runtime Epic 05).

Daytona enforces auto-stop and cleans up after itself; a local daemon does neither, so this
module does, for Docker workspaces only:

- **Idle auto-stop**: a sandbox whose ``sandbox_activity`` row (or, before its first use,
  the workspace's ``updated_at``) is older than ``SANDBOX_AUTO_STOP_MINUTES`` is stopped and
  kept; the next exec, terminal or preview starts it again.
- **Orphans** (at startup): a managed container or network whose workspace row is gone is
  removed; a workspace whose container vanished is marked ``error``.
- **Proxy attachment**: every pass re-attaches the preview proxy to each sandbox network
  (compose drops those connections when it recreates the proxy).

Only containers and networks labelled ``enigma.managed=1`` are ever touched. Concurrent
passes (API and workers, ``--reload``) are serialised with a transaction-scoped advisory
lock -- never a session lock, which PgBouncer would break (AGENTS.md gotcha 22).
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
from app.services.sandbox import SandboxNotFoundError, SandboxRuntimeError
from app.services.sandbox.docker import DockerRuntime

logger = structlog.get_logger(__name__)

PROVIDER = "docker"


async def _try_lock(session: AsyncSession, name: str) -> bool:
    return bool(await session.scalar(select(func.pg_try_advisory_xact_lock(func.hashtext(name)))))


def _count(action: str, amount: int = 1) -> None:
    if amount:
        sandbox_maintenance_actions_total.labels(provider=PROVIDER, action=action).inc(amount)


def _idle(minutes: int):
    last_used = func.coalesce(SandboxActivity.last_activity_at, Workspace.updated_at)
    return last_used < func.clock_timestamp() - timedelta(minutes=minutes)


def _idle_workspaces(minutes: int):
    return (
        select(Workspace)
        .outerjoin(SandboxActivity, SandboxActivity.workspace_id == Workspace.id)
        .where(
            Workspace.runtime == PROVIDER,
            Workspace.sandbox_id.is_not(None),
            Workspace.status != "error",
            _idle(minutes),
        )
    )


async def stop_idle_sandboxes(
    session_maker: async_sessionmaker[AsyncSession], settings: Settings, runtime: DockerRuntime
) -> int:
    """Stop (and keep) sandboxes idle past the window. The lock only guards the selection;
    each stop happens outside any transaction, after re-checking that the sandbox is still
    idle -- a stop takes seconds and must not hold a pooled connection or kill a command
    that started meanwhile. A sandbox with a live exec or terminal is skipped by the
    provider."""
    minutes = settings.sandbox_auto_stop_minutes
    if minutes <= 0:
        return 0
    async with session_maker() as session, session.begin():
        if not await _try_lock(session, "sandbox_idle_reaper"):
            return 0
        candidates = list((await session.scalars(_idle_workspaces(minutes))).all())
    stopped = 0
    for workspace in candidates:
        async with session_maker() as session:
            recheck = _idle_workspaces(minutes).where(Workspace.id == workspace.id)
            still_idle = await session.scalar(select(func.count()).select_from(recheck.subquery()))
            capped = _idle_workspaces(max(minutes, settings.sandbox_busy_max_minutes)).where(
                Workspace.id == workspace.id
            )
            past_cap = await session.scalar(select(func.count()).select_from(capped.subquery()))
        if not still_idle:
            continue
        try:
            if await runtime.stop_if_running(workspace, force=bool(past_cap)):
                stopped += 1
                logger.info("sandbox_idle_stopped", workspace_id=str(workspace.id))
        except SandboxNotFoundError:
            continue  # its container is gone: the orphan pass flags that, not this one
        except SandboxRuntimeError as error:
            logger.warning(
                "sandbox_idle_stop_failed",
                workspace_id=str(workspace.id),
                error_type=type(error).__name__,
            )
    _count("idle_stopped", stopped)
    return stopped


def _workspace_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


async def reconcile_orphans(
    session_maker: async_sessionmaker[AsyncSession],
    runtime: DockerRuntime,
    *,
    only: set[UUID] | None = None,
) -> dict[str, int]:
    """Remove this installation's sandboxes whose workspace row is gone, and flag
    workspaces whose container vanished. ``only`` restricts the pass to those workspace
    ids (tests share one database with other suites and a running dev stack)."""
    report = {"orphans_removed": 0, "sandboxes_lost": 0}
    async with session_maker() as session, session.begin():
        if not await _try_lock(session, "sandbox_orphan_reconcile"):
            return report
        # Listed under the lock, so a provision finishing meanwhile is seen consistently.
        containers = await runtime.managed_sandboxes()
        network_owners = await runtime.managed_network_workspaces()
        if only is not None:
            containers = [c for c in containers if _workspace_uuid(c[1]) in only]
            network_owners = [w for w in network_owners if _workspace_uuid(w) in only]
        claimed = {
            found
            for found in (_workspace_uuid(v) for v in [w for _, w in containers] + network_owners)
            if found is not None
        }
        existing = set(
            await session.scalars(select(Workspace.id).where(Workspace.id.in_(claimed)))
        )
        removed: set[UUID] = set()
        for container_id, label in containers:
            workspace_id = _workspace_uuid(label)
            if workspace_id is None or workspace_id in existing:
                continue  # ours but unlabelled: leave it for a human, never guess
            await runtime.remove_sandbox(workspace_id, container_id)
            removed.add(workspace_id)
            report["orphans_removed"] += 1
            logger.info("sandbox_orphan_removed", workspace_id=str(workspace_id))
        for label in network_owners:
            workspace_id = _workspace_uuid(label)
            if workspace_id and workspace_id not in existing and workspace_id not in removed:
                await runtime.remove_sandbox(workspace_id, None)
                _count("network_removed")

        live = {container_id for container_id, _ in containers}
        query = select(Workspace).where(
            Workspace.runtime == PROVIDER,
            Workspace.sandbox_id.is_not(None),
            Workspace.status != "error",
        )
        if only is not None:
            query = query.where(Workspace.id.in_(only))
        for workspace in (await session.scalars(query)).all():
            if workspace.sandbox_id not in live:
                workspace.status = "error"
                report["sandboxes_lost"] += 1
                logger.warning("sandbox_container_lost", workspace_id=str(workspace.id))
    _count("orphan_removed", report["orphans_removed"])
    _count("sandbox_lost", report["sandboxes_lost"])
    return report


async def isolate_legacy_sandboxes(
    session_maker: async_sessionmaker[AsyncSession],
    runtime: DockerRuntime,
    *,
    only: set[UUID] | None = None,
) -> int:
    """Isolate sandboxes from before the installation label, but only those this database
    names as a workspace's sandbox (same workspace id and container id): an unlabelled
    container could be another stack's, which must not get this stack's proxy."""
    legacy = await runtime.legacy_sandboxes()
    by_workspace = {
        workspace_id: container_id
        for container_id, label in legacy
        if (workspace_id := _workspace_uuid(label)) is not None
        and (only is None or workspace_id in only)
    }
    if not by_workspace:
        return 0
    async with session_maker() as session:
        rows = (
            await session.scalars(
                select(Workspace).where(
                    Workspace.id.in_(set(by_workspace)), Workspace.runtime == PROVIDER
                )
            )
        ).all()
    isolated = 0
    for workspace in rows:
        if workspace.sandbox_id != by_workspace.get(workspace.id):
            continue
        try:
            await runtime.isolate_sandbox(workspace)
            isolated += 1
        except SandboxRuntimeError as error:
            logger.warning(
                "sandbox_legacy_isolate_failed",
                workspace_id=str(workspace.id),
                error_type=type(error).__name__,
            )
    _count("legacy_isolated", isolated)
    return isolated


async def run_sandbox_maintenance(
    session_maker: async_sessionmaker[AsyncSession], settings: Settings, runtime: DockerRuntime
) -> None:
    try:
        await reconcile_orphans(session_maker, runtime)
    except Exception:
        logger.exception("sandbox_orphan_reconcile_failed")
    while True:
        try:
            await runtime.reconcile_networks()
            await isolate_legacy_sandboxes(session_maker, runtime)
            await stop_idle_sandboxes(session_maker, settings, runtime)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sandbox_maintenance_pass_failed")
        await asyncio.sleep(settings.docker_reaper_interval_seconds)


def start_sandbox_maintenance(
    session_maker: async_sessionmaker[AsyncSession], settings: Settings
) -> asyncio.Task | None:
    """Start the upkeep loop on a Docker profile whose daemon this process can reach.

    Gated on ``SANDBOX_RUNTIME_DEFAULT=docker`` (what docker-compose.sandbox.yml sets): the
    orphan sweep removes managed containers that have no workspace row, so a Daytona-default
    process -- the test suite's app included -- must never run it.
    """
    from app.services.sandbox.factory import get_runtime_by_name

    if settings.sandbox_runtime_default != PROVIDER:
        return None
    runtime = get_runtime_by_name(PROVIDER)
    if not isinstance(runtime, DockerRuntime) or not runtime.configured:
        return None
    return asyncio.create_task(run_sandbox_maintenance(session_maker, settings, runtime))
