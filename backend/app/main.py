import asyncio
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
from app.core.metrics import metrics_token_authorized
from app.core.middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from app.core.tracing import UNTRACED_URLS, configure_tracing, shutdown_tracing
from app.db.session import async_session_maker, engine
from app.services.capacity import RunCapacityExceededError, run_capacity_exceeded_handler
from app.services.fleet_metrics import start_fleet_sampler
from app.services.jobs import (
    RunQueueUnavailableError,
    close_run_dispatcher,
    run_queue_unavailable_handler,
)
from app.services.reconciliation import reconcile_interrupted_runs
from app.services.retention import start_retention
from app.services.runtime import RedisEventBus, event_bus, run_registry
from app.services.sandbox import close_all

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
    # is unreachable, which /ready would report as not ready anyway. In queue mode the API owns
    # no runs -- active rows belong to live workers, which reconcile their own (app.worker).
    if settings.run_execution_mode == "inline":
        await reconcile_interrupted_runs(async_session_maker)
    # Retention (Epic 05) runs where runs do: here in inline mode, on the workers in queue mode.
    retention = (
        start_retention(async_session_maker, settings)
        if settings.run_execution_mode == "inline"
        else None
    )
    # Epic 09: fleet-wide gauges (queue depth, active runs and spend by team) on /metrics.
    fleet_sampler = start_fleet_sampler(async_session_maker, settings)
    restore_sigterm = install_sigterm_drain(
        asyncio.get_running_loop(), settings.shutdown_drain_delay_seconds
    )

    yield

    lifecycle.begin_drain()
    fleet_sampler.cancel()
    await asyncio.gather(fleet_sampler, return_exceptions=True)
    if retention is not None:
        retention.cancel()
        await asyncio.gather(retention, return_exceptions=True)
    try:
        # Only the runs this process owns: an overlapping replica's live runs are not ours.
        owned_run_ids = run_registry.active_run_ids()
        await drain_in_flight_runs(run_registry, settings.shutdown_run_grace_seconds)
        await reconcile_interrupted_runs(async_session_maker, run_ids=owned_run_ids)
    except Exception:
        # Whatever is left active is swept by the next startup; the pools must still close.
        logger.exception("shutdown_drain_failed")
    finally:
        await close_all()
        # The queue-mode Redis clients (dispatcher pool, event bus); every other caller opens
        # and closes its own client.
        await close_run_dispatcher()
        if isinstance(event_bus, RedisEventBus):
            await event_bus.aclose()
        await engine.dispose()
        restore_sigterm()
        await asyncio.to_thread(shutdown_tracing, getattr(_app.state, "tracing", None))


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging()

    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
    # Epic 09: before any middleware, so the provider exists when the first span starts.
    app.state.tracing = configure_tracing(settings, "enigma-api")

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
    if app.state.tracing is not None:
        # Outermost of all (added after the request-context middleware), so the server span
        # is current when the request id is bound and stamped on it.
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls=UNTRACED_URLS)

    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.add_exception_handler(ServiceDrainingError, service_draining_handler)
    app.add_exception_handler(RunQueueUnavailableError, run_queue_unavailable_handler)
    app.add_exception_handler(RunCapacityExceededError, run_capacity_exceeded_handler)

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
    if not metrics_token_authorized(
        request.headers.get("authorization", ""), get_settings().metrics_token
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )


app = create_app()
