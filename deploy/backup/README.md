# Backup, restore, and the drill

## What is backed up, and what is not

| Store | Holds | Backed up | Why |
|---|---|---|---|
| Postgres | documents, chunks, embeddings, users, tenants, audit log, webhooks, index alias | yes | system of record |
| Object store | uploaded files, parent blobs | yes | the only copy of the original bytes |
| Redis | answer cache, job state, rate-limit buckets, idempotency records | **no** | derived or ephemeral; restoring it restores a cache |

Losing Redis costs a cold cache, in-flight ingest jobs, and one rate-limit window. Losing either
of the other two, without a backup, loses the corpus.

## RPO and RTO

The targets, and the reason each is what it is:

- **RPO 24h** on the nightly dump. Every document also exists wherever the tenant uploaded it
  from, so the real exposure of a day's loss is re-uploading a day of documents, not losing
  them. Continuous archiving (WAL-G / pgBackRest shipping WAL to object storage) takes the RPO
  to minutes and is the right next step if that assumption stops holding.
- **RTO 1h** for a full restore. `pg_restore` of the dump and re-uploading the object tree are
  both bounded by corpus size; the drill measures it on real data every week, which is the only
  honest source for this number.

Neither target is met by having the scripts. They are met by the drill passing.

## Taking a backup

```sh
python -m deploy.backup.backup dump --into backups/$(date +%F)
```

Postgres goes out through `pg_dump --format=custom` (compressed, restorable table by table) and
the object store is mirrored key for key. Both stores are read with the same configuration the
application uses, so a backup is taken against exactly the deployment it belongs to.

## Restoring

```sh
python -m deploy.backup.backup restore --from backups/2026-09-29
```

`pg_restore` runs `--clean --if-exists --single-transaction`: the restore is idempotent against
a database that still holds the damaged objects, which is the realistic case, and it either
applies completely or not at all.

## The drill

An untested backup is not a backup. The drill is the test:

```sh
python -m deploy.backup.backup drill --into backups/drill
```

It records a snapshot, backs up, **destroys both stores**, restores, and compares. The snapshot
is deliberately more than row counts:

- document, chunk and embedded-chunk counts, and the object count;
- the index alias, because a restore that loses it serves an empty corpus from a full database;
- one real lexical search, because `chunks.tsv` is a generated column and a restore that
  repopulated the rows without it would pass every count and answer nothing.

It exits non-zero if any of those differ, which is what lets CI run it unattended.

**It destroys the stores it is pointed at.** Run it against a scratch deployment, on a corpus
seeded with `seed_drill_corpus.py` first. Never point it at production.

## Not done

Continuous archiving and point-in-time recovery, cross-region replication of the object store,
encryption of the backup at rest (the dump inherits whatever the destination provides), and a
restore into a *different* deployment to verify the dump is portable rather than only
self-consistent.
