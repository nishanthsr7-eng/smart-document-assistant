"""Structured JSON logging. Every line carries the request/query/tenant it belongs to."""

import logging
import sys
from typing import Any, MutableMapping

import structlog
from opentelemetry import trace as otel_trace

from src.core.config import SETTINGS

_configured = False


def setup(role: str) -> None:
    """Route stdlib logging (uvicorn, sqlalchemy, arq) and our own calls through one renderer."""
    global _configured
    if _configured:
        return
    cfg = SETTINGS.observability
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _add_trace_ids,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer() if cfg.log_json else structlog.dev.ConsoleRenderer()
    )

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(cfg.log_level)
    # Access logs duplicate the request middleware's own line, with no correlation ids on them.
    logging.getLogger("uvicorn.access").disabled = True

    structlog.contextvars.bind_contextvars(
        service=cfg.service_name, role=role, env=cfg.environment
    )
    _configured = True


def _add_trace_ids(
    logger: Any, method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Correlation between a log line and its span, both ways."""
    span = otel_trace.get_current_span()
    context = span.get_span_context()
    if context.is_valid:
        event_dict["trace_id"] = format(context.trace_id, "032x")
        event_dict["span_id"] = format(context.span_id, "016x")
    return event_dict


def logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.stdlib.get_logger(name)


def bind(**fields: Any) -> None:
    structlog.contextvars.bind_contextvars(**fields)


def unbind(*keys: str) -> None:
    structlog.contextvars.unbind_contextvars(*keys)
