"""Verification orchestrator for the Verifiable Agent Loop.

Drives one verification run through ``test -> observe -> iterate -> review`` and hands the
result to the human gate. The loop state machine (``VerificationRun.status``) is:

    planning -> testing -> observing -> review -> approved/rejected

``run`` walks the run through the active statuses, recording evidence via the Task 3 test
runner and the Task 4 observation interface, then parks it at ``review`` for a human.
``approve``/``reject`` finalize it from ``review`` with a conditional UPDATE — a submission
that loses the race (the run is no longer ``review``) returns ``False`` and writes nothing.

Concurrency: the one-active-run-per-workspace guard is the ``RunLock`` (process-local
serialisation for inline mode) plus a transaction-scoped Postgres advisory lock around the
guard check and the first status transition (cross-replica). Status transitions are always
conditional UPDATEs, mirroring ``AgentService._transition``: a write that no longer matches
returns ``False`` and must not publish. There is no module-level in-flight run state.

Crash-safety: ``run`` enters ``testing`` only from ``planning``, and any non-provider
exception escaping the loop moves the run to the terminal ``error`` status before it is
re-raised, so the one-active guard can never wedge a workspace on a crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import structlog
from httpx import BaseTransport
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import async_session_maker
from app.models import VerificationRun, Workspace
from app.services.runtime import RunLock
from app.services.runtime import run_lock as default_run_lock
from app.services.sandbox import SandboxRuntime, get_sandbox_runtime
from app.services.verification.observation import observe
from app.services.verification.test_runner import run_tests

logger = structlog.get_logger(__name__)

# A run in any of these statuses is "in flight": a second loop in the same workspace is refused.
ACTIVE_STATUSES = ("planning", "testing", "observing")


@dataclass(frozen=True)
class PlanCriteria:
    """What one verification loop must satisfy to be handed to the human gate.

    ``test_command`` is run through the Task 3 runner; the loop re-runs it (up to
    ``max_iterations``) until the evidence status is ``passed`` — the "iterate" step an
    agent-driven fix loop will later interleave with. ``observe_port`` is observed through
    the Task 4 interface after tests pass (``None`` skips observation).
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


class VerificationOrchestrator:
    def __init__(
        self,
        plan_criteria: PlanCriteria,
        *,
        session_maker: async_sessionmaker[AsyncSession] | None = None,
        runtime: SandboxRuntime | None = None,
        run_lock: RunLock | None = None,
        observe_transport: BaseTransport | None = None,
    ) -> None:
        self.plan_criteria = plan_criteria
        self.session_maker = session_maker or async_session_maker
        self.runtime = runtime or get_sandbox_runtime()
        self.run_lock = run_lock or default_run_lock
        self.observe_transport = observe_transport

    @staticmethod
    def _lock_key(workspace_id: UUID) -> str:
        return f"verification_run:workspace:{workspace_id}"

    async def run(self, workspace: Workspace, run_id: UUID) -> VerificationRun:
        """Drive ``run_id`` through test -> observe -> review and return the final run.

        Idempotent against a run that already left ``planning`` (``testing``/``observing``/
        ``review``/``approved``/``rejected``/``error``): it is returned unchanged and no
        evidence is recorded. Raises ``VerificationRunAlreadyActiveError`` when another run in
        the same workspace is still active, and ``VerificationRunNotFoundError`` when ``run_id``
        does not exist.

        A non-provider exception escaping the loop moves the run to ``error`` before it is
        re-raised, so the one-active guard can never wedge a workspace on a crash.
        """
        criteria = self.plan_criteria
        lock_key = self._lock_key(workspace.id)
        async with self.run_lock.hold(lock_key):
            # Enter ``testing`` under the guard. The transaction-scoped advisory lock serialises
            # replicas for the check + first transition; the in-process RunLock serialises this
            # replica. Both are released before the (possibly slow) sandbox calls below.
            async with self.session_maker() as session:
                await session.execute(
                    select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
                )
                run = await session.get(VerificationRun, run_id)
                if run is None:
                    raise VerificationRunNotFoundError(run_id)
                if run.workspace_id != workspace.id:
                    raise VerificationRunNotInWorkspaceError(run_id)
                other = await session.scalar(
                    select(VerificationRun.id).where(
                        VerificationRun.workspace_id == workspace.id,
                        VerificationRun.status.in_(ACTIVE_STATUSES),
                        VerificationRun.id != run_id,
                    )
                )
                if other is not None:
                    raise VerificationRunAlreadyActiveError(other)
                if not await self._transition(session, run_id, "testing", ("planning",)):
                    # Already active or terminal (e.g. a replayed call): nothing to drive.
                    await session.commit()
                    return await session.get(VerificationRun, run_id)
                await session.commit()

            try:
                # Iterate the test until it passes (bounded by max_iterations).
                for _ in range(max(1, criteria.max_iterations)):
                    async with self.session_maker() as session:
                        run = await session.get(VerificationRun, run_id)
                        evidence = await run_tests(
                            workspace,
                            run,
                            criteria.test_command,
                            cwd=criteria.cwd,
                            timeout=criteria.timeout,
                            runtime=self.runtime,
                            session=session,
                        )
                        if evidence.status == "passed":
                            break

                # Observe after tests pass (best effort; a failure records evidence, not a raise).
                async with self.session_maker() as session:
                    run = await session.get(VerificationRun, run_id)
                    await self._transition(session, run_id, "observing", ("testing",))
                    await session.commit()
                    if criteria.observe_port is not None:
                        await observe(
                            workspace,
                            run,
                            criteria.observe_port,
                            runtime=self.runtime,
                            session=session,
                            transport=self.observe_transport,
                        )

                # Hand off to the human gate.
                async with self.session_maker() as session:
                    await self._transition(session, run_id, "review", ("observing",))
                    await session.commit()
                    run = await session.get(VerificationRun, run_id)
            except Exception:
                # Crash-safety: a non-provider exception that escapes the loop (a DB blip, a
                # dependency bug — anything the runner's SandboxRuntimeError/CommandTimeoutError
                # and the observer's httpx handling do not absorb) must still land the run in a
                # terminal state, or the one-active guard wedges the workspace forever.
                async with self.session_maker() as session:
                    await self._transition(session, run_id, "error", ACTIVE_STATUSES)
                    await session.commit()
                raise

        logger.info("verification_run_review", run_id=str(run_id), status=run.status)
        return run

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
