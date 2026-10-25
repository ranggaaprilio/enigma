"""Custom Prometheus metrics.

HTTP request count/latency are provided automatically by
``prometheus-fastapi-instrumentator`` (wired in ``app.main``). These are the
application-specific counters we increment by hand from the relevant code paths.
"""

import secrets
from collections.abc import Iterator
from datetime import datetime

from prometheus_client import Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily, Metric
from prometheus_client.registry import Collector
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

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

# Epic 09: WebSocket handlers in flight on this API replica, from handshake to close
# (``app.core.middleware.websocket_context``).
ws_connections = Gauge(
    "ws_connections",
    "WebSocket connections open on this replica, by stream kind.",
    labelnames=("kind",),
)

# Epic 09: run lifecycle, observed where runs execute (the workers in queue mode, the API
# inline). Labels are bounded: ``kind`` is agent|command, ``status`` a run status, ``reason``
# a stable ``error_reason`` code, provider/model the configured LLM. Never a tenant or run id.
_WAIT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)
_RUN_BUCKETS = (1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1200.0, 1800.0, 3600.0, 7500.0)
_LLM_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0)

run_queue_wait_seconds = Histogram(
    "run_queue_wait_seconds",
    "Time a run spent enqueued before a worker claimed it (queue mode only).",
    labelnames=("kind",),
    buckets=_WAIT_BUCKETS,
)

run_start_latency_seconds = Histogram(
    "run_start_latency_seconds",
    "Time from enqueue to the run's first content event or output chunk (queue mode only; a "
    "command that prints nothing records none).",
    labelnames=("kind",),
    buckets=_WAIT_BUCKETS,
)

run_duration_seconds = Histogram(
    "run_duration_seconds",
    "Wall time from a run's creation to its terminal status.",
    labelnames=("kind", "status"),
    buckets=_RUN_BUCKETS,
)

runs_finished_total = Counter(
    "runs_finished_total",
    "Runs that reached a terminal status, by status and error reason ('none' when not an error).",
    labelnames=("kind", "status", "reason"),
)

run_lease_lost_total = Counter(
    "run_lease_lost_total",
    "Heartbeats that found the run's lease no longer ours (the run was reaped or ended elsewhere).",
)

reaper_runs_ended_total = Counter(
    "reaper_runs_ended_total",
    "Active runs the reaper ended because their worker's lease expired.",
    labelnames=("kind",),
)

reaper_lost_jobs_total = Counter(
    "reaper_lost_jobs_total",
    "Queued runs the reaper ended because their job vanished from Redis before any claim.",
    labelnames=("kind",),
)

reaper_sweeps_total = Counter(
    "reaper_sweeps_total",
    "Reaper passes, by result.",
    labelnames=("result",),
)

llm_request_seconds = Histogram(
    "llm_request_seconds",
    "Wall time of a run's whole provider stream, first request to last event; it includes the "
    "tool and sandbox calls the model makes in between.",
    labelnames=("provider", "model"),
    buckets=_LLM_BUCKETS,
)

llm_time_to_first_token_seconds = Histogram(
    "llm_time_to_first_token_seconds",
    "Time from the provider request to its first content event (status events do not count).",
    labelnames=("provider", "model"),
    buckets=_LLM_BUCKETS,
)

llm_tokens_total = Counter(
    "llm_tokens_total",
    "Tokens consumed by finished agent runs, by provider, model and direction (input|output).",
    labelnames=("provider", "model", "kind"),
)

llm_cost_usd_total = Counter(
    "llm_cost_usd_total",
    "Estimated cost (USD) of finished agent runs, by provider and model (core.pricing).",
    labelnames=("provider", "model"),
)


# Epic 09: fleet-wide gauges sampled from Postgres by every API replica
# (``app.services.fleet_metrics``). Each replica reports the same numbers, so aggregate them
# with max() or avg() across replicas, never sum(). ``team`` is a team id, "none" (team-less
# workspaces) or "other" (teams beyond ``METRICS_TENANT_LABEL_LIMIT``); series are rebuilt
# on every sample, so a team that goes quiet disappears rather than lingering at zero.
_FLEET_TEAM_HELP = " Same on every API replica: aggregate with max(), not sum()."

