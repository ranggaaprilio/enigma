"""Test runner adapter for the Verifiable Agent Loop.

Runs a single test command inside a workspace's sandbox and records the outcome as a
``VerificationEvidence`` row attached to the given ``VerificationRun``.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_maker
from app.models import VerificationEvidence, VerificationRun, Workspace
from app.services.sandbox import (
    CommandTimeoutError,
    SandboxRuntime,
    SandboxRuntimeError,
    get_sandbox_runtime,
)

logger = structlog.get_logger(__name__)

TEST_KIND = "test"

# Evidence keeps only the tail of a test command's output: it is what the fix agent and the
# human reviewer need (failures are summarised last), and it is stored in JSONB.
OUTPUT_TAIL_CHARS = 8 * 1024


def _output_tail(stdout: str, stderr: str) -> tuple[str, bool]:
    """Combine stdout and stderr (providers differ on merging them) and keep the tail."""
    combined = "\n".join(part for part in (stdout, stderr) if part)
    if len(combined) <= OUTPUT_TAIL_CHARS:
        return combined, False
    return combined[-OUTPUT_TAIL_CHARS:], True


async def run_tests(
    workspace: Workspace,
    run: VerificationRun,
    command: str,
    *,
    cwd: str | None = None,
    timeout: int | None = None,
    runtime: SandboxRuntime | None = None,
    session: AsyncSession | None = None,
) -> VerificationEvidence:
    """Execute ``command`` in ``workspace``'s sandbox and record the outcome as evidence.

    ``exit_code == 0`` maps to ``passed``; any other exit code maps to ``failed``. A
    ``CommandTimeoutError`` (or any other ``SandboxRuntimeError``) maps to ``error``, with
    the reason noted in ``detail``. The result is persisted as a new
    ``VerificationEvidence(kind="test")`` on ``run`` and returned.

    ``runtime`` and ``session`` default to the process sandbox dispatcher and a fresh
    session from ``async_session_maker``; callers in an active request/worker may inject
    either.
    """
    runtime = runtime or get_sandbox_runtime()

    status: str
    detail: dict
    try:
        result = await runtime.exec(workspace, command, cwd=cwd, timeout=timeout)
    except CommandTimeoutError as error:
        status = "error"
        detail = {"command": command, "timeout": timeout, "error": str(error)}
    except SandboxRuntimeError as error:
        status = "error"
        detail = {"command": command, "error": str(error)}
    else:
        status = "passed" if result.exit_code == 0 else "failed"
        output, truncated = _output_tail(result.stdout, result.stderr)
        detail = {
            "exit_code": result.exit_code,
            "command": command,
            "output": output,
            "truncated": truncated,
        }

    evidence = VerificationEvidence(run_id=run.id, kind=TEST_KIND, status=status, detail=detail)
    if session is not None:
        session.add(evidence)
        await session.flush()
        await session.commit()
        await session.refresh(evidence)
    else:
        async with async_session_maker() as new_session:
            new_session.add(evidence)
            await new_session.commit()
            await new_session.refresh(evidence)

    logger.info(
        "verification_test_recorded",
        run_id=str(run.id),
        command=command,
        status=status,
    )
    return evidence
