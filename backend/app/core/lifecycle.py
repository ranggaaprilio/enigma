"""Process lifecycle: the draining flag, SIGTERM hand-off and in-flight run drain.

Shutdown sequence (SIGTERM from the orchestrator):

1. ``install_sigterm_drain``'s handler flips ``lifecycle.draining``. ``/ready`` now answers 503
   and new agent/command runs are refused with 503 + ``Retry-After``, while the listener stays
   open for ``shutdown_drain_delay_seconds`` so the load balancer notices and stops routing.
2. The signal is then handed to uvicorn, which closes the listener, closes open WebSockets
   (clients reconnect) and runs the lifespan shutdown.
3. The lifespan waits up to ``shutdown_run_grace_seconds`` for in-flight runs, cancels the rest,
   and sweeps their rows to ``error``/``interrupted_by_restart`` before closing pools.
"""

import asyncio
import os
import signal
import threading
from collections.abc import Callable
from types import FrameType

import structlog
from fastapi import Request
from fastapi.responses import JSONResponse

from app.services.runtime import RunRegistry

logger = structlog.get_logger(__name__)

RETRY_AFTER_SECONDS = 5
# How long cancelled runs get to unwind (finish their except/finally blocks) after the grace.
CANCEL_SETTLE_SECONDS = 5


class ServiceDrainingError(Exception):
    """Raised when new work is requested from an instance that is shutting down."""


class AppLifecycle:
    def __init__(self) -> None:
        self.draining = False

    def begin_drain(self) -> None:
        if not self.draining:
            logger.info("app_draining")
        self.draining = True

    def ensure_accepting_work(self) -> None:
        if self.draining:
            raise ServiceDrainingError


lifecycle = AppLifecycle()


async def service_draining_handler(_request: Request, _exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": "The server is restarting. Please retry in a few seconds."},
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )


def install_sigterm_drain(
    loop: asyncio.AbstractEventLoop,
    delay_seconds: float,
    state: AppLifecycle = lifecycle,
) -> Callable[[], None]:
    """Interpose on SIGTERM: drain first, hand the signal to the server ``delay_seconds`` later.

    uvicorn closes its listening socket the moment it sees SIGTERM, so without this delay a
    draining ``/ready`` could never be observed. A second SIGTERM skips the remaining delay.
    Returns a callable restoring the previous handler (a no-op off the main thread, where
    signal handlers cannot be installed -- e.g. an app driven by a test client).
    """
    if threading.current_thread() is not threading.main_thread():
        return lambda: None

    previous = signal.getsignal(signal.SIGTERM)
    pending: list[asyncio.TimerHandle] = []

    def forward(sig: int, frame: FrameType | None) -> None:
        for handle in pending:
            handle.cancel()
        pending.clear()
        if callable(previous):
            previous(sig, frame)
        elif previous != signal.SIG_IGN:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGTERM)

    def start_delay(sig: int, frame: FrameType | None) -> None:
        logger.info("app_draining", delay_seconds=delay_seconds)
        pending.append(loop.call_later(delay_seconds, forward, sig, frame))

    def handler(sig: int, frame: FrameType | None) -> None:
        # Runs between bytecodes, possibly in the middle of the loop's own bookkeeping: only
        # flip the flag (so /ready answers 503 at once) and hand everything else -- logging,
        # timers, forwarding -- to the loop via its thread-safe wake-up.
        if state.draining:
            loop.call_soon_threadsafe(forward, sig, frame)
            return
        state.draining = True
        loop.call_soon_threadsafe(start_delay, sig, frame)

    signal.signal(signal.SIGTERM, handler)

    def restore() -> None:
        for handle in pending:
            handle.cancel()
        signal.signal(signal.SIGTERM, previous)

    return restore


async def drain_in_flight_runs(registry: RunRegistry, grace_seconds: float) -> int:
    """Wait up to ``grace_seconds`` for in-flight runs, then cancel the rest.

    Returns how many runs had to be cancelled. Cancelled runs keep their active status; the
    caller's reconciliation sweep records them as interrupted.
    """
    tasks = registry.active_tasks()
    if not tasks:
        return 0
    logger.info("draining_in_flight_runs", count=len(tasks), grace_seconds=grace_seconds)
    _done, pending = await asyncio.wait(tasks, timeout=grace_seconds)
    for task in pending:
        task.cancel()
    if pending:
        _done, stuck = await asyncio.wait(pending, timeout=CANCEL_SETTLE_SECONDS)
        logger.warning("in_flight_runs_cancelled", count=len(pending), still_unwinding=len(stuck))
    return len(pending)
