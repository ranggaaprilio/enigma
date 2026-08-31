from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from app.api.router import api_router
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.middleware import REQUEST_ID_HEADER, RequestContextMiddleware
from app.services.daytona_runtime import get_daytona_runtime


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
    yield
    await get_daytona_runtime().close()


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

    app.include_router(api_router)

    # Prometheus: auto HTTP request count/latency + a scrape endpoint at /metrics.
    Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)

    return app


app = create_app()
