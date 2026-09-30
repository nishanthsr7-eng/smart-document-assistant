# Architecture

Engineering reference for the Smart Document Assistant: what the system is, how a request moves
through it, and why each load-bearing decision is what it is. It describes the system as built.

The product thesis, stated once so every decision below can be checked against it:

> A general chat assistant answers from the model's memory and treats attached files as a
> courtesy. This tool inverts that: **no claim exists unless a passage in the user's corpus
> supports it, and every claim is traceable to a page.** The differentiators are provenance,
> refusal, calibration and auditability — not fluency.

Companion documents: [SLOs](slos.md) · [runbooks](runbooks.md) · [on-call](oncall.md) ·
[twelve-factor review](twelve-factor.md) · [backup and restore](../deploy/backup/README.md).

---

## 1. Shape

```
                       +-------------------------------------------+
  upload (pdf/txt) --->| API: validate, stage to object store,     |
                       |      enqueue job, return 202              |
                       +---------------------+---------------------+
                                             | arq queue (Redis)
                                             v
                       +-------------------------------------------+
                       | ingest worker                             |
                       |  parsers  -> Element[] (kind, page,       |
                       |              section_path), OCR fallback  |
                       |  chunker  -> ParentChunk[] (~800 tok)     |
                       |              ChildChunk[]  (~200 tok)     |
                       |  pipeline -> rows + vectors, state machine|
                       +---------------------+---------------------+
                                             |
                       +---------------------+---------------------+
                       | stores                                    |
                       |  Postgres: documents, chunks,             |
                       |            vector(768) HNSW, tsvector GIN |
                       |  Object store: raw uploads, parent blobs  |
                       |  Redis: answer cache, locks, queue,       |
                       |         rate limits, idempotency          |
                       +---------------------+---------------------+
                                             |
  question -> sanitize -> condense -> decompose -> dense || lexical -> RRF -> rerank
                                             |                                |
                                             v                                v
                       +-------------------------------------------+  abstention gate
                       | context assembly                         |
                       |  parent lookup -> MMR -> per-doc cap ->  |
                       |  span-centred window -> token budget     |
                       +---------------------+---------------------+
                                             | 4 numbered sources, ~3000 tok
                                             v
                       +-------------------------------------------+
                       | generation (Gemini | Groq | Ollama)       |
                       |  streamed, inline [n] citations required  |
                       +---------------------+---------------------+
                                             v
                       +-------------------------------------------+
                       | trust                                     |
                       |  sentence split -> numeric grounding ->   |
                       |  cross-encoder support -> conflicts ->    |
                       |  output scan -> confidence                |
                       +---------------------+---------------------+
                                             v
                             React SPA: answer, sources, trace
```

## 2. Topology

Three process types, scaled independently, sharing all state through Postgres, Redis and the
object store. Nothing correctness-bearing lives in a process.

| Process | Bound by | Notes |
|---|---|---|
| API (`uvicorn`, N workers) | IO, plus reranking when TEI is not configured | Stateless. Sheds load with 503 rather than queueing. 2-minute grace period so in-flight SSE answers drain. |
| Ingest worker (`arq`) | CPU — parsing, OCR, embedding | Holds model weights through a parse. 16-minute grace period so a 200-page document is not torn out from under a job. |
| TEI (optional) | CPU or GPU | Moves the embedder and reranker forward passes off the request path; batches across callers. |

Postgres, Redis and the object store are external dependencies in every environment, including
development. There is no local-disk fallback: a process that cannot reach them does not start.

## 3. Module inventory

