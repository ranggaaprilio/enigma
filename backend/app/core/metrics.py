"""Custom Prometheus metrics.

HTTP request count/latency are provided automatically by
``prometheus-fastapi-instrumentator`` (wired in ``app.main``). These are the
application-specific counters we increment by hand from the relevant code paths.
"""

import secrets

from prometheus_client import Counter, Gauge

agent_runs_created_total = Counter(
    "agent_runs_created_total",
    "Number of agent runs created.",
)

rate_limit_rejections_total = Counter(
    "rate_limit_rejections_total",
    "Number of requests rejected by a rate limiter.",
    labelnames=("scope",),
)

run_capacity_rejections_total = Counter(
    "run_capacity_rejections_total",
    "Number of runs refused by a concurrency cap or queue backpressure.",
    labelnames=("scope",),
)

# Sampled at admission time (only while queue backpressure is enabled), not on every scrape.
run_queue_depth = Gauge(
    "run_queue_depth",
    "Runs enqueued but not yet claimed by a worker, as last seen at admission.",
)

# Epic 07: the preview proxy (its own process, scraped separately from the API).
preview_proxy_requests_total = Counter(
    "preview_proxy_requests_total",
    "Requests addressed to a preview host, by protocol.",
    labelnames=("protocol",),
)

preview_proxy_upstream_errors_total = Counter(
    "preview_proxy_upstream_errors_total",
    "Proxied requests that failed to reach the sandbox (Daytona error or upstream connect).",
    labelnames=("protocol",),
)

preview_proxy_active_websockets = Gauge(
    "preview_proxy_active_websockets",
    "WebSocket bridges currently open on this proxy replica.",
)


def metrics_token_authorized(authorization: str, expected: str | None) -> bool:
    """Whether an ``Authorization`` header may scrape ``/metrics``; open when no token is set."""
    if not expected:
        return True
    scheme, _, presented = authorization.partition(" ")
    # Constant-time compare so the token cannot be recovered by timing the endpoint. Bytes, not
    # str: `compare_digest` raises on a non-ASCII str, which would turn a bad token into a 500.
    return scheme.lower() == "bearer" and secrets.compare_digest(
        presented.encode("utf-8"), expected.encode("utf-8")
    )
