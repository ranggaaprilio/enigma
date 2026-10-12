"""Run ownership leases (Epic 04).

A worker that claims a run stamps ``lease_expires_at`` and keeps pushing it forward from a
heartbeat while the run executes; a terminal transition clears it. A run that is still active
after its lease ran out has lost its worker, and the reaper (``app.services.reaper``) ends it.

Every lease timestamp is Postgres' clock, never the worker's, so clock skew between workers can
neither expire a live lease early nor keep a dead one alive.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta

import structlog
from sqlalchemy import DateTime, func

from app.core.metrics import run_lease_lost_total

logger = structlog.get_logger(__name__)


def db_now():
    """Postgres wall-clock time; ``now()`` would be frozen at the transaction's start."""
    return func.clock_timestamp(type_=DateTime(timezone=True))


def lease_deadline(seconds: float):
    return db_now() + timedelta(seconds=seconds)


async def heartbeat(
    renew: Callable[[], Awaitable[bool]],
    *,
    interval: float,
    on_lost: Callable[[], None] | None = None,
) -> None:
    """Call ``renew`` every ``interval`` until it reports the lease gone, then ``on_lost``.

    A renewal that raises (a DB blip) is retried on the next beat: the lease outlives several
    beats, and if it does lapse the reaper ends the run, which the next renewal then reports.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            renewed = await renew()
        except Exception:
            logger.warning("run_lease_renew_failed", exc_info=True)
            continue
        if not renewed:
            run_lease_lost_total.inc()
            logger.warning("run_lease_lost")
            if on_lost is not None:
                on_lost()
            return
