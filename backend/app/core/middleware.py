import functools
import re
import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.websockets import WebSocket

from app.core.metrics import ws_connections

REQUEST_ID_HEADER = "X-Request-ID"
# What a caller-supplied id may look like. Anything else is replaced, never trimmed: the id
# lands in every log line, on run rows and in stream frames, so it must be short and inert.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

logger = structlog.get_logger("app.request")


def sanitize_request_id(raw: str | None) -> str:
    """Return ``raw`` when it is a well-formed request id, otherwise a fresh one."""
    candidate = raw.strip() if raw else ""
    if candidate and _REQUEST_ID_RE.match(candidate):
        return candidate
    return uuid.uuid4().hex


def current_request_id() -> str | None:
    """The request id bound to this task's log context, if any.

    Set by ``RequestContextMiddleware`` (HTTP), ``websocket_context`` (WebSockets) and the
    worker (from the run row), so a service can stamp it on what it creates without every
    route threading it through.
    """
    value = structlog.contextvars.get_contextvars().get("request_id")
    return value if isinstance(value, str) else None


WebSocketHandler = Callable[..., Awaitable[None]]


def websocket_context(kind: str) -> Callable[[WebSocketHandler], WebSocketHandler]:
    """Give a WebSocket handler a request id and count it while it is open.

    ``BaseHTTPMiddleware`` never sees WebSocket connections, so this does for them what
    ``RequestContextMiddleware`` does for requests: the id comes from the handshake's
    ``X-Request-ID`` (sanitised) or is generated, and is bound into the log context for the
    connection's lifetime, which is one asyncio task. The ``ws_connections`` gauge counts
    handlers in flight from handshake to close. Apply it *inside* ``router.websocket``.
    """

    def decorate(handler: WebSocketHandler) -> WebSocketHandler:
        @functools.wraps(handler)
        async def wrapper(websocket: WebSocket, **kwargs) -> None:
            request_id = sanitize_request_id(websocket.headers.get(REQUEST_ID_HEADER))
            structlog.contextvars.clear_contextvars()
            structlog.contextvars.bind_contextvars(request_id=request_id, ws_kind=kind)
            ws_connections.labels(kind=kind).inc()
            try:
                await handler(websocket=websocket, **kwargs)
            finally:
                ws_connections.labels(kind=kind).dec()
                structlog.contextvars.clear_contextvars()

        return wrapper

    return decorate


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a correlation id to every request and bind it into the log context.

    The id is taken from an incoming ``X-Request-ID`` header when present (so a proxy or
    caller can propagate one), otherwise a fresh one is generated. It is bound into
    structlog's contextvars — which ``merge_contextvars`` (already in the processor chain)
    folds into every log line for the duration of the request — stored on ``request.state``,
    and echoed back on the response header.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = sanitize_request_id(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start = time.perf_counter()
        logger.info("request.start", method=request.method, path=request.url.path)
        try:
            response = await call_next(request)
        except Exception:
            # An unhandled exception propagates past this middleware to Starlette's
            # ServerErrorMiddleware (outermost — see app.main's generic Exception handler),
            # which builds the actual client-facing response, so this branch never returns
            # one itself. It still attaches X-Request-ID: the handler in app.main reads it
            # back off `request.state`, which is the same underlying ASGI scope this
            # middleware set it on above.
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.exception(
                "request.error",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            raise
        else:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            logger.info(
                "request.finish",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=duration_ms,
            )
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            structlog.contextvars.clear_contextvars()