| Layer | Module | Responsibility |
|---|---|---|
| core | `config.py` | `pydantic-settings` groups, validated at import; `ingest_version` and `retrieval_version` hashes |
| core | `flags.py` | Deploy-time selection of a threshold artifact, plus validated dotted-path overrides |
| core | `thresholds.py` | Loads `config/thresholds/<version>.yaml` and folds it into the cache key |
| core | `cache.py` | Redis answer cache and parent-payload cache, invalidated on ingest and delete |
| core | `limits.py` | Per-tenant token buckets and daily token/USD budgets |
| core | `redaction.py` | Pattern-based PII scrub over logs, span attributes and prompt captures |
| core | `errors.py` | Typed document/model/generation errors carrying user-facing messages |
| core | `health.py` | Backend pings only, memoized; no model construction, no provider call |
| core | `observability.py`, `otel.py`, `metrics.py`, `logs.py`, `tracing.py`, `llm_traces.py` | One `setup(role)` entrypoint: OTel traces, Prometheus metrics, JSON logs, Langfuse spans |
| core | `tokens.py` | Token counting via the embedder's tokenizer |
| ingestion | `upload.py` | Streams a multipart body to a spooled temp file; the request body is never held whole |
| ingestion | `parsers.py` | pypdf fast path or docling layout path; OCR when the text layer is below the per-page floor; table linearisation; BLIP figure captions; header/footer strip; NFKC normalisation |
| ingestion | `chunker.py` | Section-aware parents, sentence-window children with one sentence of overlap, atomic windows for tables and figures, `char_span_in_parent` for highlighting |
| ingestion | `pipeline.py` | Content-hash `doc_id`, `pending -> parsed -> indexed -> live` state machine, replace-by-filename under a Redis lock, duplicate short-circuit |
| ingestion | `jobs.py`, `worker.py` | Queue contract, job state and progress events, dead-letter queue |
| ingestion | `reindex.py` | Builds a new `ingest_version` alongside the live one, then cuts over |
| retrieval | `embedder.py` | `bge-base-en-v1.5`, normalised, asymmetric query prefix |
| retrieval | `tei.py` | HTTP transport for a TEI embedder or reranker, score-compatible with the local backend |
| retrieval | `vector_store.py` | pgvector cosine search, tenant and `ingest_version` predicates applied in SQL |
| retrieval | `keyword_index.py` | Postgres `tsvector` search ranked by `ts_rank_cd` |
| retrieval | `hybrid.py` | Multi-variant dense merge plus reciprocal rank fusion |
| retrieval | `reranker.py` | `mxbai-rerank-base-v1` cross-encoder, Platt-calibrated to a probability |
| retrieval | `query.py` | Question sanitisation, sub-query decomposition, LLM expansion, HyDE (off) |
| generation | `client.py` | `Provider` protocol, Gemini/Groq/Ollama implementations, failover chain with a circuit breaker |
| generation | `prompts.py` | Grounding prompt, nonce-fenced source blocks, citation parsing |
| generation | `answerer.py` | Orchestrator: cache -> condense -> decompose -> retrieve -> fuse -> rerank -> gate -> assemble -> generate -> validate -> score |
| trust | `abstention.py` | Threshold with a soft band and a retrieval-consensus override |
| trust | `citations.py` | Numeric grounding prefilter, cross-encoder support scoring, cross-document conflict detection |
| trust | `scanner.py` | Output-side injection check; a hit abstains rather than editing the answer |
| trust | `confidence.py` | Weighted signals into a score and a High/Medium/Low label |
| auth | `principal.py`, `passwords.py`, `tokens.py`, `service.py`, `audit.py` | Tenant-scoped principal, scrypt hashing, HS256 tokens, user management, audit log |
| storage | `db.py`, `models.py`, `objects.py`, `redis_client.py` | Engine and session, ORM, object store, Redis |
| storage | `alias.py` | The blue-green pointer: which `ingest_version` reads are served from |
| storage | `erasure.py` | Right-to-erasure across all three stores, with a verified receipt |
| api | `router.py`, `schemas.py`, `deps.py` | Endpoints, request/response models, bearer auth and role gates |
| api | `problems.py` | RFC 9457 `application/problem+json` for every error |
| api | `idempotency.py` | `Idempotency-Key` replay, backed by Redis |
| api | `pagination.py` | Opaque keyset cursors |
| api | `conditional.py` | `ETag` / `If-None-Match` on polled collections |
| api | `webhooks.py` | Ingest-completion callbacks: registration, signing, delivery |

## 4. Data model

Postgres is the system of record. Seven tables, defined in `migrations/versions/`:

| Table | Holds |
|---|---|
| `tenants` | One workspace |
| `users` | Email, scrypt hash, role, tenant |
| `audit_log` | Upload, query, delete and admin events, per tenant |
| `documents` | One row per ingested file: `state`, `ingest_version`, page and element counts, `deleted_at`, `superseded_by`, `version` |
| `chunks` | Child chunks with `embedding vector(768)`, generated `tsv`, parent pointer, page range, `section_path`, `char_span_in_parent` |
| `webhooks` | Per-tenant subscriptions and their signing secrets |
| `index_alias` | Which `ingest_version` reads are served from |

Two indexes carry retrieval: HNSW on `embedding` with `vector_cosine_ops`, and GIN on the
generated `tsv` column. `tenant_id` is denormalised onto `chunks` so the tenancy filter is a
predicate rather than a join, and the listing index is `(tenant_id, ingest_version, state)` —
exactly the predicate every read runs.

**Versioning.** `ingest_version` hashes the chunker settings and the embedder name;
`retrieval_version` additionally covers the threshold artifact and keys the answer cache. Writes
use the version the current configuration produces. Reads follow `index_alias` instead, so
changing a chunk size does not make the corpus disappear — it makes a new build possible, which
`reindex.py` fills and then cuts over to, with the previous build retained for rollback.

