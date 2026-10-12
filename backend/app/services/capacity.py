"""Epic 06: run admission control -- concurrent-run caps and queue-depth backpressure.

Caps are Postgres counts of active agent runs, taken under transaction-scoped advisory locks
so the count-then-insert in ``AgentService.create_run`` is atomic across replicas (the locks
release at its commit). Lock order is always workspace -> team -> global, so no two creates
can wait on each other in a cycle. Command runs are not capped, only subject to backpressure.
"""

from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.metrics import run_capacity_rejections_total, run_queue_depth
from app.models import AgentRun, CommandRun, Workspace

ACTIVE_RUN_STATUSES = ("thinking", "executing")
ACTIVE_COMMAND_RUN_STATUS = "running"

_MESSAGES = {
    "team": "Your team is at its limit of concurrent agent runs.",
    "global": "The platform is at its limit of concurrent agent runs.",
    "queue": "The run queue is backed up.",
}


class RunCapacityExceededError(Exception):
    def __init__(self, scope: str, retry_after: int) -> None:
        self.scope = scope
        self.retry_after = retry_after
        super().__init__(_MESSAGES[scope])


def _reject(scope: str, settings: Settings) -> RunCapacityExceededError:
    run_capacity_rejections_total.labels(scope).inc()
    return RunCapacityExceededError(scope, settings.run_capacity_retry_after_seconds)


async def _xact_lock(session: AsyncSession, key: str) -> None:
    await session.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(key, 0))))


async def enforce_run_caps(session: AsyncSession, settings: Settings, workspace: Workspace) -> None:
    """Refuse a new agent run when its team or the platform is at its concurrent-run cap.

    Call with the workspace advisory lock already held, in the transaction that inserts the run.
    """
    if settings.run_cap_per_team and workspace.team_id is not None:
        await _xact_lock(session, f"agent_run:team:{workspace.team_id}")
        team_active = await session.scalar(
            select(func.count(AgentRun.id))
            .join(Workspace, Workspace.id == AgentRun.workspace_id)
            .where(
                Workspace.team_id == workspace.team_id,
                AgentRun.status.in_(ACTIVE_RUN_STATUSES),
            )
        )
        if team_active >= settings.run_cap_per_team:
            raise _reject("team", settings)

    if settings.run_cap_global:
        await _xact_lock(session, "agent_run:global")
        active = await session.scalar(
            select(func.count(AgentRun.id)).where(AgentRun.status.in_(ACTIVE_RUN_STATUSES))
        )
        if active >= settings.run_cap_global:
            raise _reject("global", settings)


async def waiting_run_counts(session: AsyncSession) -> tuple[int, int]:
    """Agent and command runs enqueued but not yet claimed by a worker.

    The one definition of queue depth: admission (below), the fleet gauges (Epic 09) and the
    KEDA worker trigger (``deploy/k8s``) must all count these rows.
    """
    waiting_agents = await session.scalar(
        select(func.count(AgentRun.id)).where(
            AgentRun.status.in_(ACTIVE_RUN_STATUSES),
            AgentRun.enqueued_at.is_not(None),
            AgentRun.claimed_at.is_(None),
        )
    )
    waiting_commands = await session.scalar(
        select(func.count(CommandRun.id)).where(
            CommandRun.status == ACTIVE_COMMAND_RUN_STATUS,
            CommandRun.enqueued_at.is_not(None),
            CommandRun.claimed_at.is_(None),
        )
    )
    return waiting_agents or 0, waiting_commands or 0


async def enforce_queue_depth(session: AsyncSession, settings: Settings) -> None:
    """Refuse new runs while too many queued runs wait for a worker (queue mode only).

    Unlocked on purpose: a soft brake, not an invariant, so a burst may overshoot slightly.
    """
    if settings.run_execution_mode != "queue" or not settings.run_queue_max_depth:
        return
    waiting_agents, waiting_commands = await waiting_run_counts(session)
    depth = waiting_agents + waiting_commands
    run_queue_depth.set(depth)
    if depth >= settings.run_queue_max_depth:
        raise _reject("queue", settings)


async def run_capacity_exceeded_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RunCapacityExceededError)
    # A cap is the tenant's own limit (429); a backed-up queue is server overload (503).
    status_code = 503 if exc.scope == "queue" else 429
    return JSONResponse(
        status_code=status_code,
        content={"detail": str(exc), "scope": exc.scope},
        headers={"Retry-After": str(exc.retry_after)},
    )
