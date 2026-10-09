"""Per-(user, workspace) rate limits for expensive endpoints that have no limiter of their own.

Agent and command runs keep their dependencies next to their routes (``api/agents.py``,
``api/commands.py``); these cover Spec Kit phase triggers and sandbox-touching actions.
"""

from uuid import UUID

from fastapi import Depends

from app.api.auth import CURRENT_USER_DEP
from app.core.config import Settings, get_settings
from app.models import User
from app.services.rate_limit import RedisRateLimiter

SETTINGS_DEP = Depends(get_settings)


async def enforce_spec_run_rate_limit(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    settings: Settings = SETTINGS_DEP,
) -> None:
    """Throttle Spec Kit actions that start an agent run (start, run, answer, request changes)."""
    limiter = RedisRateLimiter(
        prefix="spec:run",
        scope="spec_run",
        attempts=settings.spec_run_rate_limit_attempts,
        window_seconds=settings.spec_run_rate_limit_window_seconds,
    )
    await limiter.check(f"{current_user.id}:{workspace_id}")


async def enforce_sandbox_rate_limit(
    workspace_id: UUID,
    current_user: User = CURRENT_USER_DEP,
    settings: Settings = SETTINGS_DEP,
) -> None:
    """Throttle actions that do work in the workspace sandbox (uploads, writes, starting the
    editor, dev server, services or a terminal)."""
    limiter = RedisRateLimiter(
        prefix="sandbox",
        scope="sandbox",
        attempts=settings.sandbox_rate_limit_attempts,
        window_seconds=settings.sandbox_rate_limit_window_seconds,
    )
    await limiter.check(f"{current_user.id}:{workspace_id}")


SPEC_RUN_RATE_LIMIT_DEP = Depends(enforce_spec_run_rate_limit)
SANDBOX_RATE_LIMIT_DEP = Depends(enforce_sandbox_rate_limit)
