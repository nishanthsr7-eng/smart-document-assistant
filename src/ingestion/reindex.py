"""Rebuild the index under a new build, then cut over to it.

`ingest_version` is a hash of the chunker settings and the embedder name. Before this module it
was a hash with nothing behind it: change the chunk size and every existing document stopped
matching the new value, vanished from `/documents`, and stayed vanished -- no migration, no
error, and the only way back was to re-upload every file.

The rebuild is blue-green because it has to be. Reindexing in place would mean the corpus is
partly old and partly new for as long as it takes, and a question asked in that window would be
answered from whichever half it happened to land in. Instead the new chunks are written beside
the old ones, reads stay pinned to `storage.alias` throughout, and the cutover is one row
changing. The rollback is that row changing back, for as long as the old chunks survive
`RETENTION_OLD_INDEX_DAYS`.
"""

import json
import time
from typing import Any, Optional, cast

from src.auth.principal import Principal
from src.core import logs, metrics
from src.core.config import SETTINGS
from src.ingestion.parsers import FigureCaptioner
from src.ingestion.pipeline import Embedding, reindex_document, stale_documents
from src.storage import alias
from src.storage.redis_client import client

logger = logs.logger(__name__)

STATE_KEY = "reindex:state"
LOCK_KEY = "reindex"

IDLE = "idle"
RUNNING = "running"
DONE = "done"
FAILED = "failed"


def status() -> dict:
    """What the last or current rebuild is doing, next to what the alias says.

    Counts only, no filenames: a reindex spans every tenant, and this is read by one admin.
    """
    raw = cast(Optional[bytes], client().get(STATE_KEY))
    progress = json.loads(raw) if raw else {"status": IDLE}
    return {**alias.state(), "progress": progress}


def _write(**fields: Any) -> None:
    client().set(STATE_KEY, json.dumps(fields), ex=SETTINGS.jobs.state_ttl_s)


def run(embedder: Embedding, captioner: Optional[FigureCaptioner] = None) -> dict:
    """Rebuild every stale document, then promote. Returns the summary it also stores."""
    target = SETTINGS.ingest_version
    started = time.perf_counter()
    worklist = stale_documents()
    _write(status=RUNNING, target=target, total=len(worklist), done=0, failed=0, started=started)
    logger.info("Reindex starting", extra={"target": target, "documents": len(worklist)})

    done = 0
    failures: list[str] = []
    for entry in worklist:
        owner = Principal(
            user_id=entry["owner_id"],
            tenant_id=entry["tenant_id"],
            email="",
            role="admin",
        )
        try:
            reindex_document(entry["doc_id"], entry["filename"], owner, embedder, captioner)
            done += 1
        except Exception as exc:
            # One unreadable document must not strand the cutover: it is recorded, skipped, and
            # left at its old build, where the old alias is still serving it.
            logger.exception("Reindex failed for a document", extra={"doc_id": entry["doc_id"]})
            failures.append(f"{entry['doc_id']}: {exc}")
        metrics.REINDEXED_DOCUMENTS.inc()
        _write(
            status=RUNNING,
            target=target,
            total=len(worklist),
            done=done,
            failed=len(failures),
            started=started,
        )

    summary = {
        "status": DONE if not failures else FAILED,
        "target": target,
        "total": len(worklist),
        "done": done,
        "failed": len(failures),
        "errors": failures[:10],
        "duration_s": round(time.perf_counter() - started, 2),
        "promoted": False,
    }
    # Promote only on a clean sweep. A partial cutover would hide every document the rebuild
    # could not produce, which is precisely the silent disappearance this module exists to stop.
    if not failures:
        summary["previous"] = alias.promote(target)
        summary["promoted"] = True
    _write(**summary)
    logger.info("Reindex finished", extra=summary)
    return summary
