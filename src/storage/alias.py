"""The blue-green index pointer: which `ingest_version` reads are served from.

`SETTINGS.ingest_version` is what the *current configuration* would produce. That is the right
thing to write and the wrong thing to read: the moment someone changes the chunk size, every
already-indexed document stops matching it and the whole corpus disappears from `/documents`
with no migration and no warning. This module is the fix -- reads follow a pointer that only a
completed reindex moves.

The value is cached for a few seconds rather than read per query. It changes at most once per
reindex, and the cost of a stale read for that window is that a reader is served by the index
that was working a moment ago.
"""

import time
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from src.core import logs
from src.core.config import SETTINGS
from src.storage.db import session
from src.storage.models import IndexAlias

logger = logs.logger(__name__)

ACTIVE = "active"
_CACHE_TTL_S = 5.0

_cached: Optional[str] = None
_cached_at = 0.0


def active_version() -> str:
    """The version reads are pinned to. Seeds itself to the current build on first use."""
    global _cached, _cached_at
    now = time.monotonic()
    if _cached is not None and now - _cached_at < _CACHE_TTL_S:
        return _cached
    with session() as sess:
        row = sess.scalars(select(IndexAlias).where(IndexAlias.name == ACTIVE)).first()
        if row is None:
            # A fresh install: the first thing indexed defines the alias.
            sess.execute(
                insert(IndexAlias)
                .values(name=ACTIVE, ingest_version=SETTINGS.ingest_version)
                .on_conflict_do_nothing(index_elements=[IndexAlias.name])
            )
            version = SETTINGS.ingest_version
        else:
            version = row.ingest_version
    _cached, _cached_at = version, now
    return version


def state() -> dict:
    """What an operator needs to decide whether a reindex is due: what is being read, what the
    running configuration would write, and what the last cutover replaced."""
    with session() as sess:
        row = sess.scalars(select(IndexAlias).where(IndexAlias.name == ACTIVE)).first()
    return {
        "active_version": row.ingest_version if row else None,
        "previous_version": row.previous_version if row else None,
        "building_version": SETTINGS.ingest_version,
        "reindex_needed": bool(row and row.ingest_version != SETTINGS.ingest_version),
        "updated_at": row.updated_at.isoformat() if row else None,
    }


def promote(version: str) -> str:
    """Cut over to `version`, remembering what it replaced so a rollback has somewhere to go."""
    with session() as sess:
        row = sess.scalars(select(IndexAlias).where(IndexAlias.name == ACTIVE)).first()
        previous = row.ingest_version if row else None
        if row is None:
            sess.execute(insert(IndexAlias).values(name=ACTIVE, ingest_version=version))
        else:
            sess.execute(
                update(IndexAlias)
                .where(IndexAlias.name == ACTIVE)
                .values(ingest_version=version, previous_version=previous)
            )
    _invalidate()
    logger.info("Index alias promoted", extra={"version": version, "previous": previous})
    return previous or version


def rollback() -> str:
    """Point back at the previous build. Possible exactly as long as its chunks are unpurged."""
    with session() as sess:
        row = sess.scalars(select(IndexAlias).where(IndexAlias.name == ACTIVE)).first()
        if row is None or not row.previous_version:
            raise LookupError("No previous index version to roll back to.")
        target, current = row.previous_version, row.ingest_version
        sess.execute(
            update(IndexAlias)
            .where(IndexAlias.name == ACTIVE)
            .values(ingest_version=target, previous_version=current)
        )
    _invalidate()
    logger.warning("Index alias rolled back", extra={"version": target, "from": current})
    return target


def _invalidate() -> None:
    global _cached, _cached_at
    _cached, _cached_at = None, 0.0
