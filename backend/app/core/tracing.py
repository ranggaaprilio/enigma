"""Epic 09: distributed tracing (OpenTelemetry), off unless an OTLP endpoint is configured.

``configure_tracing`` installs an SDK ``TracerProvider`` exporting over OTLP/HTTP and
instruments FastAPI (in ``app.main``), SQLAlchemy, redis, httpx and aiohttp (the Daytona SDK).
It runs before anything else sets a global provider: the Daytona SDK and Laminar each install
one only when their own env flags are set, and OpenTelemetry keeps the first. With no endpoint
nothing is installed and every helper below is a cheap no-op on the API's non-recording spans.

A run's trace crosses processes through the ``traceparent`` column on its row (migration
0035): ``current_traceparent()`` captures it in ``create_run`` under the request's server span,
and the worker's ``run_span`` starts a CONSUMER span as that span's child.

Incoming trace context is never trusted: the global propagator only *injects*, so a
``traceparent`` header from a browser or an upstream proxy cannot choose our trace ids or force
sampling, and every API server span is a root sampled by our own ratio. The worker extracts
the run row's context explicitly, which the API wrote.
"""

from __future__ import annotations

import socket
import typing
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlsplit

import structlog
from opentelemetry import context as otel_context
from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import SpanKind
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from app.core.config import Settings

logger = structlog.get_logger(__name__)

TRACEPARENT_HEADER = "traceparent"
# Health/metrics endpoints are scraped constantly and tell nothing about a request's story.
UNTRACED_URLS = "live,ready,health,metrics"

# The W3C format itself, used only where *we* wrote the carrier (the run row).
_W3C = TraceContextTextMapPropagator()


class InjectOnlyPropagator(TraceContextTextMapPropagator):
    """W3C ``traceparent`` out, nothing in: inbound headers never become a span's parent."""

    def extract(
        self,
        carrier: typing.Any,
        context: otel_context.Context | None = None,
        getter: typing.Any = None,
    ) -> otel_context.Context:
        return context if context is not None else otel_context.get_current()


def build_tracer_provider(
    settings: Settings,
    service_name: str,
    *,
    exporter: SpanExporter,
    instance_id: str | None = None,
) -> TracerProvider:
    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "enigma",
            "service.instance.id": instance_id or socket.gethostname(),
            "deployment.environment": settings.environment,
        }
    )
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(TraceIdRatioBased(settings.otel_traces_sample_ratio)),
    )
    # A short export timeout: the flush at shutdown must not eat into the drain grace.
    provider.add_span_processor(BatchSpanProcessor(exporter, export_timeout_millis=5_000))
    return provider


def configure_tracing(
    settings: Settings, service_name: str, *, instance_id: str | None = None
) -> TracerProvider | None:
    """Install tracing for this process, or return ``None`` when no endpoint is configured."""
    endpoint = settings.otel_exporter_otlp_endpoint
    if not endpoint:
        return None
    # Imported lazily: the OTLP exporter pulls in protobuf at import time, which an
    # untraced process never needs.
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.aiohttp_client import AioHttpClientInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor
    from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

    from app.db.session import engine

    exporter = OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces")
    provider = build_tracer_provider(
        settings, service_name, exporter=exporter, instance_id=instance_id
    )
    trace.set_tracer_provider(provider)
    if trace.get_tracer_provider() is not provider:
        # Something (Laminar, the Daytona SDK) got there first; our spans would go nowhere.
        logger.warning("tracing_provider_already_set", service=service_name)
    propagate.set_global_textmap(InjectOnlyPropagator())
    # The async engine wraps a sync one; that is what the instrumentation hooks. No SQL
    # commenter: behind PgBouncer the comment only defeats statement caching.
    SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine, enable_commenter=False)
    RedisInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()
    AioHttpClientInstrumentor().instrument()
    # The host only: an endpoint URL may carry credentials.
    logger.info("tracing_configured", service=service_name, collector=urlsplit(endpoint).hostname)
    return provider


def shutdown_tracing(provider: TracerProvider | None) -> None:
    """Flush what the batch processor still holds; a no-op when tracing is off."""
    if provider is not None:
        provider.shutdown()


def current_traceparent() -> str | None:
    """The W3C ``traceparent`` of the current span, or ``None`` outside a recorded trace."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier.get(TRACEPARENT_HEADER)


def current_trace_id() -> str | None:
    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return format(span_context.trace_id, "032x")


def bind_trace_context(request_id: str | None = None) -> None:
    """Fold the current trace id into the log context and stamp the request id on the span."""
    span = trace.get_current_span()
    if request_id and span.is_recording():
        span.set_attribute("enigma.request_id", request_id)
    if (trace_id := current_trace_id()) is not None:
        structlog.contextvars.bind_contextvars(trace_id=trace_id)


@contextmanager
def run_span(name: str, traceparent: str | None, **attributes: str | int) -> Iterator[None]:
    """A CONSUMER span for executing a run, as a child of the request that created it.

    ``traceparent`` is the run row's; without one the span starts a new trace. The trace id
    is bound into the log context for the span's duration (``trace_id``), so worker log lines
    and spans join up.
    """
    parent = (
        _W3C.extract({TRACEPARENT_HEADER: traceparent})
        if traceparent
        else otel_context.get_current()
    )
    tracer = trace.get_tracer("enigma.worker")
    with tracer.start_as_current_span(
        name, context=parent, kind=SpanKind.CONSUMER, attributes=attributes
    ):
        if (trace_id := current_trace_id()) is not None:
            structlog.contextvars.bind_contextvars(trace_id=trace_id)
        try:
            yield
        finally:
            structlog.contextvars.unbind_contextvars("trace_id")
