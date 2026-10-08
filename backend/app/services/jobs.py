"""Hand-off of runs from the API to the worker fleet (Epic 02, ``RUN_EXECUTION_MODE=queue``).

A job carries only ids -- never secrets or run content. The worker reloads everything from
Postgres, which stays the source of truth; the queue is just the trigger. Job ids are derived
from the run id, so enqueueing the same run twice is a no-op on the arq side, and the worker's
conditional claim (``claimed_at IS NULL``) makes redelivery safe on ours.

Jobs are JSON, not arq's default pickle: unpickling a queue entry would turn write access to
Redis into code execution on a worker that holds every decryption key.
"""

import asyncio
import json
from typing import Protocol
from uuid import UUID

from arq.connections import ArqRedis, RedisSettings, create_pool
from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.config import Settings, get_settings
from app.core.lifecycle import RETRY_AFTER_SECONDS

AGENT_RUN_JOB = "run_agent_job"
COMMAND_RUN_JOB = "run_command_job"
SPEC_FINALIZE_JOB = "finalize_spec_phase_job"

# Stable `error_reason` codes for runs the queue path ends.
ENQUEUE_FAILED = "enqueue_failed"
WORKER_LOST = "worker_lost"
DEAD_LETTER = "dead_letter"
JOB_TIMEOUT = "job_timeout"
WORKER_ERROR = "worker_error"


def serialize_job_payload(data: dict) -> bytes:
    return json.dumps(data, default=str).encode()


def deserialize_job_payload(raw: bytes) -> dict:
    # Anything that is not JSON (e.g. a pickle) raises ValueError and is never executed.
    return json.loads(raw)


class RunQueueUnavailableError(Exception):
    """The run row was written but the job could not be handed to the queue."""


class RunDispatcher(Protocol):
    async def enqueue_agent_run(self, run_id: UUID) -> None: ...

    async def enqueue_command_run(self, run_id: UUID) -> None: ...

    async def enqueue_spec_finalize(self, phase_id: UUID, run_id: UUID) -> None: ...


class ArqDispatcher:
    def __init__(self, settings: Settings, pool: ArqRedis | None = None) -> None:
        self.settings = settings
        self._pool = pool
        self._pool_lock = asyncio.Lock()

    async def _get_pool(self) -> ArqRedis:
        async with self._pool_lock:
            if self._pool is None:
                self._pool = await create_pool(
                    RedisSettings.from_dsn(self.settings.redis_url),
                    default_queue_name=self.settings.run_queue_name,
                    job_serializer=serialize_job_payload,
                    job_deserializer=deserialize_job_payload,
                )
            return self._pool

    async def _enqueue(self, function: str, *args: str, job_id: str) -> None:
        pool = await self._get_pool()
        await pool.enqueue_job(function, *args, _job_id=job_id)

    async def enqueue_agent_run(self, run_id: UUID) -> None:
        await self._enqueue(AGENT_RUN_JOB, str(run_id), job_id=f"agent_run:{run_id}")

    async def enqueue_command_run(self, run_id: UUID) -> None:
        await self._enqueue(COMMAND_RUN_JOB, str(run_id), job_id=f"command_run:{run_id}")

    async def enqueue_spec_finalize(self, phase_id: UUID, run_id: UUID) -> None:
        await self._enqueue(
            SPEC_FINALIZE_JOB,
            str(phase_id),
            str(run_id),
            job_id=f"spec_finalize:{phase_id}:{run_id}",
        )

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.aclose()
            self._pool = None


# One dispatcher per process (a connection pool holder, not a registry of in-flight work).
_dispatcher: RunDispatcher | None = None


def get_run_dispatcher() -> RunDispatcher:
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = ArqDispatcher(get_settings())
    return _dispatcher


def set_run_dispatcher(dispatcher: RunDispatcher | None) -> None:
    global _dispatcher
    _dispatcher = dispatcher


async def close_run_dispatcher() -> None:
    if isinstance(_dispatcher, ArqDispatcher):
        await _dispatcher.aclose()
    set_run_dispatcher(None)


async def run_queue_unavailable_handler(_request: Request, _exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={"detail": "The run queue is unavailable. Please retry in a few seconds."},
        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
    )
