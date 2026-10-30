"""The shared idle clock for local Docker sandboxes (local-docker-runtime Epic 05).

A sandbox is used from three processes (API, worker, preview proxy), so its last-activity
time lives in Postgres (``sandbox_activity``), never in memory. Each process debounces its
own writes on this recorder instance -- at most one upsert per workspace per window -- so a
busy terminal or preview costs one statement every ``debounce_seconds``. The timestamp is
Postgres' ``clock_timestamp()``, the clock the reaper compares against.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert

from app.models import SandboxActivity

logger = structlog.get_logger(__name__)

ACTIVITY_DEBOUNCE_SECONDS = 30


class SandboxActivityRecorder:
    def __init__(
        self,
        session_maker: Callable[[], Any] | None = None,
        *,
        debounce_seconds: float = ACTIVITY_DEBOUNCE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_maker = session_maker
        self._debounce = debounce_seconds
        self._clock = clock
        self._last: dict[UUID, float] = {}  # this instance's writes, for debouncing only

    async def touch(self, workspace_id: UUID) -> None:
        now = self._clock()
        last = self._last.get(workspace_id)
        if last is not None and now - last < self._debounce:
            return
        self._last[workspace_id] = now
        if len(self._last) > 4096:  # a long-lived process: forget windows that have closed
            self._last = {k: v for k, v in self._last.items() if now - v < self._debounce}
        stamp = func.clock_timestamp()
        statement = (
            insert(SandboxActivity)
            .values(workspace_id=workspace_id, last_activity_at=stamp)
            .on_conflict_do_update(
                index_elements=[SandboxActivity.workspace_id], set_={"last_activity_at": stamp}
            )
        )
        try:
            async with self._maker()() as session:
                await session.execute(statement)
                await session.commit()
        except Exception as error:  # never break the exec/preview this rides on
            self._last.pop(workspace_id, None)  # retry on the next touch
            logger.warning("sandbox_activity_write_failed", error_type=type(error).__name__)

    def _maker(self) -> Callable[[], Any]:
        if self._session_maker is None:
            from app.db.session import async_session_maker

            self._session_maker = async_session_maker
        return self._session_maker