## 5. Request paths

### Ingest

`POST /ingest` streams the body to a spooled temp file, validates type, size and page count,
stages the bytes in the object store, writes a `pending` document row under a Redis lock keyed on
the filename, enqueues an arq job and returns 202 with a `job_id`. The API never parses.

The worker claims the row, parses, chunks, embeds, writes chunks and vectors, then flips the row
to `live`. A crash at any point leaves a row that is neither listed nor answerable, never a
half-indexed document. Failures land in a dead-letter queue readable at `GET /jobs/dead-letters`,
and drop the pending row and the staged blob. Progress is an SSE stream at
`GET /jobs/{job_id}/events`; `GET /jobs/{job_id}` is the poll equivalent.

`POST /ingest/batch` accepts up to `MAX_BULK_FILES` files in one request and enqueues each.

### Query

`POST /query` checks the answer cache (keyed on question, document set, mode, history and
`retrieval_version`), then: sanitize the question, condense it against history, decompose it into
sub-queries, run dense and lexical search, fuse with RRF, rerank with the cross-encoder — each
passage keeping its best score across sub-queries — apply the abstention gate, assemble sources
within the token budget, generate, validate citations, scan the output, score confidence.

Answers stream as SSE by default; `stream: false` returns one JSON body. Concurrency is bounded by
a semaphore matched exactly to the reranker's thread pool: past that, the endpoint answers 503
with `Retry-After` rather than queueing behind a saturated CPU. `QUERY_TIMEOUT_S` bounds the rest.

### Reindex

`POST /admin/index/reindex` rebuilds every live document under the new `ingest_version` while the
old build keeps serving, then moves the alias. `POST /admin/index/rollback` moves it back.
`GET /admin/index` reports both builds and their document counts. Admin index and retention routes
additionally require `OPERATOR_TOKEN`: they are deployment-level operations, not tenant ones.

## 6. Trust controls

1. **Question sanitisation** — instruction-shaped clauses are dropped before anything reads the
   text, so the embedding, the cross-encoder pair and the gate all see the actual question.
2. **Pre-generation gate** — abstain below the calibrated top-1 probability, with a soft band
   where support and grounding still hold a veto, and a consensus override when dense and lexical
   search independently rank the same chunk highly.
3. **Closed-book prompt** — sources are nonce-fenced and declared untrusted data; tag-shaped text
   inside documents is stripped.
4. **Numeric grounding** — every figure in a sentence must appear, normalised, in the cited text
   before semantic scoring is attempted.
5. **Cross-encoder support** — each citing sentence is scored against each source it cites; below
   threshold it is surfaced as `unverified`, and with no citation at all as `unsupported`.
6. **Conflict detection** — cross-document numeric disagreement on same-shaped claims is shown
   with both citations rather than silently resolved.
7. **Output scan** — a finished answer showing the signature of a followed instruction abstains.
8. **Confidence** — weighted signals, surfaced as a label plus the component breakdown.

Every threshold above lives in `config/thresholds/<version>.yaml`, not in source.

## 7. Tenancy and authorisation

Every row holding user data carries `tenant_id`. A caller's tenant comes from their bearer token
and never from a parameter, and the filter is applied inside the retrieval layer, so another
workspace's `doc_id` returns nothing rather than erroring. Roles are ordered: `viewer` reads and
asks, `editor` also uploads and deletes, `admin` also manages users and reads the audit log and
dead-letter queue. `tests/test_tenancy.py` asserts that no retrieval path, delete, job read or
cache entry crosses a workspace boundary.

## 8. API conventions

| Concern | Convention |
|---|---|
| Errors | RFC 9457 `application/problem+json`, with a stable `type` URI to branch on |
| Replay | `Idempotency-Key` on unsafe requests: a finished request replays its response, an in-flight one is 409, the same key with a different body is 422 |
| Collections | Opaque keyset cursors (`next_cursor`), stable under concurrent insertion |
| Polling | `ETag` / `If-None-Match` on `/documents`, so an unchanged list costs a 304 with no body |
| Streaming | SSE for answers and for ingest progress, with keepalives |
| Callbacks | Signed `ingest.completed` / `ingest.failed` webhooks, retried with backoff and disabled after repeated failure; private network targets are rejected unless explicitly allowed |
| Load | 503 with `Retry-After` when past the concurrency cap or while draining |

## 9. Limits and budgets

Two independent controls, both in Redis so every replica enforces one number. Rate limits are
per-tenant token buckets on the query, ingest and auth paths: they bound how fast the service is
asked to work and refill on their own. Budgets are daily counters of generation tokens and USD:
they bound what a tenant can spend, and only a new day clears them. Unauthenticated auth attempts
are bucketed by client address, since the caller has no tenant yet.

