"""Custom Prometheus metrics.

HTTP request count/latency are provided automatically by
``prometheus-fastapi-instrumentator`` (wired in ``app.main``). These are the
application-specific counters we increment by hand from the relevant code paths.
"""

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