fleet_queue_depth = Gauge(
    "fleet_queue_depth",
    "Runs enqueued but not yet claimed by a worker, by kind." + _FLEET_TEAM_HELP,
    labelnames=("kind",),
)

fleet_active_runs = Gauge(
    "fleet_active_runs",
    "Agent runs currently thinking/executing, by team." + _FLEET_TEAM_HELP,
    labelnames=("team",),
)

fleet_tokens_last_hour = Gauge(
    "fleet_tokens_last_hour",
    "Tokens used by agent runs that finished in the last hour, by team." + _FLEET_TEAM_HELP,
    labelnames=("team",),
)

fleet_cost_usd_last_hour = Gauge(
    "fleet_cost_usd_last_hour",
    "Estimated cost (USD) of agent runs that finished in the last hour, by team."
    + _FLEET_TEAM_HELP,
    labelnames=("team",),
)

fleet_sample_timestamp_seconds = Gauge(
    "fleet_sample_timestamp_seconds",
    "Unix time of this replica's last successful fleet sample; stale means the sampler is stuck.",
)

redis_ping_seconds = Histogram(
    "redis_ping_seconds",
    "Round-trip time of a Redis PING from this process, by role (api|worker).",
    labelnames=("role",),
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)

redis_ping_errors_total = Counter(
    "redis_ping_errors_total",
    "Redis PINGs that failed or timed out, by role.",
    labelnames=("role",),
)


class DbPoolCollector(Collector):
    """SQLAlchemy pool occupancy of this process's engine, read at scrape time.

    The pool's counters are plain attributes, so no database round trip happens on a scrape.
    Under PgBouncer (``DB_PGBOUNCER=true``) the engine has no pool to report, and this emits
    nothing rather than zeros.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    def collect(self) -> Iterator[Metric]:
        pool = self._engine.pool
        if isinstance(pool, NullPool):
            return
        yield GaugeMetricFamily(
            "db_pool_size", "Connections the pool keeps open (DB_POOL_SIZE).", value=pool.size()
        )
        yield GaugeMetricFamily(
            "db_pool_checked_out",
            "Connections currently in use by this process.",
            value=pool.checkedout(),
        )
        yield GaugeMetricFamily(
            "db_pool_overflow",
            "Connections open beyond the pool size (bounded by DB_MAX_OVERFLOW).",
            value=max(pool.overflow(), 0),
        )


def _seconds_between(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return max((end - start).total_seconds(), 0.0)


def observe_queue_wait(kind: str, enqueued_at: datetime | None, claimed_at: datetime | None):
    if (seconds := _seconds_between(enqueued_at, claimed_at)) is not None:
        run_queue_wait_seconds.labels(kind=kind).observe(seconds)


def observe_run_start(kind: str, enqueued_at: datetime | None, first_at: datetime | None):
    if (seconds := _seconds_between(enqueued_at, first_at)) is not None:
        run_start_latency_seconds.labels(kind=kind).observe(seconds)


def observe_run_finished(
    kind: str,
    status: str,
    reason: str | None,
    started_at: datetime | None,
    completed_at: datetime | None,
) -> None:
    runs_finished_total.labels(kind=kind, status=status, reason=reason or "none").inc()
    if (seconds := _seconds_between(started_at, completed_at)) is not None:
        run_duration_seconds.labels(kind=kind, status=status).observe(seconds)


def observe_llm_usage(
    provider: str | None,
    model: str | None,
    *,
    input_tokens: int | None,
    output_tokens: int | None,
    cost_usd: float | None,
) -> None:
    labels = {"provider": provider or "unknown", "model": model or "unknown"}
    if input_tokens:
        llm_tokens_total.labels(kind="input", **labels).inc(input_tokens)
    if output_tokens:
        llm_tokens_total.labels(kind="output", **labels).inc(output_tokens)
    if cost_usd:
        llm_cost_usd_total.labels(**labels).inc(cost_usd)

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
