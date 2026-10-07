import asyncio
import secrets
from contextlib import asynccontextmanager

import structlog
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from app.api.router import api_router
from app.core.config import get_settings
from app.core.lifecycle import (
    ServiceDrainingError,
    drain_in_flight_runs,
    install_sigterm_drain,
    lifecycle,
    service_draining_handler,
)
from app.core.logging import configure_logging
from app.core.middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from app.db.session import async_session_maker, engine
from app.services.daytona_runtime import get_daytona_runtime
from app.services.reconciliation import reconcile_interrupted_runs
from app.services.runtime import run_registry

logger = structlog.get_logger(__name__)


async def unhandled_exception_handler(request: Request, _exc: Exception) -> JSONResponse:
    """Turn an unhandled route-handler exception into a plain 500 response.

    Registering a handler for the bare ``Exception`` class makes Starlette use it as
    ``ServerErrorMiddleware``'s handler — that middleware sits outermost, above
    ``RequestContextMiddleware``, so the exception still propagates past it (logged there)
    before this handler builds the actual response. Attach X-Request-ID here from
    ``request.state`` (set by ``RequestContextMiddleware`` on the same underlying ASGI scope)
    so error responses carry the same correlation id as successful ones.
    """
    response = JSONResponse(status_code=500, content={"detail": "Internal Server Error"})
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        response.headers[REQUEST_ID_HEADER] = request_id
    return response


@asynccontextmanager
async def lifespan(_app: FastAPI):
    settings = get_settings()
    # Runs left active by a previous process (crash, deploy) have no owner any more; close
    # them out before serving so their workspaces are not blocked. Fails startup if Postgres
    # is unreachable, which /ready would report as not ready anyway.
    await reconcile_interrupted_runs(async_session_maker)
    restore_sigterm = install_sigterm_drain(
        asyncio.get_running_loop(), settings.shutdown_drain_delay_seconds
    )

    yield

    lifecycle.begin_drain()
    try:
        # Only the runs this process owns: an overlapping replica's live runs are not ours.
        owned_run_ids = run_registry.active_run_ids()
        await drain_in_flight_runs(run_registry, settings.shutdown_run_grace_seconds)
        await reconcile_interrupted_runs(async_session_maker, run_ids=owned_run_ids)
    except Exception:
        # Whatever is left active is swept by the next startup; the pools must still close.
        logger.exception("shutdown_drain_failed")
    finally:
        await get_daytona_runtime().close()
        # Redis needs no step here: every caller opens and closes its own client.
        await engine.dispose()
        restore_sigterm()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging()

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.backend_cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # Added last so it runs outermost — every request gets a correlation id bound before
    # any other middleware or route handler executes.
    app.add_middleware(RequestContextMiddleware)

    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.add_exception_handler(ServiceDrainingError, service_draining_handler)

    app.include_router(api_router)

    # Prometheus: auto HTTP request count/latency + a scrape endpoint at /metrics. The
    # endpoint exposes per-route traffic and workspace-level counters, so it is gated on a
    # bearer token whenever one is configured (mandatory outside a local environment —
    # `Settings.enforce_secure_defaults` refuses to boot without it).
    Instrumentator().instrument(app).expose(
        app,
        endpoint="/metrics",
        include_in_schema=False,
        dependencies=[Depends(require_metrics_token)],
    )

    return app


async def require_metrics_token(request: Request) -> None:
    """Authorise a /metrics scrape against ``settings.metrics_token``."""
    expected = get_settings().metrics_token
    if not expected:
        return
    header = request.headers.get("authorization", "")
    scheme, _, presented = header.partition(" ")
    # Constant-time compare so the token cannot be recovered by timing the endpoint.
    if scheme.lower() != "bearer" or not secrets.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )


app = create_app()
