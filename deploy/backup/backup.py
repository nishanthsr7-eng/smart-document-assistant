"""Back up and restore the two stores that hold state, and prove the backup works.

Postgres holds every row -- documents, chunks, embeddings, users, the audit log, the index
alias -- and the object store holds the uploaded bytes and the parent blobs. Redis holds the
answer cache, job state and rate-limit buckets, all of which are derived or ephemeral, so it is
deliberately not backed up: restoring it would restore a cache.

`drill` is the part that matters. An untested backup is not a backup, so the drill takes a
backup, destroys both stores, restores, and then asserts the corpus is queryable again --
document count, chunk count, embedding dimensions, the index alias, and a real lexical search
returning the passage it returned before. It exits non-zero if any of that is not true, which is
what makes it something CI can run on a schedule.

    python -m deploy.backup.backup dump    --into backups/2026-09-29
    python -m deploy.backup.backup restore --from backups/2026-09-29
    python -m deploy.backup.backup drill   --into backups/drill
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import func, select, text
from sqlalchemy.exc import ProgrammingError

from src.core.config import SETTINGS
from src.storage import objects
from src.storage.db import engine, session
from src.storage.models import Chunk, Document, IndexAlias

DUMP_NAME = "postgres.dump"
OBJECTS_DIR = "objects"


def _pg_env() -> dict[str, str]:
    """libpq reads the password from the environment; the URL is passed on the command line and
    would otherwise appear in `ps` output."""
    url = urlparse(SETTINGS.storage.database_url.replace("+psycopg", ""))
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = url.password
    return env


def _pg_args() -> list[str]:
    url = urlparse(SETTINGS.storage.database_url.replace("+psycopg", ""))
    return [
        "-h", url.hostname or "localhost",
        "-p", str(url.port or 5432),
        "-U", url.username or "postgres",
        "-d", (url.path or "/postgres").lstrip("/"),
    ]


def dump(into: Path) -> None:
    into.mkdir(parents=True, exist_ok=True)
    # Custom format: compressed, and restorable table by table, which is what a partial recovery
    # needs. `--no-owner` so a restore into a differently-named role works.
    subprocess.run(
        ["pg_dump", "--format=custom", "--no-owner", "--file", str(into / DUMP_NAME), *_pg_args()],
        env=_pg_env(),
        check=True,
    )
    _dump_objects(into / OBJECTS_DIR)
    print(f"Backed up to {into}")


def _dump_objects(into: Path) -> int:
    if into.exists():
        shutil.rmtree(into)
    into.mkdir(parents=True)
    client = objects._client()
    bucket = SETTINGS.storage.s3_bucket
    count = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            target = into / obj["Key"]
            target.parent.mkdir(parents=True, exist_ok=True)
            client.download_file(bucket, obj["Key"], str(target))
            count += 1
    return count


def restore(source: Path) -> None:
    dump_file = source / DUMP_NAME
    if not dump_file.exists():
        raise SystemExit(f"No dump at {dump_file}")
    # `--clean --if-exists` makes the restore idempotent against a database that still has the
    # old objects in it, which is the realistic case: a restore is usually a recovery, not a
    # first install.
    subprocess.run(
        [
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "--single-transaction",
            *_pg_args(),
            str(dump_file),
        ],
        env=_pg_env(),
        check=True,
    )
    objects.ensure_bucket()
    _restore_objects(source / OBJECTS_DIR)
    print(f"Restored from {source}")


def _restore_objects(source: Path) -> int:
    if not source.exists():
        return 0
    client = objects._client()
    bucket = SETTINGS.storage.s3_bucket
    count = 0
    for path in source.rglob("*"):
        if path.is_file():
            client.upload_file(str(path), bucket, path.relative_to(source).as_posix())
            count += 1
    return count


EMPTY = {
    "documents": 0,
    "chunks": 0,
    "embedded_chunks": 0,
    "alias": None,
    "probe_chunk": None,
    "objects": 0,
}


def snapshot() -> dict:
    """The facts a restore has to reproduce. Deliberately includes a real query: row counts
    prove the rows came back, and only a search proves the index did.

    A missing schema is a state this is expected to observe -- the drill calls it immediately
    after destroying the stores, and "nothing is there" is the answer it wants, not a crash.
    """
    try:
        return _snapshot()
    except ProgrammingError:
        return {**EMPTY, "objects": len(_object_keys())}


def _snapshot() -> dict:
    with session() as sess:
        documents = sess.scalar(select(func.count()).select_from(Document))
        chunks = sess.scalar(select(func.count()).select_from(Chunk))
        embedded = sess.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.embedding.isnot(None))
        )
        alias_row = sess.scalars(select(IndexAlias)).first()
        # One lexical hit, from the generated tsvector column -- which is exactly the thing a
        # naive row-by-row restore would leave unpopulated.
        probe = sess.execute(
            text(
                "SELECT chunk_id FROM chunks "
                "WHERE tsv @@ plainto_tsquery('english', :q) ORDER BY chunk_id LIMIT 1"
            ),
            {"q": "leave"},
        ).scalar()
    return {
        "documents": documents,
        "chunks": chunks,
        "embedded_chunks": embedded,
        "alias": alias_row.ingest_version if alias_row else None,
        "probe_chunk": probe,
        "objects": len(_object_keys()),
    }


def _object_keys() -> list[str]:
    client = objects._client()
    keys: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=SETTINGS.storage.s3_bucket):
        keys += [obj["Key"] for obj in page.get("Contents", [])]
    return keys


def _destroy() -> None:
    """Simulate losing both stores. Only ever called by the drill, and only against the
    database the drill is pointed at."""
    with engine().begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    objects.delete_keys(_object_keys())


def drill(into: Path) -> int:
    before = snapshot()
    if not before["documents"]:
        raise SystemExit(
            "Nothing to drill on: ingest at least one document into this deployment first."
        )
    print(f"before:  {before}")

    dump(into)
    _destroy()
    after_destroy = snapshot()
    if after_destroy["documents"]:
        raise SystemExit("The drill did not actually destroy anything; the result proves nothing.")

    restore(into)
    after = snapshot()
    print(f"after:   {after}")

    differences = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    if differences:
        print("RESTORE DRILL FAILED -- these did not come back:", file=sys.stderr)
        for key, (was, now) in differences.items():
            print(f"  {key}: {was!r} -> {now!r}", file=sys.stderr)
        return 1
    print("RESTORE DRILL PASSED")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["dump", "restore", "drill", "snapshot"])
    parser.add_argument("--into", type=Path, default=Path("backups/latest"))
    parser.add_argument("--from", dest="source", type=Path)
    args = parser.parse_args()

    if args.action == "dump":
        dump(args.into)
    elif args.action == "restore":
        restore(args.source or args.into)
    elif args.action == "snapshot":
        print(snapshot())
    else:
        return drill(args.into)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
