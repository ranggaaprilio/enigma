"""Verification orchestrator for the Verifiable Agent Loop.

Drives one verification run through ``test -> (fix -> test)* -> observe -> review`` and hands
the result to the human gate. The state machine (``VerificationRun.status``) is:

    planning -> testing -> [fixing -> testing]* -> observing -> review -> approved/rejected

The loop is resumable: ``advance`` performs exactly one step chosen from the run's persisted
state (``status``, ``criteria``, ``iteration``, ``agent_run_id``) and returns a ``Step``.
``drive`` loops it in-process (inline mode, sleeping on ``WAIT``); the queue job calls it once
per attempt and defers on ``WAIT``, so a worker never holds a slot while a fix agent run waits
for one. With a ``Fixer``, a failed test with iterations left hands off to one agent run that
fixes the code; without one, the test is simply re-run.

Concurrency: the one-active-run-per-workspace guard is the ``RunLock`` (process-local) plus a
transaction-scoped Postgres advisory lock around the guard check and the first transition
(cross-replica). Every transition is a conditional UPDATE, mirroring
``AgentService._transition``: a step that lost a race writes nothing, so replays are harmless.
There is no module-level in-flight run state.

Crash-safety: any non-provider exception escaping a step moves the run to the terminal
``error`` status before it is re-raised, so the one-active guard can never wedge a workspace.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

import structlog
from httpx import BaseTransport
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import async_session_maker
from app.models import VerificationEvidence, VerificationRun, Workspace
from app.services.runtime import RunLock
from app.services.runtime import run_lock as default_run_lock
from app.services.sandbox import SandboxRuntime, get_sandbox_runtime
from app.services.verification.fixer import FixDeferredError, Fixer, FixRefusedError
from app.services.verification.observation import observe
from app.services.verification.test_runner import run_tests

logger = structlog.get_logger(__name__)

# A run in any of these statuses is "in flight": a second loop in the same workspace is refused.
ACTIVE_STATUSES = ("planning", "testing", "fixing", "observing")
# Agent run statuses that mean the fix attempt is still in progress.
AGENT_ACTIVE_STATUSES = ("thinking", "executing")


@dataclass(frozen=True)
class PlanCriteria:
    """What one verification loop must satisfy to be handed to the human gate.

    ``test_command`` is run through the test runner up to ``max_iterations`` times until it
    passes; between attempts a ``Fixer`` (if any) lets an agent fix the code. ``observe_port``
    is observed through the observation interface before the gate (``None`` skips it).
    Persisted on the run as ``criteria`` so any process can resume it.
    """

    test_command: str
    observe_port: int | None = None
    cwd: str | None = None
    timeout: int | None = None
    max_iterations: int = 1


class VerificationRunNotFoundError(Exception):
    pass


class VerificationRunNotInWorkspaceError(Exception):
    pass


class VerificationRunAlreadyActiveError(Exception):
    pass


class Step(Enum):
    """What ``advance`` left the run needing next."""

    CONTINUE = "continue"  # advance again right away
    WAIT = "wait"  # waiting on something external (an agent run); advance again later
    DONE = "done"  # parked at the human gate or terminal; nothing left to drive


def criteria_from_row(row: dict, fallback: PlanCriteria) -> PlanCriteria:
    """The run's persisted criteria; ``fallback`` for rows written before Plan 2 (empty)."""
    if not row:
        return fallback
    return PlanCriteria(
        test_command=row["test_command"],
        observe_port=row.get("observe_port"),
        cwd=row.get("cwd"),
        timeout=row.get("timeout"),
        max_iterations=row.get("max_iterations", 1),
    )


def criteria_to_row(criteria: PlanCriteria) -> dict:
    return {
        "test_command": criteria.test_command,
        "observe_port": criteria.observe_port,
        "cwd": criteria.cwd,
        "timeout": criteria.timeout,
        "max_iterations": criteria.max_iterations,
    }


