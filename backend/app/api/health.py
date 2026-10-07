from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from app.core.config import get_settings
from app.core.lifecycle import lifecycle
from app.db.session import async_session_maker
from app.services.daytona_runtime import DaytonaRuntime
from app.services.redis import get_redis_client

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    database: str
    redis: str
    daytona: str


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


async def check_daytona() -> str:
    runtime = DaytonaRuntime(get_settings())
    try:
        return await runtime.health()
    finally:
        await runtime.close()


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse | JSONResponse:
    database_status = "ok"
    redis_status = "ok"
    daytona_status = "ok"

    try:
        database_status = await check_database()
    except Exception:
        database_status = "error"

    try:
        redis_status = await check_redis()
    except Exception:
        redis_status = "error"

    try:
        daytona_status = await check_daytona()
    except Exception:
        daytona_status = "error"

    status = (
        "ok"
        if database_status == "ok" and redis_status == "ok" and daytona_status == "ok"
        else "degraded"
    )
    payload = HealthResponse(
        status=status,
        database=database_status,
        redis=redis_status,
        daytona=daytona_status,
    )

    if status == "ok":
        return payload

    return JSONResponse(status_code=503, content=payload.model_dump())


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
