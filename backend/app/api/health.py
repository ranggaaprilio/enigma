import asyncio
import time
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.core.config import get_settings
from app.core.lifecycle import lifecycle
from app.core.metrics import metrics_token_authorized
from app.db.session import async_session_maker
from app.services.redis import get_redis_client
from app.services.sandbox import get_runtime_by_name
from app.services.sandbox.availability import (
    HEALTH_TIMEOUT_SECONDS,
    PROVIDERS,
    runtime_availability,
)

router = APIRouter(tags=["health"])

# /health is unauthenticated: provider probes (a Daytona API call, Docker daemon calls) are
# shared by every caller for this long, one in flight at a time, so a flood of /health never
# becomes a flood of upstream calls (Epic 06 security review).
RUNTIME_HEALTH_TTL_SECONDS = 5.0


class RuntimeHealth(BaseModel):
    """One provider, at the exposure level of the rest of /health: configured yes/no, a
    status and a generic reason -- never a socket path, URL or version (Epic 06)."""

    configured: bool
    status: str  # ok | error | unconfigured | disabled (SANDBOX_RUNTIMES_ALLOWED)
    reason: str | None = None
    # Docker only: this installation's sandboxes on the daemon, and when this process last
    # ran the upkeep pass (None where it does not run it, e.g. the API in queue mode). Shown
    # only to a METRICS_TOKEN bearer when a token is set: operational detail, not status.
    managed_containers: int | None = None
    upkeep_last_pass_at: datetime | None = None


class HealthResponse(BaseModel):
    status: str
    database: str
    redis: str
    # Kept for existing monitors; the same value as runtimes["daytona"].status.
    daytona: str
    sandbox_runtime_default: str
    runtimes: dict[str, RuntimeHealth]


class LiveResponse(BaseModel):
    status: str


class ReadyResponse(BaseModel):
    status: str
    database: str
    redis: str


async def check_database() -> str:
    async with async_session_maker() as session:
        await session.execute(text("SELECT 1"))
    return "ok"


async def check_redis() -> str:
    redis = get_redis_client()
    try:
        await redis.ping()
    finally:
        # Always close: the caller swallows the exception and probes again, so an
        # unclosed client on the failure path leaks a connection pool per failed probe.
        await redis.aclose()
    return "ok"


async def count_managed_containers() -> int | None:
    runtime = get_runtime_by_name("docker")
    try:
        found = await asyncio.wait_for(runtime.managed_sandboxes(), HEALTH_TIMEOUT_SECONDS)
    except Exception:
        return None
    return len(found)


def upkeep_last_pass_at() -> datetime | None:
    return getattr(get_runtime_by_name("docker"), "last_upkeep_at", None)


async def check_runtime(provider: str) -> RuntimeHealth:
    found = await runtime_availability(provider)
    entry = RuntimeHealth(configured=found.configured, status=found.status, reason=found.reason)
    if provider == "docker" and found.available:
        entry.managed_containers = await count_managed_containers()
        entry.upkeep_last_pass_at = upkeep_last_pass_at()
    return entry


async def check_runtimes() -> dict[str, RuntimeHealth]:
    checked = await asyncio.gather(*(check_runtime(provider) for provider in PROVIDERS))
    return dict(zip(PROVIDERS, checked, strict=True))


async def shared_runtimes(request: Request) -> dict[str, RuntimeHealth]:
    """``check_runtimes``, shared through ``app.state`` for ``RUNTIME_HEALTH_TTL_SECONDS``."""
    state = request.app.state
    cached = getattr(state, "runtime_health", None)
    now = time.monotonic()
    if cached is None or (cached[1].done() and cached[0] <= now):
        cached = (now + RUNTIME_HEALTH_TTL_SECONDS, asyncio.ensure_future(check_runtimes()))
        state.runtime_health = cached
    # Shielded: a caller that disconnects must not cancel the probe the others wait on.
    return await asyncio.shield(cached[1])


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse | JSONResponse:
    settings = get_settings()
    database_status = "ok"
    redis_status = "ok"

    try:
        database_status = await check_database()
    except Exception:
        database_status = "error"

    try:
        redis_status = await check_redis()
    except Exception:
        redis_status = "error"

    runtimes = await shared_runtimes(request)
    if not metrics_token_authorized(
        request.headers.get("authorization", ""), settings.metrics_token
    ):
        runtimes = {
            name: entry.model_copy(update={"managed_containers": None, "upkeep_last_pass_at": None})
            for name, entry in runtimes.items()
        }
    # An unconfigured or disabled provider is not a fault (a Docker-only install has no
    # Daytona key), but a configured, enabled one that is down is, and so is having nowhere
    # to put a workspace.
    configured = [
        entry for entry in runtimes.values() if entry.configured and entry.status != "disabled"
    ]
    runtimes_ok = bool(configured) and all(entry.status == "ok" for entry in configured)

    status = (
        "ok" if database_status == "ok" and redis_status == "ok" and runtimes_ok else "degraded"
    )
    payload = HealthResponse(
        status=status,
        database=database_status,
        redis=redis_status,
        daytona=runtimes["daytona"].status,
        sandbox_runtime_default=settings.sandbox_runtime_default,
        runtimes=runtimes,
    )

    if status == "ok":
        return payload

    return JSONResponse(status_code=503, content=payload.model_dump(mode="json"))


@router.get("/live", response_model=LiveResponse)
async def live() -> LiveResponse:
    """Liveness probe: is the process up and serving its event loop?

    Deliberately dependency-free -- a Postgres or Redis outage must not get the instance
    killed and restarted. Use ``/ready`` to gate traffic and ``/health`` for diagnostics.
    """
    return LiveResponse(status="ok")


@router.get("/ready", response_model=ReadyResponse)
async def ready() -> ReadyResponse | JSONResponse:
    """Readiness probe: can this instance serve API traffic?

    Checks the hard dependencies required to serve requests — Postgres and Redis. Daytona
    is deliberately excluded: it is an external sandbox provider whose outage should not pull
    the instance out of the load balancer.

    A draining instance (shutting down after SIGTERM) answers 503 without probing, so the load
    balancer stops routing to it while in-flight work finishes.
    """
    if lifecycle.draining:
        return JSONResponse(
            status_code=503,
            content=ReadyResponse(
                status="draining", database="skipped", redis="skipped"
            ).model_dump(),
        )

    database_status = "ok"
    redis_status = "ok"

    try:
        database_status = await check_database()
    except Exception:
        database_status = "error"

    try:
        redis_status = await check_redis()
    except Exception:
        redis_status = "error"

    status = "ready" if database_status == "ok" and redis_status == "ok" else "not_ready"
    payload = ReadyResponse(status=status, database=database_status, redis=redis_status)

    if status == "ready":
        return payload

    return JSONResponse(status_code=503, content=payload.model_dump())
