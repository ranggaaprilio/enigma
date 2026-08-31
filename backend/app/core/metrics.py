"""Custom Prometheus metrics.

HTTP request count/latency are provided automatically by
``prometheus-fastapi-instrumentator`` (wired in ``app.main``). These are the
application-specific counters we increment by hand from the relevant code paths.
"""

from prometheus_client import Counter

agent_runs_created_total = Counter(
    "agent_runs_created_total",
    "Number of agent runs created.",
)

rate_limit_rejections_total = Counter(
    "rate_limit_rejections_total",
    "Number of requests rejected by a rate limiter.",
    labelnames=("scope",),
)