class VerificationOrchestrator:
    def __init__(
        self,
        plan_criteria: PlanCriteria,
        *,
        session_maker: async_sessionmaker[AsyncSession] | None = None,
        runtime: SandboxRuntime | None = None,
        run_lock: RunLock | None = None,
        observe_transport: BaseTransport | None = None,
        poll_seconds: float = 5.0,
        fixer: Fixer | None = None,
    ) -> None:
        self.plan_criteria = plan_criteria
        self.session_maker = session_maker or async_session_maker
        self.runtime = runtime or get_sandbox_runtime()
        self.run_lock = run_lock or default_run_lock
        self.observe_transport = observe_transport
        self.poll_seconds = poll_seconds
        # Without a fixer, a failed test is simply re-run (``max_iterations`` attempts).
        self.fixer = fixer

    @staticmethod
    def _lock_key(workspace_id: UUID) -> str:
        return f"verification_run:workspace:{workspace_id}"

    async def run(self, workspace: Workspace, run_id: UUID) -> VerificationRun:
        """Enter ``run_id`` from ``planning`` and drive it to the gate; return the final run.

        Idempotent against a run that already left ``planning``: it is returned unchanged and
        no evidence is recorded (use ``drive`` to resume a run another process left mid-loop).
        Raises ``VerificationRunAlreadyActiveError`` when another run in the same workspace is
        still active, and ``VerificationRunNotFoundError`` when ``run_id`` does not exist.
        """
        if not await self._enter(workspace.id, run_id):
            async with self.session_maker() as session:
                return await session.get(VerificationRun, run_id)
        return await self.drive(run_id)

    async def drive(self, run_id: UUID) -> VerificationRun:
        """Advance ``run_id`` until it is parked or terminal, sleeping on ``Step.WAIT``.

        The inline execution path. The queue path calls ``advance`` once per job attempt and
        defers on ``WAIT`` instead, so a worker slot is never held while waiting.
        """
        while True:
            step = await self.advance(run_id)
            if step is Step.DONE:
                break
            if step is Step.WAIT:
                await asyncio.sleep(self.poll_seconds)
        async with self.session_maker() as session:
            return await session.get(VerificationRun, run_id)

    async def advance(self, run_id: UUID) -> Step:
        """Perform exactly one step of the loop, chosen from the run's persisted state.

        Safe to replay: every transition is a conditional UPDATE, so a step that lost a race
        (another process already moved the run) writes nothing. A non-provider exception moves
        the run to ``error`` before it is re-raised, so the one-active guard can never wedge a
        workspace on a crash.
        """
        async with self.session_maker() as session:
            run = await session.get(VerificationRun, run_id)
            if run is None:
                raise VerificationRunNotFoundError(run_id)
            status = run.status
            workspace_id = run.workspace_id

        if status == "planning":
            await self._enter(workspace_id, run_id)
            return Step.CONTINUE
        if status not in ACTIVE_STATUSES:
            return Step.DONE

        try:
            async with self.session_maker() as session:
                run = await session.get(VerificationRun, run_id)
                workspace = await session.get(Workspace, workspace_id)
                criteria = criteria_from_row(run.criteria, self.plan_criteria)
            # A fresh session for the step: it only takes a connection when the step writes,
            # so none is held (idle in transaction) while tests or the observation run.
            async with self.session_maker() as session:
                if status == "testing":
                    await self._step_test(session, workspace, run, criteria)
                    return Step.CONTINUE
                if status == "fixing":
                    return await self._step_fix(session, workspace, run)
                if status == "observing":
                    await self._step_observe(session, workspace, run, criteria)
                    return Step.DONE
            return Step.CONTINUE
        except Exception:
            # Crash-safety: a non-provider exception that escapes a step (a DB blip, a
            # dependency bug — anything the runner's SandboxRuntimeError/CommandTimeoutError
            # and the observer's httpx handling do not absorb) must still land the run in a
            # terminal state, or the one-active guard wedges the workspace forever.
            async with self.session_maker() as session:
                await self._transition(session, run_id, "error", ACTIVE_STATUSES)
                await session.commit()
            raise

    async def _enter(self, workspace_id: UUID, run_id: UUID) -> bool:
        """``planning -> testing`` under the one-active guard; ``False`` if not in planning."""
        lock_key = self._lock_key(workspace_id)
        async with self.run_lock.hold(lock_key):
            # The transaction-scoped advisory lock serialises replicas for the check + first
            # transition; the in-process RunLock serialises this replica.
            async with self.session_maker() as session:
                await session.execute(
                    select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
                )
                run = await session.get(VerificationRun, run_id)
                if run is None:
                    raise VerificationRunNotFoundError(run_id)
                if run.workspace_id != workspace_id:
                    raise VerificationRunNotInWorkspaceError(run_id)
                other = await session.scalar(
                    select(VerificationRun.id).where(
                        VerificationRun.workspace_id == workspace_id,
                        VerificationRun.status.in_(ACTIVE_STATUSES),
                        VerificationRun.id != run_id,
                    )
                )
                if other is not None:
                    raise VerificationRunAlreadyActiveError(other)
                entered = await self._transition(session, run_id, "testing", ("planning",))
                await session.commit()
                return entered

    async def _step_test(
        self,
        session: AsyncSession,
        workspace: Workspace,
        run: VerificationRun,
        criteria: PlanCriteria,
    ) -> None:
        """Run the tests once, then move on: observe, or try again (re-test)."""
        evidence = await run_tests(
            workspace,
            run,
            criteria.test_command,
            cwd=criteria.cwd,
            timeout=criteria.timeout,
            runtime=self.runtime,
            session=session,
        )
        retries_left = run.iteration < max(1, criteria.max_iterations) - 1
        if evidence.status == "passed" or not retries_left:
            await self._transition(session, run.id, "observing", ("testing",))
        elif self.fixer is not None:
            await self._transition(session, run.id, "fixing", ("testing",))
        else:
            await session.execute(
                update(VerificationRun)
                .where(VerificationRun.id == run.id, VerificationRun.status == "testing")
                .values(iteration=VerificationRun.iteration + 1)
                .execution_options(synchronize_session=False)
            )
        await session.commit()

    async def _step_fix(
        self, session: AsyncSession, workspace: Workspace, run: VerificationRun
    ) -> Step:
        """Start one fix agent run, wait for it, record the attempt, then go back to testing.

        Idempotent: the agent run is started under a transaction-scoped advisory lock with the
        row re-read inside it, so a replayed step never starts a second agent run, and the
        hand-back to ``testing`` is a conditional UPDATE keyed on that agent run.
        """
        fixer = self.fixer
        if fixer is None:  # a run left in fixing by a process that had a fixer: just re-test
            await self._transition(session, run.id, "testing", ("fixing",))
            await session.commit()
            return Step.CONTINUE

        if run.agent_run_id is None:
            await session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtextextended(f"verification_fix:{run.id}", 0)
                    )
                )
            )
            run = await session.get(VerificationRun, run.id, populate_existing=True)
            if run.status != "fixing" or run.agent_run_id is not None:
                await session.commit()
                return Step.CONTINUE
            failing = await session.scalar(
                select(VerificationEvidence)
                .where(VerificationEvidence.run_id == run.id, VerificationEvidence.kind == "test")
                .order_by(VerificationEvidence.created_at.desc())
                .limit(1)
            )
            try:
                agent_run_id = await fixer.start(workspace, run, failing)
            except FixDeferredError:
                await session.commit()
                return Step.WAIT
            except FixRefusedError as error:
                # Never wedge: record why, stop fixing, and gate with the evidence so far.
                session.add(
                    VerificationEvidence(
                        run_id=run.id, kind="fix", status="error", detail={"error": str(error)}
                    )
                )
                await self._transition(session, run.id, "observing", ("fixing",))
                await session.commit()
                return Step.CONTINUE
            await session.execute(
                update(VerificationRun)
                .where(VerificationRun.id == run.id, VerificationRun.agent_run_id.is_(None))
                .values(agent_run_id=agent_run_id)
                .execution_options(synchronize_session=False)
            )
            await session.commit()
            logger.info(
                "verification_fix_started", run_id=str(run.id), agent_run_id=str(agent_run_id)
            )
            return Step.WAIT

        agent_run_id = run.agent_run_id
        agent_status = await fixer.status(agent_run_id)
        if agent_status in AGENT_ACTIVE_STATUSES:
            return Step.WAIT

        # Terminal (or vanished): hand back to testing exactly once, then record the attempt.
        handed_back = await session.scalar(
            update(VerificationRun)
            .where(
                VerificationRun.id == run.id,
                VerificationRun.status == "fixing",
                VerificationRun.agent_run_id == agent_run_id,
            )
            .values(status="testing", iteration=VerificationRun.iteration + 1, agent_run_id=None)
            .returning(VerificationRun.id)
            .execution_options(synchronize_session=False)
        )
        if handed_back is not None:
            session.add(
                VerificationEvidence(
                    run_id=run.id,
                    kind="fix",
                    status="passed" if agent_status == "completed" else "error",
                    detail={"agent_run_id": str(agent_run_id), "agent_status": agent_status},
                )
            )
        await session.commit()
        return Step.CONTINUE

    async def _step_observe(
        self,
        session: AsyncSession,
        workspace: Workspace,
        run: VerificationRun,
        criteria: PlanCriteria,
    ) -> None:
        """Observe the preview (best effort), then hand off to the human gate."""
        if criteria.observe_port is not None:
            await observe(
                workspace,
                run,
                criteria.observe_port,
                runtime=self.runtime,
                session=session,
                transport=self.observe_transport,
            )
        await self._transition(session, run.id, "review", ("observing",))
        await session.commit()
        logger.info("verification_run_review", run_id=str(run.id))

    async def create_run(
        self,
        workspace_id: UUID,
        plan_ref: str,
        *,
        criteria: PlanCriteria | None = None,
        created_by: UUID | None = None,
        spec_workflow_id: UUID | None = None,
    ) -> VerificationRun:
        """Create a run parked at ``planning`` (not yet driven), with its criteria persisted.

        Refuses with ``VerificationRunAlreadyActiveError`` *before* inserting when the
        workspace already has an active run, so a refusal never leaves an orphan behind.
        """
        lock_key = self._lock_key(workspace_id)
        async with self.run_lock.hold(lock_key):
            async with self.session_maker() as session:
                await session.execute(
                    select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
                )
                other = await session.scalar(
                    select(VerificationRun.id).where(
                        VerificationRun.workspace_id == workspace_id,
                        VerificationRun.status.in_(ACTIVE_STATUSES),
                    )
                )
                if other is not None:
                    raise VerificationRunAlreadyActiveError(other)
                run = VerificationRun(
                    workspace_id=workspace_id,
                    plan_ref=plan_ref,
                    status="planning",
                    criteria=criteria_to_row(criteria) if criteria is not None else {},
                    created_by=created_by,
                    spec_workflow_id=spec_workflow_id,
                )
                session.add(run)
                await session.commit()
                await session.refresh(run)
                return run

    async def fail(self, run_id: UUID) -> bool:
        """End an active run as ``error`` (e.g. it could not be handed to the queue)."""
        async with self.session_maker() as session:
            changed = await self._transition(session, run_id, "error", ACTIVE_STATUSES)
            await session.commit()
            return changed

    async def get_run(self, workspace_id: UUID, run_id: UUID) -> VerificationRun:
        """Fetch a run by id, refusing runs of another workspace."""
        async with self.session_maker() as session:
            run = await session.get(VerificationRun, run_id)
            if run is None:
                raise VerificationRunNotFoundError(run_id)
            if run.workspace_id != workspace_id:
                raise VerificationRunNotInWorkspaceError(run_id)
            return run

    async def list_runs(
        self,
        workspace_id: UUID,
        *,
        limit: int = 20,
        offset: int = 0,
        spec_workflow_id: UUID | None = None,
    ) -> list[VerificationRun]:
        statement = select(VerificationRun).where(VerificationRun.workspace_id == workspace_id)
        if spec_workflow_id is not None:
            statement = statement.where(VerificationRun.spec_workflow_id == spec_workflow_id)
        async with self.session_maker() as session:
            rows = await session.scalars(
                statement.order_by(VerificationRun.created_at.desc(), VerificationRun.id.desc())
                .limit(limit)
                .offset(offset)
            )
            return list(rows)

    async def approve(self, run_id: UUID) -> bool:
        """Approve at the human gate; ``True`` iff it moved ``review -> approved``."""
        return await self._finalize(run_id, "approved")

    async def reject(self, run_id: UUID) -> bool:
        """Reject the run at the human gate. ``True`` iff it transitioned ``review -> rejected``."""
        return await self._finalize(run_id, "rejected")

    async def _finalize(self, run_id: UUID, status: str) -> bool:
        async with self.session_maker() as session:
            changed = await self._transition(session, run_id, status, ("review",))
            await session.commit()
            return changed

    @staticmethod
    async def _transition(
        session: AsyncSession, run_id: UUID, status: str, from_statuses: tuple[str, ...]
    ) -> bool:
        """Move the run to ``status`` only if it is in ``from_statuses``; ``False`` writes nothing.

        Conditional UPDATE (``synchronize_session=False``), mirroring ``AgentService._transition``:
        a concurrent write that already moved the run out of ``from_statuses`` loses the race and
        must not publish. In-memory ORM copies are left stale; callers re-read afterwards.
        """
        result = await session.execute(
            update(VerificationRun)
            .where(
                VerificationRun.id == run_id,
                VerificationRun.status.in_(from_statuses),
            )
            .values(status=status)
            .returning(VerificationRun.id)
            .execution_options(synchronize_session=False)
        )
        return result.scalar_one_or_none() is not None
