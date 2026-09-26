"""One setup call per process: logs, traces, error reporting. Metrics need no setup."""

from src.core import logs, otel
from src.core.config import SETTINGS


def setup(role: str) -> None:
    """`role` is "api" or "worker": it labels every span, metric-free log line and Sentry event."""
    logs.setup(role)
    _setup_sentry(role)
    otel.setup(role)
    _instrument_backends()


def shutdown() -> None:
    otel.shutdown()


def _setup_sentry(role: str) -> None:
    cfg = SETTINGS.observability
    if not cfg.sentry_dsn:
        return
    import sentry_sdk

    sentry_sdk.init(
        dsn=cfg.sentry_dsn,
        environment=cfg.environment,
        release=cfg.service_version,
        traces_sample_rate=cfg.sentry_traces_sample_rate,
        # Prompts and document text pass through this process; nothing is attached by default.
        send_default_pii=False,
    )
    sentry_sdk.set_tag("role", role)


def _instrument_backends() -> None:
    from src.storage.db import engine

    otel.instrument_engine(engine())
