"""Close out runs whose owning process is gone.

Agent runs, command runs and Spec Kit phase finalizers are driven by in-process asyncio tasks.
When the process dies (crash, deploy, OOM) their rows stay in an active status forever, and the
per-workspace guard in ``AgentService.create_run`` then blocks every new agent run in that
workspace. This sweep marks them ``error`` with ``error_reason = "interrupted_by_restart"``.

Startup calls it unscoped: *every* active row is treated as orphaned, which is only correct
while a single backend replica runs at a time (see
docs/horizontal-scale/01-stateless-control-plane.md). Shutdown passes ``run_ids`` -- the runs
this process owned -- so it never touches another live replica's work.

In ``RUN_EXECUTION_MODE=queue`` (Epic 02) the API owns no runs and skips the unscoped sweep;
each worker passes its own ``worker_id`` instead, reclaiming only the runs it claimed. Spec
phases are then advanced by DB-driven finalizer jobs, so a worker sweep leaves them alone.
Runs whose worker never comes back are ended by their expired leases (Epic 04,
``app.services.reaper``), so this sweep only speeds up the common restart case.
"""

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import AgentEvent, AgentRun, CommandRun, SpecPhase, SpecWorkflow
from app.services.agents import ACTIVE_RUN_STATUSES, AgentRuntime, agent_runtime, event_payload
from app.services.commands import CommandRuntime, command_runtime, command_status_frame

logger = structlog.get_logger(__name__)

INTERRUPTED_BY_RESTART = "interrupted_by_restart"
INTERRUPTED_MESSAGE = "Run interrupted by a server restart. Please retry."


@dataclass
class ReconciliationResult:
    agent_run_ids: list[UUID] = field(default_factory=list)
    command_run_ids: list[UUID] = field(default_factory=list)
    spec_phase_ids: list[UUID] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.agent_run_ids) + len(self.command_run_ids) + len(self.spec_phase_ids)


async def reconcile_interrupted_runs(
    session_maker: async_sessionmaker[AsyncSession],
    *,
    agent_runtime: AgentRuntime = agent_runtime,
    command_runtime: CommandRuntime = command_runtime,
    run_ids: Collection[UUID] | None = None,
    worker_id: str | None = None,
) -> ReconciliationResult:
    """Mark orphaned runs interrupted. ``run_ids`` limits the sweep to those agent/command run
    ids and spec phase ids; ``worker_id`` to the runs that worker claimed (and no spec phases);
    neither sweeps every active row."""
    result = ReconciliationResult()
    events: list[AgentEvent] = []
    now = datetime.now(UTC)

    async with session_maker() as session:
        agent_query = select(AgentRun).where(AgentRun.status.in_(ACTIVE_RUN_STATUSES))
        command_query = select(CommandRun).where(CommandRun.status == "running")
        phase_query = select(SpecPhase).where(SpecPhase.status == "running")
        if run_ids is not None:
            ids = list(run_ids)
            agent_query = agent_query.where(AgentRun.id.in_(ids))
            command_query = command_query.where(CommandRun.id.in_(ids))
            phase_query = phase_query.where(SpecPhase.id.in_(ids))
        if worker_id is not None:
            agent_query = agent_query.where(AgentRun.worker_id == worker_id)
            command_query = command_query.where(CommandRun.worker_id == worker_id)
            phase_query = phase_query.where(False)

        agent_runs = (await session.execute(agent_query.with_for_update())).scalars().all()
        for run in agent_runs:
            run.status = "error"
            run.error_reason = INTERRUPTED_BY_RESTART
            run.error_message = INTERRUPTED_MESSAGE
            run.completed_at = now
            run.lease_expires_at = None
            event = AgentEvent(
                agent_run_id=run.id,
                event_type="error",
                content=INTERRUPTED_MESSAGE,
                event_metadata={"status": "error", "reason": INTERRUPTED_BY_RESTART},
            )
            session.add(event)
            events.append(event)
            result.agent_run_ids.append(run.id)

        command_runs = (await session.execute(command_query.with_for_update())).scalars().all()
        for run in command_runs:
            run.status = "error"
            run.error_reason = INTERRUPTED_BY_RESTART
            run.completed_at = now
            run.lease_expires_at = None
            run.output = f"{run.output}\n{INTERRUPTED_MESSAGE}\n"
            result.command_run_ids.append(run.id)

        # A phase is "running" only while its in-process finalizer polls the agent run; with
        # the process gone nothing will ever advance it. A workflow's own "running" also means
        # "idle between phases", so only workflows with a swept phase are touched.
        phases = (await session.execute(phase_query.with_for_update())).scalars().all()
        for phase in phases:
            phase.status = "error"
            result.spec_phase_ids.append(phase.id)
        workflows = []
        if phases:
            workflows = (
                await session.execute(
                    select(SpecWorkflow)
                    .where(
                        SpecWorkflow.id.in_({phase.workflow_id for phase in phases}),
                        SpecWorkflow.status == "running",
                    )
                    .with_for_update()
                )
            ).scalars().all()
        for workflow in workflows:
            workflow.status = "error"

        await session.flush()
        for event in events:
            await session.refresh(event)
        request_ids = {run.id: run.request_id for run in agent_runs}
        agent_payloads = [
            (event.agent_run_id, event_payload(event, request_ids.get(event.agent_run_id)))
            for event in events
        ]
        command_payloads = [(run.id, command_status_frame(run)) for run in command_runs]
        await session.commit()

    # Only after the commit, so a subscriber that re-reads the run sees the terminal state.
    for run_id, payload in agent_payloads:
        await agent_runtime.publish(run_id, payload)
    for run_id, payload in command_payloads:
        await command_runtime.publish(run_id, payload)

    if result.total or workflows:
        logger.warning(
            "interrupted_runs_reconciled",
            agent_runs=len(result.agent_run_ids),
            command_runs=len(result.command_run_ids),
            spec_phases=len(result.spec_phase_ids),
            spec_workflows=len(workflows),
        )
    return result