## 10. Configuration

Every group in `src/core/config.py` is a `BaseSettings` model validated at import, so a bad value
fails at startup rather than at first use. Decision thresholds are the deliberate exception to
strict config-in-environment: they are a swept set of related numbers, so they ship as a versioned
YAML artifact selected by `THRESHOLDS_VERSION`, with per-deploy overrides validated against the
same schema. Rationale in the [twelve-factor review](twelve-factor.md).

## 11. Observability

One span tree per request (`POST /query` -> `answer` -> `searching` / `reranking` / `assembling` /
`generating`, with SQL spans nested inside the stage that issued them) and one per ingest job.
Metrics cover stage and answer latency, abstain rate, confidence distribution, citation validity,
retrieval top score, tokens and USD per model, cache hit rate, ingest outcomes and queue depth.
Logs are JSON, every line carrying `request_id`, `query_id` and `tenant_id` — or `job_id` and
`doc_id` in the worker — plus `trace_id` and `span_id`. Langfuse reads the same span tree for
prompt-level traces. Alert rules live in `deploy/helm/sda/files/alerts.yml`, loaded both by the
compose Prometheus and by the chart. Targets and error budgets are in [slos.md](slos.md).

## 12. Data lifecycle

Deletes are soft. Chunks go immediately, because a deleted document must stop being answerable at
once, while the row and the stored bytes outlive the index by the purge window — which is what
makes `POST /documents/{doc_id}/restore` possible. Replacing a file by name no longer destroys
what it replaced: the old row points at the new one through `superseded_by`, so
`GET /documents/{doc_id}/versions` can answer which version was live when a question was asked. A
sweep purges past the retention windows, and `DELETE /auth/users/{user_id}/data` walks Postgres,
the object store and Redis, then re-reads each and returns a countable receipt. Backup scope,
RPO/RTO and the weekly restore drill are in
[deploy/backup/README.md](../deploy/backup/README.md).

## 13. Frontend

A Vite SPA. `api.js` is the only module that talks HTTP, and responses are validated against Zod
schemas at that boundary, so a renamed field surfaces as one named error instead of `undefined`
three components deep. Server state is React Query (`useBootstrap`, `useDocuments`, `useChat`,
`useAuth`); an `ErrorBoundary` wraps the tree so a render error shows a recoverable state rather
than a blank page. User-visible strings go through a small `i18n` catalogue. Error reporting and
analytics are opt-in and lazily loaded, so a default build ships neither — which is what keeps the
gzipped bundle budget enforced by `scripts/check-bundle-size.mjs` meaningful.

## 14. Testing and evaluation

`pytest` covers the API, tenancy, the ingest pipeline, parsers, limits, failover, lifecycle,
erasure, security, config validation, the OpenAPI and SSE contracts, and property-based
invariants — gated at 75% coverage against real Postgres, Redis and object storage. Vitest covers
the API client, the i18n catalogue and the components with logic in them. `evaluation/run_eval.py`
scores a golden set and gates CI on it in two profiles: `retrieval` on every pull request without
an API key, `generation` nightly with one. `tests/load/query.js` is a k6 profile for the answer
path, run by hand against hardware worth measuring.

## 15. Load-bearing decisions

| Decision | Alternative rejected | Why |
|---|---|---|
| Postgres + pgvector for vectors and metadata | Embedded ChromaDB | One transactional store shared by every replica; the embedded client was single-writer and per-process |
| Postgres `tsvector` for lexical search | In-process BM25 | The BM25 corpus lived in one worker's RAM and rebuilt on every upload; fusion is rank-based, so the change of score scale is immaterial |
| Parent/child chunking | Flat chunks | Children are sized to the embedder's window for retrieval precision; parents give the model enough context to answer |
| Reciprocal rank fusion | Score-weighted blend | Rank-based fusion needs no calibration between two incomparable score scales |
| Cross-encoder rerank by default | Dense-only | It is both the precision pass and the citation-support signal; `hybrid` and `dense` remain available when latency matters more |
| Queue the ingest, never serve it | Parse in the request | Parsing is minutes of CPU: in-request it blocks the event loop and ties document durability to an HTTP connection |
| Cloud LLM by default, Ollama offline | Local-only generation | CPU generation capable of following citation instructions is multi-second per answer; the offline path stays supported |
| Thresholds as a versioned artifact | Environment variables | They are a swept set; setting one independently is the mistake the artifact prevents |
| Blue-green reindex behind an alias | In-place reindex | A chunker change must not make the corpus vanish, and a bad build must be revertible without re-uploading every file |
| Soft delete with a purge window | Hard delete | A delete made in error stays recoverable, while the document stops being answerable immediately |
