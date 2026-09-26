"""OpenTelemetry tracing: one provider per process, exported over OTLP/HTTP and to Langfuse.

Spans are created by hand rather than by the `opentelemetry-instrumentation-*` packages. The
pipeline stages are the spans that matter here and they are already explicit in `Trace`; the one
piece worth automating is SQL, which `instrument_engine` covers through SQLAlchemy's own events.
"""

import contextvars
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Optional, TypeVar

from opentelemetry import context as otel_context
from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import SpanKind, Status, StatusCode
from sqlalchemy import event
from sqlalchemy.engine import Engine

from src.core import llm_traces
from src.core.config import SETTINGS

_T = TypeVar("_T")

_provider: Optional[TracerProvider] = None
_SPAN_KEY = "sda_span"


class TracedPool(ThreadPoolExecutor):
    """A pool whose workers inherit the submitting thread's context.

    A bare worker thread starts with an empty context, so the spans it opens become roots of
    their own traces: the parallel dense and lexical searches would vanish from the waterfall.
    """

    def submit(self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> "Future[_T]":
        return super().submit(contextvars.copy_context().run, fn, *args, **kwargs)


def setup(role: str) -> None:
    global _provider
    if _provider is not None:
        return
    cfg = SETTINGS.observability
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": cfg.service_name,
                "service.version": cfg.service_version,
                "deployment.environment": cfg.environment,
                "sda.role": role,
            }
        ),
        # ParentBased: a sampled request keeps every downstream span, so a waterfall is never
        # half-recorded.
        sampler=ParentBased(TraceIdRatioBased(cfg.trace_sample_ratio)),
    )
    if cfg.otlp_endpoint:
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(endpoint=f"{cfg.otlp_endpoint.rstrip('/')}/v1/traces")
            )
        )
    otel_trace.set_tracer_provider(provider)
    _provider = provider
    llm_traces.attach(provider)


def provider() -> TracerProvider:
    if _provider is None:
        raise RuntimeError("Tracing is not set up; call src.core.observability.setup first.")
    return _provider


def tracer() -> otel_trace.Tracer:
    return otel_trace.get_tracer("sda")


def shutdown() -> None:
    global _provider
    llm_traces.shutdown()
    if _provider is not None:
        _provider.shutdown()
        _provider = None


def instrument_engine(engine: Engine) -> None:
    """A span per statement, so a slow pgvector or tsvector query is visible inside its stage."""
    if not SETTINGS.observability.db_spans:
        return

    def before(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> None:
        span = tracer().start_span(_statement_name(statement), kind=SpanKind.CLIENT)
        span.set_attribute("db.system", engine.dialect.name)
        span.set_attribute("db.statement", statement[:2000])
        conn.info.setdefault(_SPAN_KEY, []).append(
            (span, otel_context.attach(otel_trace.set_span_in_context(span)))
        )

    def after(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool) -> None:
        _end(conn)

    def failed(context: Any) -> None:
        if context.connection is not None:
            _end(context.connection, error=context.original_exception)

    event.listen(engine, "before_cursor_execute", before)
    event.listen(engine, "after_cursor_execute", after)
    event.listen(engine, "handle_error", failed)


def _end(conn: Any, error: Optional[BaseException] = None) -> None:
    pending = conn.info.get(_SPAN_KEY)
    if not pending:
        return
    span, token = pending.pop()
    if error is not None:
        span.set_status(Status(StatusCode.ERROR, str(error)))
        span.record_exception(error)
    otel_context.detach(token)
    span.end()


def _statement_name(statement: str) -> str:
    """Verb plus table keeps span names low-cardinality; the statement itself is an attribute."""
    words = statement.strip().split()
    verb = words[0].lower() if words else "sql"
    for index, word in enumerate(words[:-1]):
        if word.lower() in ("from", "into", "update", "table"):
            table = words[index + 1].strip('("')
            return f"{verb} {table}"
    return verb
