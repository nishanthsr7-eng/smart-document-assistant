import asyncio
import logging
from dataclasses import asdict

from src.api import deps
from src.auth import audit
from src.auth.principal import Principal
from src.core.config import SETTINGS
from src.core.errors import DocumentError
from src.ingestion import jobs
from src.ingestion.pipeline import discard_failed, ingest
from src.storage import objects

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def ingest_job(
    ctx: dict, doc_id: str, filename: str, job_id: str, owner: dict
) -> dict:
    """Parse, chunk, embed and index a staged upload. Runs off the API's event loop."""
    return await asyncio.to_thread(_run, job_id, doc_id, filename, Principal(**owner))


def _run(job_id: str, doc_id: str, filename: str, owner: Principal) -> dict:
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
        _fail(job_id, doc_id, filename, exc.message, owner)
        raise
    except Exception as exc:
        logger.exception("Ingest job %s failed", job_id)
        _fail(job_id, doc_id, filename, f"Ingest failed: {exc}", owner)
        raise
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


def _fail(job_id: str, doc_id: str, filename: str, message: str, owner: Principal) -> None:
    jobs.record_failed(job_id, doc_id, filename, message, owner.tenant_id)
    audit.record(owner, "ingest_failed", doc_id=doc_id, filename=filename, error=message)
    # A failed ingest leaves a pending row and the staged upload behind; both are dead weight.
    discard_failed(doc_id)


async def startup(ctx: dict) -> None:
    objects.ensure_bucket()


class WorkerSettings:
    functions = [ingest_job]
    on_startup = startup
    redis_settings = jobs.redis_settings()
    queue_name = SETTINGS.jobs.queue_name
    max_jobs = SETTINGS.jobs.concurrency
    job_timeout = SETTINGS.jobs.job_timeout_s
    keep_result = SETTINGS.jobs.keep_result_s
    max_tries = 1
    health_check_key = "sda:ingest:health"
