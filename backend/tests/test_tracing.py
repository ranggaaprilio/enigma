"""Epic 09: tracing is off by default, and a run's trace crosses API -> row -> worker."""

from __future__ import annotations

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import propagate, trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased

from app.core.middleware import RequestContextMiddleware
from app.core.tracing import (
    InjectOnlyPropagator,
    build_tracer_provider,
    configure_tracing,
    current_trace_id,
    current_traceparent,
    run_span,
    shutdown_tracing,
)
from app.models import AgentRun
from app.services.agents import AgentRuntime, AgentService
from app.services.runtime import InMemoryEventBus
from app.worker import run_agent_job
from tests.test_job_queue import FakeDispatcher, db_session, queue_settings, seed
from tests.test_worker_jobs import make_ctx

__all__ = ["db_session"]

EXPORTER = InMemorySpanExporter()


@pytest.fixture(scope="module", autouse=True)
def recording_provider():
    """One SDK provider for the module (OpenTelemetry keeps the first global provider)."""
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    provider.add_span_processor(SimpleSpanProcessor(EXPORTER))
    propagate.set_global_textmap(InjectOnlyPropagator())  # what configure_tracing installs
    yield provider


@pytest.fixture(autouse=True)
def clear_spans():
    EXPORTER.clear()
    yield
    structlog.contextvars.clear_contextvars()


def _span(name: str):
    return next(span for span in EXPORTER.get_finished_spans() if span.name == name)


def test_tracing_is_a_no_op_without_an_endpoint() -> None:
    assert configure_tracing(queue_settings(otel_exporter_otlp_endpoint=None), "enigma-api") is None
    shutdown_tracing(None)
    assert current_traceparent() is None
    assert current_trace_id() is None


def test_provider_carries_the_service_identity_and_a_parent_based_ratio_sampler() -> None:
    settings = queue_settings(otel_traces_sample_ratio=0.25, environment="local")
    provider = build_tracer_provider(
        settings, "enigma-worker", exporter=InMemorySpanExporter(), instance_id="worker-9"
    )
    attributes = provider.resource.attributes
    assert attributes["service.name"] == "enigma-worker"
    assert attributes["service.namespace"] == "enigma"
    assert attributes["service.instance.id"] == "worker-9"
    assert attributes["deployment.environment"] == "local"
    assert isinstance(provider.sampler, ParentBased)
    assert isinstance(provider.sampler._root, TraceIdRatioBased)
    assert provider.sampler._root.rate == 0.25
    provider.shutdown()


def test_run_span_is_a_child_of_the_request_that_created_the_run() -> None:
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("POST /runs") as request_span:
        traceparent = current_traceparent()
        assert traceparent and traceparent.startswith("00-")
        request_context = request_span.get_span_context()

    with run_span("agent_run", traceparent, run_id="r1", worker_id="w1", job_try=1):
        context = trace.get_current_span().get_span_context()
        assert context.trace_id == request_context.trace_id
        assert structlog.contextvars.get_contextvars()["trace_id"] == current_trace_id()
    assert "trace_id" not in structlog.contextvars.get_contextvars()

    worker_span = _span("agent_run")
    assert worker_span.parent.span_id == request_context.span_id
    assert worker_span.attributes["run_id"] == "r1"
    assert worker_span.kind is trace.SpanKind.CONSUMER


def test_run_span_without_a_traceparent_starts_its_own_trace() -> None:
    with run_span("command_run", None, run_id="r2"):
        assert current_trace_id() is not None
    assert _span("command_run").parent is None


async def test_the_trace_follows_the_run_through_the_row_to_the_worker(db_session):
    session, session_maker = db_session
    user, workspace = await seed(session)
    service = AgentService(
        session,
        session_maker,
        queue_settings(),
        runtime=AgentRuntime(bus=InMemoryEventBus()),
        dispatcher=FakeDispatcher(),
    )
    with trace.get_tracer("test").start_as_current_span("POST /runs") as request_span:
        run = await service.create_run(workspace.id, user.id, "hello")
        expected = current_traceparent()
    stored = await session.get(AgentRun, run.id)
    await session.refresh(stored)
    assert stored.traceparent == expected

    assert await run_agent_job(make_ctx(session_maker), str(run.id)) == "completed"

    worker_span = _span("agent_run")
    assert worker_span.context.trace_id == request_span.get_span_context().trace_id
    assert worker_span.parent.span_id == request_span.get_span_context().span_id
    assert worker_span.attributes["worker_id"] == "worker-a"


def test_request_context_middleware_joins_logs_and_spans() -> None:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    FastAPIInstrumentor.instrument_app(app)

    @app.get("/ping")
    async def ping() -> dict:
        return dict(structlog.contextvars.get_contextvars())

    forged = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    try:
        response = TestClient(app).get(
            "/ping", headers={"X-Request-ID": "req-traced", "traceparent": forged}
        )
    finally:
        FastAPIInstrumentor.uninstrument_app(app)

    body = response.json()
    assert body["request_id"] == "req-traced"
    server_span = next(
        span for span in EXPORTER.get_finished_spans() if span.kind is trace.SpanKind.SERVER
    )
    assert body["trace_id"] == format(server_span.context.trace_id, "032x")
    assert server_span.attributes["enigma.request_id"] == "req-traced"
    # A client-supplied traceparent neither picks our trace id nor becomes the parent.
    assert server_span.parent is None
    assert body["trace_id"] != "0af7651916cd43dd8448eb211c80319c"


def test_inbound_trace_context_is_never_extracted_but_ours_still_propagates() -> None:
    forged = {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}
    assert trace.get_current_span(propagate.extract(forged)).get_span_context().is_valid is False
    with trace.get_tracer("test").start_as_current_span("ours"):
        assert current_traceparent().startswith("00-")
