import re
import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

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
