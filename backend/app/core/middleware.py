import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"

logger = structlog.get_logger("app.request")


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a correlation id to every request and bind it into the log context.

    The id is taken from an incoming ``X-Request-ID`` header when present (so a proxy or
    caller can propagate one), otherwise a fresh one is generated. It is bound into
    structlog's contextvars — which ``merge_contextvars`` (already in the processor chain)
    folds into every log line for the duration of the request — stored on ``request.state``,
    and echoed back on the response header.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        incoming = request.headers.get(REQUEST_ID_HEADER)
        request_id = incoming.strip() if incoming and incoming.strip() else uuid.uuid4().hex
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
