"""Where a verification run executes: an in-process task (inline) or the arq queue (Plan 2).

``start_verification_run`` is the only entry point the API uses. Inline mode drives the run in
a task registered in ``run_registry`` so the shutdown drain waits for (or cancels and sweeps)
it; queue mode enqueues ``run_verification_job`` (``app.worker``), which advances the run and
defers itself while a fix agent run is in progress.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.services.artifact_store import get_artifact_store
from app.services.jobs import RunDispatcher, RunQueueUnavailableError, get_run_dispatcher
from app.services.runtime import run_registry
from app.services.sandbox import SandboxRuntime
from app.services.verification.fixer import AgentFixer
from app.services.verification.orchestrator import (
    ACTIVE_STATUSES,
    PlanCriteria,
    VerificationOrchestrator,
)

logger = structlog.get_logger(__name__)


def build_orchestrator(
    settings: Settings,
    *,
    session_maker: async_sessionmaker[AsyncSession] | None = None,
    runtime: SandboxRuntime | None = None,
) -> VerificationOrchestrator:
    """The production orchestrator: criteria come from each run's row, fixes from an agent."""
    orchestrator = VerificationOrchestrator(
        PlanCriteria(test_command=""),
        session_maker=session_maker,
        runtime=runtime,
        poll_seconds=settings.verification_poll_seconds,
    )
    orchestrator.fixer = AgentFixer(session_maker=orchestrator.session_maker, settings=settings)
    orchestrator.artifact_store = get_artifact_store(settings)
    return orchestrator


async def start_verification_run(
    run_id: UUID,
    settings: Settings,
    *,
    session_maker: async_sessionmaker[AsyncSession] | None = None,
    runtime: SandboxRuntime | None = None,
    dispatcher: RunDispatcher | None = None,
) -> None:
    orchestrator = build_orchestrator(settings, session_maker=session_maker, runtime=runtime)
    if settings.run_execution_mode == "queue":
        try:
            await (dispatcher or get_run_dispatcher()).enqueue_verification_run(run_id)
        except Exception as error:
            # The committed `planning` row would otherwise hold the workspace guard forever.
            await orchestrator.fail(run_id)
            raise RunQueueUnavailableError from error
        return

    task = asyncio.create_task(_drive_inline(orchestrator, run_id))
    # The registry keeps the strong reference and lets the shutdown drain cover the task.
    run_registry.register(run_id, task)


async def _drive_inline(orchestrator: VerificationOrchestrator, run_id: UUID) -> None:
    try:
        await orchestrator.drive(run_id)
    except Exception:
        # Nothing awaits this task's result. End the run so it never holds the workspace's
        # guard (``advance`` already did for a failing step, but not for e.g. entering it).
        logger.exception("verification_run_failed", run_id=str(run_id))
        await orchestrator.fail(run_id)


async def stop_verification_run(orchestrator: VerificationOrchestrator, run_id: UUID) -> str | None:
    """Stop an active run as ``rejected``; the status it left, ``None`` if it was not active.

    ``end`` also stops a fix agent run. An inline test or observation is cancelled with its
    task (cancelling the sandbox command); an inline fix is left to exit on its next poll, so
    the agent's own bookkeeping is never interrupted. A queue job exits on its next step.
    """
    previous = await orchestrator.end(run_id, "rejected", ACTIVE_STATUSES)
    if previous in ("testing", "observing"):
        task = run_registry.get_task(run_id)
        if task is not None and not task.done():
            task.cancel()
    return previous
