"""Langfuse export of the answer trace: prompts, retrieved context, usage and cost per query.

Langfuse 3+ is itself an OpenTelemetry consumer, so it attaches to the tracer provider built in
`src.core.otel` instead of running a second tracing stack. There is one span tree; Langfuse reads
it through the attributes named below, which are inert when Langfuse is not configured.
"""

import json
from typing import Any, Optional

from langfuse import Langfuse, LangfuseOtelSpanAttributes
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Span

from src.core import redaction
from src.core.config import SETTINGS

_client: Optional[Langfuse] = None


def attach(provider: TracerProvider) -> None:
    """Add Langfuse's exporter to our provider. No keys configured means no export."""
    global _client
    cfg = SETTINGS.observability
    if _client is not None or not (cfg.langfuse_public_key and cfg.langfuse_secret_key):
        return
    _client = Langfuse(
        public_key=cfg.langfuse_public_key,
        secret_key=cfg.langfuse_secret_key,
        host=cfg.langfuse_host,
        environment=cfg.environment,
        release=cfg.service_version,
        sample_rate=cfg.langfuse_sample_rate,
        tracer_provider=provider,
        # Langfuse exports only spans it created itself by default; ours are plain OTel spans
        # annotated with its attributes, so the whole pipeline waterfall has to be let through.
        should_export_span=lambda span: True,
    )


def enabled() -> bool:
    return _client is not None


def flush() -> None:
    if _client is not None:
        _client.flush()


def shutdown() -> None:
    global _client
    if _client is not None:
        _client.shutdown()
        _client = None


def annotate_trace(
    span: Span,
    *,
    name: str,
    user_id: str = "",
    session_id: str = "",
    question: Optional[str] = None,
    answer: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    tags: Optional[list[str]] = None,
) -> None:
    """Promote a span to the Langfuse trace root and give it the query's input and output."""
    attrs = LangfuseOtelSpanAttributes
    span.set_attribute(attrs.TRACE_NAME, name)
    span.set_attribute(attrs.ENVIRONMENT, SETTINGS.observability.environment)
    if user_id:
        span.set_attribute(attrs.TRACE_USER_ID, user_id)
    if session_id:
        span.set_attribute(attrs.TRACE_SESSION_ID, session_id)
    if question is not None:
        span.set_attribute(attrs.TRACE_INPUT, _dumps(question))
    if answer is not None:
        span.set_attribute(attrs.TRACE_OUTPUT, _dumps(answer))
    if metadata:
        span.set_attribute(attrs.TRACE_METADATA, _dumps(metadata))
    if tags:
        span.set_attribute(attrs.TRACE_TAGS, tags)


def annotate_observation(
    span: Span,
    *,
    kind: str = "span",
    input_payload: Optional[Any] = None,
    output_payload: Optional[Any] = None,
    metadata: Optional[dict[str, Any]] = None,
    model: str = "",
    usage: Optional[dict[str, int]] = None,
    cost: Optional[dict[str, float]] = None,
) -> None:
    """Type one stage span for Langfuse: `kind="generation"` is what carries usage and cost."""
    attrs = LangfuseOtelSpanAttributes
    span.set_attribute(attrs.OBSERVATION_TYPE, kind)
    if input_payload is not None:
        span.set_attribute(attrs.OBSERVATION_INPUT, _dumps(input_payload))
    if output_payload is not None:
        span.set_attribute(attrs.OBSERVATION_OUTPUT, _dumps(output_payload))
    if metadata:
        span.set_attribute(attrs.OBSERVATION_METADATA, _dumps(metadata))
    if model:
        span.set_attribute(attrs.OBSERVATION_MODEL, model)
    if usage:
        span.set_attribute(attrs.OBSERVATION_USAGE_DETAILS, _dumps(usage))
    if cost:
        span.set_attribute(attrs.OBSERVATION_COST_DETAILS, _dumps(cost))


def _dumps(value: Any) -> str:
    """Every payload leaving for Langfuse goes through here, which is where PII is masked."""
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return redaction.scrub(text)
