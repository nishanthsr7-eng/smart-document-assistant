import asyncio
import logging
import time
from dataclasses import asdict

from arq import cron
from prometheus_client import start_http_server

from src.api import deps, webhooks
from src.auth import audit
from src.auth.principal import Principal
from src.core import logs, metrics, observability, otel
from src.core.config import SETTINGS
from src.core.errors import DocumentError
from src.ingestion import jobs, reindex
from src.ingestion.pipeline import discard_failed, ingest, sweep_expired
from src.storage import objects
from src.storage.redis_client import lock

observability.setup("worker")
logger = logs.logger(__name__)


async def ingest_job(
    ctx: dict, doc_id: str, filename: str, job_id: str, owner: dict
) -> dict:
    """Parse, chunk, embed and index a staged upload. Runs off the API's event loop."""
    principal = Principal(**owner)
    try:
        report = await asyncio.to_thread(_run, job_id, doc_id, filename, principal)
    except Exception as exc:
        # After the job's own failure handling, and never in place of it: a callback is how a
        # caller hears about the outcome, not part of producing it.
        await webhooks.dispatch(
            principal.tenant_id,
            webhooks.INGEST_FAILED,
            {"job_id": job_id, "doc_id": doc_id, "filename": filename, "error": str(exc)},
        )
        raise
    await webhooks.dispatch(
        principal.tenant_id,
        webhooks.INGEST_COMPLETED,
        {"job_id": job_id, "doc_id": doc_id, "filename": filename, "report": report},
    )
    return report


def _run(job_id: str, doc_id: str, filename: str, owner: Principal) -> dict:
    logs.bind(job_id=job_id, doc_id=doc_id, tenant_id=owner.tenant_id)
    started = time.perf_counter()
    with otel.tracer().start_as_current_span("ingest") as span:
        span.set_attribute("sda.job_id", job_id)
        span.set_attribute("sda.doc_id", doc_id)
        span.set_attribute("sda.tenant_id", owner.tenant_id)
        try:
            return _ingest(job_id, doc_id, filename, owner)
        finally:
            metrics.INGEST_DURATION.observe(time.perf_counter() - started)
            logs.unbind("job_id", "doc_id", "tenant_id")


def _ingest(job_id: str, doc_id: str, filename: str, owner: Principal) -> dict:
    jobs.record_stage(job_id, "Starting")
    try:
        data = objects.get_raw(doc_id, filename)
        report = ingest(
            filename,
            data,
            deps.embedder(),
            owner,
            captioner=deps.figure_captioner(),
            on_stage=lambda stage: jobs.record_stage(job_id, stage),
        )
    except DocumentError as exc:
        _fail(job_id, doc_id, filename, exc.message, owner, outcome="rejected")
        raise
    except Exception as exc:
        logger.exception("Ingest job failed")
        _fail(job_id, doc_id, filename, f"Ingest failed: {exc}", owner, outcome="failed")
        raise
    metrics.INGEST_JOBS.labels(report.outcome).inc()
    metrics.INGEST_CHUNKS.inc(report.num_children)
    payload = asdict(report)
    jobs.record_done(job_id, doc_id, payload)
    audit.record(
        owner,
        "ingest",
        doc_id=doc_id,
        filename=filename,
        outcome=report.outcome,
        num_children=report.num_children,
    )
    return payload


def _fail(
    job_id: str, doc_id: str, filename: str, message: str, owner: Principal, outcome: str
) -> None:
    metrics.INGEST_JOBS.labels(outcome).inc()
    jobs.record_failed(job_id, doc_id, filename, message, owner.tenant_id)
    audit.record(owner, "ingest_failed", doc_id=doc_id, filename=filename, error=message)
    # A failed ingest leaves a pending row and the staged upload behind; both are dead weight.
    discard_failed(doc_id)


async def reindex_job(ctx: dict) -> dict:
    """Rebuild the index at the running configuration's version and cut over to it.

    Serialized on a Redis lock: two workers rebuilding the same documents would race on the
    same chunk rows, and the second cutover would promote a version the first was still writing.
    """
    with lock(reindex.LOCK_KEY, SETTINGS.jobs.job_timeout_s, wait_s=0.5):
        return await asyncio.to_thread(reindex.run, deps.embedder(), deps.figure_captioner())


async def retention_sweep(ctx: dict) -> dict:
    """Hard-delete what has outlived its window. Runs on a schedule, never on a request."""
    if not SETTINGS.retention.enabled:
        return {"skipped": True}
    result = await asyncio.to_thread(sweep_expired)
    for kind, count in result.items():
        if count:
            metrics.RETENTION_PURGED.labels(kind.removesuffix("_purged")).inc(count)
    logger.info("Retention sweep", extra=result)
    return result


async def startup(ctx: dict) -> None:
    # arq's CLI installs its own plain-text handler after import, which duplicates every line
    # next to the JSON one. Dropping it here runs after that configuration.
    logging.getLogger("arq").handlers = []
    objects.ensure_bucket()
    # arq has no HTTP server of its own, so the worker exposes its own scrape endpoint.
    start_http_server(SETTINGS.observability.worker_metrics_port)


async def shutdown(ctx: dict) -> None:
    observability.shutdown()


class WorkerSettings:
    functions = [ingest_job, reindex_job, retention_sweep]
    # Hourly and off the hour: a sweep is idempotent, and staggering it keeps it away from
    # whatever else a deployment runs at :00.
    cron_jobs = [cron(retention_sweep, minute=7, run_at_startup=False)]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = jobs.redis_settings()
    queue_name = SETTINGS.jobs.queue_name
    max_jobs = SETTINGS.jobs.concurrency
    job_timeout = SETTINGS.jobs.job_timeout_s
    keep_result = SETTINGS.jobs.keep_result_s
    max_tries = 1
    health_check_key = "sda:ingest:health"
