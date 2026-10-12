"""Epic 09: a worker's ``/metrics`` listener.

Workers run no HTTP server, yet they are where runs execute, so the run, LLM, lease and reaper
series live in their processes. This serves the default registry on ``METRICS_PORT`` under the
same bearer gate as the API's and the proxy's ``/metrics`` (``metrics_token_authorized``). It
is a thread-per-connection WSGI server on its own thread rather than something on arq's
event loop: a scrape never competes with a run for the loop, a stalled client never blocks the
next scrape, and nothing here touches signal handling, which arq owns. It is best-effort:
a port that cannot be bound costs the worker its metrics, not its jobs.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterable
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import structlog
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.core.config import Settings
from app.core.metrics import metrics_token_authorized
from app.services.fleet_metrics import ping_redis

logger = structlog.get_logger(__name__)


class _QuietHandler(WSGIRequestHandler):
    # A client that connects and never sends a request line is dropped after this long.
    timeout = 10

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 (wsgiref's name)
        return


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


def metrics_wsgi_app(token: str | None) -> Callable[[dict, Callable], Iterable[bytes]]:
    """``GET /metrics`` gated on ``token``; anything else is a 404."""

    def app(environ: dict, start_response: Callable) -> Iterable[bytes]:
        if environ.get("PATH_INFO") != "/metrics":
            start_response("404 Not Found", [("Content-Type", "application/json")])
            return [b'{"detail":"Not Found"}']
        if not metrics_token_authorized(environ.get("HTTP_AUTHORIZATION", ""), token):
            start_response(
                "401 Unauthorized",
                [("Content-Type", "application/json"), ("WWW-Authenticate", "Bearer")],
            )
            return [b'{"detail":"Not authenticated"}']
        body = generate_latest()
        start_response(
            "200 OK", [("Content-Type", CONTENT_TYPE_LATEST), ("Content-Length", str(len(body)))]
        )
        return [body]

    return app


class WorkerMetricsServer:
    """Serve ``/metrics`` on a daemon thread; ``port=0`` binds an ephemeral port (tests).

    Binding happens here, so a port already in use raises ``OSError`` from the constructor;
    ``start_if_possible`` turns that into a warning for the worker.
    """

    def __init__(self, settings: Settings, *, host: str = "0.0.0.0", port: int | None = None):
        self._server: WSGIServer = make_server(
            host,
            settings.metrics_port if port is None else port,
            metrics_wsgi_app(settings.metrics_token),
            server_class=_ThreadingWSGIServer,
            handler_class=_QuietHandler,
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="worker-metrics", daemon=True
        )

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> WorkerMetricsServer:
        self._thread.start()
        logger.info("worker_metrics_listening", port=self.port)
        return self

    def stop(self) -> None:
        """Blocking (up to a few seconds): call it off the event loop, ``asyncio.to_thread``."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def start_if_possible(settings: Settings) -> WorkerMetricsServer | None:
    """Start the listener, or log and return ``None`` when the port cannot be bound."""
    try:
        return WorkerMetricsServer(settings).start()
    except OSError:
        logger.warning("worker_metrics_unavailable", port=settings.metrics_port, exc_info=True)
        return None


async def run_redis_probe(settings: Settings) -> None:
    """Time a Redis PING from this worker every sample interval, until cancelled."""
    while True:
        try:
            await ping_redis("worker")
        except Exception:
            logger.warning("redis_ping_probe_failed", exc_info=True)
        await asyncio.sleep(settings.metrics_sample_interval_seconds)
