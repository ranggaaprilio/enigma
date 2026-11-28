"""The agent side of the verification loop's *Iterate* step (Plan 2 Task 4).

When a test fails and iterations remain, the orchestrator asks a ``Fixer`` to start one agent
run that fixes the code, then polls ``Fixer.status`` until that run is terminal and re-tests.
``AgentFixer`` is the production fixer: it goes through ``AgentService.create_run`` so budget,
concurrency caps, the one-active-run guard and request correlation all still apply, and maps
every refusal to ``FixRefusedError`` with a human-readable reason for the evidence.
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.lifecycle import ServiceDrainingError
from app.models import AgentRun, VerificationEvidence, VerificationRun, Workspace
from app.services.agents import (
    AgentConfigurationError,
    AgentRunAlreadyActiveError,
    AgentService,
)
from app.services.budgets import BudgetExceededError
from app.services.capacity import RunCapacityExceededError
from app.services.jobs import RunQueueUnavailableError
from app.services.workspaces import WorkspaceAccessDeniedError, WorkspaceNotFoundError

logger = structlog.get_logger(__name__)


class FixRefusedError(Exception):
    """The fix agent run could not be started; ``str(error)`` is the reason for the human."""


class FixDeferredError(Exception):
    """The fix agent run cannot start *right now* (this process is draining); try later."""


class Fixer(Protocol):
    async def start(
        self, workspace: Workspace, run: VerificationRun, failing: VerificationEvidence | None
    ) -> UUID: ...

    async def status(self, agent_run_id: UUID) -> str | None: ...

    async def stop(self, run: VerificationRun, agent_run_id: UUID) -> None: ...


def build_fix_prompt(*, plan_ref: str, command: str, exit_code: Any, output: str) -> str:
    return (
        "A verification run failed its tests and needs a fix.\n\n"
        f"Plan: {plan_ref} (read it if it is a file in the repository).\n"
        f"Test command: {command}\n"
        f"Exit code: {exit_code}\n\n"
        "Tail of the test output:\n"
        f"```\n{output}\n```\n\n"
        "Change the code so the test command passes and the plan is still satisfied. "
        "Do not delete or weaken tests to make them pass. "
        "Finish with a short summary of what you changed."
    )


def _refusal_reason(error: Exception) -> str:
    if isinstance(error, AgentRunAlreadyActiveError):
        return "the workspace already has an active agent run"
    if isinstance(error, BudgetExceededError):
        return "an enforcing budget is over its limit"
    if isinstance(error, RunCapacityExceededError):
        return str(error) or "agent run capacity is exhausted"
    if isinstance(error, AgentConfigurationError):
        return "no LLM is configured for agent runs"
    if isinstance(error, RunQueueUnavailableError):
        return "the run queue is unavailable"
    return "the run's author can no longer drive this workspace"


_REFUSALS = (
    AgentRunAlreadyActiveError,
    BudgetExceededError,
    RunCapacityExceededError,
    AgentConfigurationError,
    RunQueueUnavailableError,
    WorkspaceAccessDeniedError,
    WorkspaceNotFoundError,
)


class AgentFixer:
    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        settings: Settings | None = None,
        service_factory: Any = AgentService,
    ) -> None:
        self.session_maker = session_maker
        self.settings = settings or get_settings()
        self.service_factory = service_factory

    async def start(
        self, workspace: Workspace, run: VerificationRun, failing: VerificationEvidence | None
    ) -> UUID:
        if run.created_by is None:
            # Agent runs are attributed and budgeted to a user; a run without one cannot spend.
            raise FixRefusedError("the run has no author to attribute the fix agent run to")
        # ``None`` only if the run reached `fixing` without test evidence (e.g. hand-edited).
        detail = (failing.detail if failing is not None else None) or {}
        prompt = build_fix_prompt(
            plan_ref=run.plan_ref,
            command=str(detail.get("command", "")),
            exit_code=detail.get("exit_code", "n/a"),
            output=str(detail.get("output") or detail.get("error") or ""),
        )
        async with self.session_maker() as session:
            # Plan 9: verifying an agent run, the fix continues it -- same worktree (so it lands
            # on that branch) and same session (so the agent sees what it did). Spec Kit and
            # hand-started runs have no source and start fresh, as does a deleted source.
            source = (
                await session.get(AgentRun, run.source_agent_run_id)
                if run.source_agent_run_id is not None
                else None
            )
            service = self.service_factory(session, self.session_maker, self.settings)
            try:
                agent_run = await service.create_run(
                    workspace.id,
                    run.created_by,
                    prompt,
                    working_dir=(run.criteria or {}).get("cwd"),
                    worktree_id=source.worktree_id if source is not None else None,
                    session_id=source.session_id if source is not None else None,
                )
            except ServiceDrainingError:
                # A rollout is not a reason to give up on the fix: wait for a live process.
                raise FixDeferredError from None
            except _REFUSALS as error:
                raise FixRefusedError(_refusal_reason(error)) from None
            return agent_run.id

    async def status(self, agent_run_id: UUID) -> str | None:
        async with self.session_maker() as session:
            agent_run = await session.get(AgentRun, agent_run_id)
            return agent_run.status if agent_run is not None else None

    async def stop(self, run: VerificationRun, agent_run_id: UUID) -> None:
        """Stop a fix agent run its verification run no longer waits for. Best effort.

        Goes through ``AgentService.stop_active_run`` (idempotent, no permission check: the
        run's author may since have lost access or been deleted), so the agent's events, lease
        and ``stopped`` control message stay consistent; a failure is logged, never raised,
        because the verification run has already ended.
        """
        try:
            async with self.session_maker() as session:
                service = self.service_factory(session, self.session_maker, self.settings)
                await service.stop_active_run(agent_run_id)
        except Exception:
            logger.exception(
                "verification_fix_stop_failed",
                run_id=str(run.id),
                agent_run_id=str(agent_run_id),
            )
