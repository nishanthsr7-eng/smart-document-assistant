# Production readiness — gap analysis

Audit date: 2026-09-26. Reviewed `src/`, `frontend/src/`, `evaluation/`, `tests/`, `.claude/`.

Scope: what separates this repo (a well-structured prototype) from a service an organization would
operate. Findings are code-anchored; line numbers are from the audit commit (`17eaaf8`).

Nothing here says the current code is wrong for what it is. The structure — `trust/` as a
first-class layer, the `Trace` abstraction, the provider `Protocol`, span-anchored citations — is
better than most production RAG. The gap is almost entirely the assumption that **one Python process
owns all the state**.

---

## P0 — Blockers: it cannot run as a service today

### 1. All state is process-local, so exactly one worker, forever  — **DONE**
- `src/api/deps.py` builds `lru_cache` singletons; `KeywordIndex` (`src/retrieval/keyword_index.py:23`)
  holds the entire BM25 corpus in RAM and rebuilds on every upload. `uvicorn --workers 4` gives four
  divergent indexes — an upload served by worker 1 is invisible to workers 2-4 until restart.
- `ANSWER_CACHE` (`src/core/cache.py:46`) is in-process: hit rate collapses across replicas, and
  invalidation-on-ingest only affects one process.
- `manifest.json` (`src/ingestion/pipeline.py:158`) is read-modify-write with **no lock** — two
  concurrent uploads lose an entry or truncate the file.
- Parents/children persist as per-doc JSON on local disk: no shared volume, no replicas, no
  surviving a pod restart.

**Fix:** Postgres (SQLAlchemy 2.0 + Alembic) as system of record for documents/chunks/manifest;
S3/MinIO for raw files and parent blobs; Redis for answer cache and distributed locks. Highest
leverage change in this document — everything else assumes it.

**Done.** `src/storage/` holds the engine/session, ORM models, object store and Redis client.
`manifest.json` and the per-doc parent JSON are gone: documents and chunks live in Postgres, raw
uploads and parent payloads in MinIO, the answer and parent caches in Redis. The replace-by-filename
read-modify-write now runs under a Redis lock keyed on the filename. A document is written `pending`
and flipped `live` only after its embeddings are acknowledged, so a half-finished ingest is never
listed or queried.

### 2. ChromaDB `PersistentClient` is an embedded library, not a database — **DONE**
Single-writer, no auth, no snapshots, no HA, no horizontal reads. Migrate to **pgvector** (best fit:
one transactional store for metadata + vectors, no extra service) or **Qdrant** for HNSW tuning,
payload indexes, sparse vectors, quantization, snapshot/restore. Replace in-memory BM25 with
Postgres `tsvector` + GIN, Qdrant sparse vectors / SPLADE, or OpenSearch if you want real lexical
features (BM25F, synonyms, analyzers).

**Done, via pgvector.** Vectors are an HNSW/cosine `vector(768)` column on `chunks`; lexical search
is a generated `tsvector` column with a GIN index, ranked by `ts_rank_cd`. This landed with item 1
rather than after it: a multi-worker deployment is not actually reached while the vector index and
the BM25 corpus still live in each process. `chromadb` and `rank-bm25` are dropped. The remaining
Qdrant arguments (sparse vectors, quantization, snapshot/restore) still stand if the corpus grows
past what one Postgres comfortably serves.

### 3. Ingestion runs inside the HTTP request and blocks the event loop — **DONE**
`ingest_document` (`src/api/router.py:63`) is `async def` but calls synchronous docling parsing, BLIP
captioning and embedding directly. That blocks the **whole event loop** — during a 90s PDF ingest
every other request, including `/health`, hangs.

**Fix:** job queue. Celery or arq/Dramatiq on Redis for straightforward fan-out; **Temporal** for
durable multi-step pipelines with per-stage retries and visibility. `POST /documents` returns `202` +
`job_id`; progress over SSE/WebSocket from job state in Redis; idempotency key = content hash; DLQ
for poison documents; separate CPU/GPU worker pool scaled independently from the API.

**Done, with arq.** `POST /ingest` validates, stages the raw bytes in the object store and returns
`202` with a `job_id`; `src/ingestion/worker.py` is a separate process (`python -m arq
src.ingestion.worker.WorkerSettings`) that runs the pipeline in a thread off its event loop.
Progress is streamed from `GET /jobs/{job_id}/events` (per-stage SSE, ends on terminal state or
client disconnect) and polled from `GET /jobs/{job_id}`; terminal failures land in a Redis DLQ
readable at `GET /jobs/dead-letters`, and a failed job drops its pending row and staged blob.
The idempotency key is the content hash: a Redis claim on `sha256(bytes)` collapses a concurrent
burst into one job. Measured on a 13-page PDF: `POST /ingest` returns in 0.42s against a 23.5s
ingest, and `/livez` stays at 3ms p50 / 27ms max throughout — previously the whole event loop was
blocked for the duration. Not done: a GPU/CPU worker pool split, and automatic retries (a failed
job is terminal and waits in the DLQ).

### 4. Upload path OOMs on a large POST
`data = await file.read()` buffers the entire body into RAM, and `validate_upload`
(`src/ingestion/parsers.py:38`) checks the 20 MB cap **after** the bytes are resident. A 2 GB POST
kills the process before validation runs. Stream to a temp file with a hard byte cap, plus
`client_max_body_size` at the proxy.

### 5. No identity, no tenancy — documents are a global namespace — **DONE**
`doc_id = sha256(content)` is global, and `_find_by_filename` (`src/ingestion/pipeline.py:155`) will
**replace another user's document** because it shares a filename. `DELETE /documents/{doc_id}`
accepts any id from anyone.

**Fix:** OIDC/JWT auth (Keycloak self-hosted, or Auth0/Clerk/WorkOS); `tenant_id` + `owner_id` on
every chunk's metadata with the filter injected in the retrieval layer, not by callers;
`doc_id = hash(tenant_id, content)`; RBAC (viewer/editor/admin); audit log of upload/query/delete.
Add a test that a query scoped to tenant A can never surface a tenant B chunk — that is the one test
auditors ask for.

**Done, with local JWT auth.** `src/auth/` holds the principal, scrypt password hashing, HS256
token issue/verify, the registration and user service, and the audit log. Registration creates a
tenant with its first user as admin; that admin adds the rest, and the tenant is never a request
parameter. Roles are ordered `viewer < editor < admin`: viewers read and ask, editors upload and
delete, admins manage users and read the audit log. `tenants`, `users` and `audit_log` are new
tables; `documents` gained `tenant_id` + `owner_id` and `chunks` a denormalized `tenant_id`.

`doc_id` is now `sha256(tenant_id || content)`, which kills the replace-by-filename bug at the
root: two tenants uploading the same file get two documents, and no tenant can name another's
document by hashing a file it already holds. The tenant filter is a predicate inside
`VectorStore` and `KeywordIndex` rather than something callers pass through — every method takes
`tenant_id`, so omitting it is a type error, not a silent leak. The router additionally narrows
caller-supplied `doc_ids` with `scope_doc_ids`, dropping foreign ids rather than 404ing on them,
since a 404 would reveal that the id exists somewhere. The answer cache key gained `tenant_id`;
without it two tenants asking the same question of same-named documents shared one answer. Job
state and the dead-letter list are tenant-checked on read.

`tests/test_tenancy.py` is the isolation suite (13 tests): both documents embed to the *same*
vector and share a filename, so only the tenant predicate separates them. It asserts dense and
lexical retrieval scoped to A never return a chunk of B even when B's `doc_id` is passed in
explicitly, that delete and job reads across tenants are 404s, and that the cache key differs.
`tests/test_auth.py` covers hashing, token forgery and expiry, role ranking, and the audit log.
Verified end to end against a running API and worker: 35 checks, including that tenant A's
answer contains its own figure and never B's while naming both documents in the request.

Not done: an external IdP (this is a local HS256 issuer, not OIDC/Keycloak), refresh tokens and
revocation, token rotation, and per-tenant rate limits (item 11).

### 6. No container, no CI, no deploy artifact — **DONE**
No Dockerfile, no compose, no pipeline. Model weights (~1.5 GB across bge + mxbai + BLIP) download
from HuggingFace on first use, so a cold pod is minutes of downloading before readiness.

**Fix:** multi-stage Dockerfile with weights baked in, or an init container pulling into a shared
PVC; `docker-compose` for local (api, worker, postgres, redis, qdrant, minio); Helm chart with
separate api/worker deployments, HPA, PDB, resource limits; GitHub Actions running `ruff`,
`mypy --strict`, `pytest --cov` with a coverage floor, `oxlint`, `npm run build`, `pip-audit`, Trivy
image scan, and the eval suite. Terraform for infra.

**Done.** `docker/backend.Dockerfile` is multi-stage (deps -> model bake -> runtime) and produces
one image serving three roles through its entrypoint argument: `api`, `worker`, `migrate`. The API
and the worker being the same artifact is the point -- they share the pipeline code and cannot
drift apart on dependencies. Weights are baked (bge, mxbai, BLIP, docling artifacts, ~1.5 GB) so a
cold replica is ready in seconds rather than after a HuggingFace pull, and `HF_HUB_OFFLINE=1` makes
a missed bake fail loudly instead of quietly reaching for the network at request time.
`docker/frontend.Dockerfile` builds the SPA and serves it from unprivileged nginx, which also
proxies `/api` to the API service -- one origin, so no CORS in a deployed environment and no API
hostname baked into the bundle. Both images run non-root with a read-only root filesystem.

`docker-compose.yml` gained an `app` profile (`docker compose --profile app up -d --build`) with
`migrate`, `api`, `worker` and `frontend`. The profile keeps the documented dev loop -- plain
`docker compose up -d` for shared state, code on the host -- unchanged.

`deploy/helm/sda` has separate API and worker deployments with their own resource envelopes, HPAs
and PDBs, a `pre-install,pre-upgrade` hook Job for `alembic upgrade head`, an ingress, and probes
wired to `/livez` (liveness) and `/health` (readiness) from item 7. Grace periods are asymmetric on
purpose: 120s for the API so in-flight SSE answers drain, 960s for the worker so a 200-page parse
is not killed mid-job. `existingSecret` is the production path, so nothing has to land in
`values.yaml` or in Helm release history. Postgres, Redis and the object store are deliberately not
in the chart -- they are managed services, not workloads this chart should own.

`.github/workflows/ci.yml` runs five jobs: `backend` (ruff, `mypy src`, migrations, `pytest
--cov-fail-under=65` against a real Postgres, Redis and SeaweedFS), `frontend` (oxlint, vite
build), `audit` (pip-audit, Trivy filesystem scan), `chart` (helm lint and template), and `images`
(buildx build of both images with a GHA cache, then a Trivy image scan of each).

Making the gates real needed code changes. `ruff.toml` selects `E,F,I,B,C4,SIM,T20`; the eleven
findings are fixed -- six `zip()` calls gained `strict=True`, which turns a silent truncation into
a loud error on genuinely equal-length pairs, plus a lambda assignment and import ordering. `mypy
src` had eight pre-existing errors and is now clean: `_retry_transient` proves its return by
running the final attempt outside the loop, `_PROVIDERS` is typed as a factory map, the Groq client
handle is `Any` because the SDK's overloads do not cover `stream_options`, and three `Any` returns
are annotated at their source. Coverage is 70% today against a 65% floor.

Building it surfaced four real defects, all fixed. The PyPI `torchvision` that docling pulls in
transitively does not register its ops against a CPU-index `torch`, so every `transformers` import
died on `operator torchvision::nms does not exist` -- both must come from the CPU index, in the
image and on a developer's machine. The base image's pip 23.0.1 mis-normalizes package names on
that index and tries to build `typing_extensions` from source. opencv, also via docling, links
against the X client libraries even headless. And the object store's compose healthcheck had been
failing since it was written -- the S3 root answers `403` unauthenticated and `localhost` resolves
to `::1`, which the listener is not bound to; nothing noticed because nothing depended on it being
healthy until `api` and `worker` did.

Verified end to end against the containerized stack: register, upload, worker ingest (24 chunks
indexed), retrieval, rerank, and a generated answer at High confidence with citations -- all
through the SPA's own origin on :8080, with the embedder and reranker loading from the baked cache
rather than the network. The frontend re-resolves the API per request off the container's own
nameservers, so recreating the API container does not 502 the SPA until nginx restarts; verified
by recreating it.

Not done: `mypy --strict` (the repo runs the non-strict profile, and `tests/` still has 12 errors,
so CI type-checks `src` only), `UP`/pyupgrade in ruff (a repo-wide `Optional[X]` rewrite belongs in
its own commit), a GPU node pool, Terraform, image signing and SBOM (item 16), and eval gating
(item 17 -- CI runs the test suite, not `evaluation/run_eval.py`).

### 7. `/health` is unsafe as a probe — **DONE**
`check_health` (`src/core/health.py`) instantiates a **second** `Embedder()` and a fresh LLM client
on every call (double memory, bypasses the cached deps), runs a real embedding pass, and calls
`all_chunks()` — a full scan of the collection. A k8s liveness probe pointed at it will thrash, then
OOM-kill.

**Fix:** `/livez` returns 200 statically; `/readyz` checks cached dependency handles with a 10s
memoized result and a timeout. Never touch a paid API or do a full scan in a probe.

**Done.** `/livez` is static. `/health` pings Postgres, Redis and the object store only, each in a
worker thread with a 5s timeout, memoized for 10s. Model handles are reported from
`lru_cache.cache_info()` without being constructed, and the provider is never called. Measured
0.35s cold, 0s memoized, against a probe that previously ran a real embedding pass and a live LLM
call per request. Readiness gates on backends alone: models load lazily, and a worker that has not
served a request yet can still serve one.

---

## P1 — Correctness and operational maturity

### 8. Streaming: unbounded threads, no cancellation
`_stream_answer` (`src/api/router.py:118`) spawns a bare daemon thread per query with an unbounded
queue. No concurrency cap (1000 concurrent questions = 1000 threads each running a reranker forward
pass), no per-request timeout, and **no disconnect handling** — close the browser tab and the thread
keeps streaming and billing Gemini tokens to completion.

**Fix:** `anyio.to_thread.run_sync` with a `CapacityLimiter`, or a bounded `ThreadPoolExecutor`; poll
`await request.is_disconnected()` and set a cancel event the answerer checks between stages; bounded
queue for backpressure; hard wall-clock timeout per query; load-shed 503 when saturated.

### 9. Answer cache key omits conversation history, so it serves wrong answers
`_cache_key` (`src/generation/answerer.py:39`) hashes `question | doc_ids | mode | generate` but not
`history`. Ask "What is the revenue?" in two conversations whose condensed standalone questions
differ, and the second gets the first's answer. Include a hash of the condensed standalone question
(or the history window) in the key. Then move to a **semantic cache** (Redis vector similarity /
GPTCache) so paraphrases hit, with a similarity floor and per-tenant namespacing.

### 10. Ingest is not transactional — **PARTLY DONE**
`ingest_document` (`src/api/router.py:63`) saves the manifest, then deletes the replaced doc's
vectors, then embeds and indexes. A crash mid-way leaves a manifest entry with no vectors — or the
old document deleted and the new one absent. Use a state machine (`pending` → `parsed` → `indexed` →
`live`) in Postgres, a transactional outbox for the vector-store write, and flip to `live` only after
the index write is acknowledged. `list_indexed` returns only `live`.

### 11. No rate limiting, quotas, or cost controls
An unauthenticated caller can upload 200-page PDFs in a loop or hammer `/query` and drain your Gemini
quota. Add slowapi or gateway-level limits (Kong/Cloudflare), per-tenant token and spend budgets
enforced **before** the LLM call, monthly cost accounting per tenant from `last_usage`, and a circuit
breaker on the provider.

### 12. Provider failover is boot-time only
`LLM_PROVIDER` is frozen at import (`src/core/config.py:26`), so a Gemini outage is a full outage
until someone edits `.env` and restarts. Put **LiteLLM** (or your own router) in front with an ordered
fallback chain, circuit breaker (`pybreaker`/`purgatory`), per-provider timeouts, and
structured-output retry. `_retry_transient` (`src/generation/client.py:16`) is correct — move it
behind the router so all providers share it. Add prompt caching for the system prompt + repeated
context.

### 13. Observability is one `logging.basicConfig` and a hand-rolled `Trace`
`src/core/tracing.py` is a nice design but only emits a log line. No metrics, no distributed traces,
no error aggregation, no request correlation.

- **OpenTelemetry** with FastAPI/httpx/SQLAlchemy auto-instrumentation; emit `StageRecord`s as real
  spans so retrieval → rerank → generate shows as a waterfall instead of a JSON blob.
- **Prometheus** `/metrics`: p50/p95/p99 per stage, abstain rate, confidence distribution,
  citation-validity rate, tokens and cost per request, cache hit rate, queue depth, retrieval
  top-score histogram. Grafana dashboards + Alertmanager on SLO burn.
- **structlog** JSON logs with `query_id` / `tenant_id` / `trace_id` on every line.
- **Sentry** for exceptions (backend + frontend with source maps).
- **Langfuse** or Arize Phoenix for prompt/response traces, cost attribution, and turning production
  traces into eval datasets. This is the layer that makes RAG debuggable in production and the
  biggest missing piece after persistence.

### 14. Config is read at class-definition time and never validated
`ModelConfig` (`src/core/config.py:26`) evaluates `os.environ.get(...)` as **dataclass field
defaults**, i.e. at import. Nothing can override it afterward (tests, container env injection
ordering), and no value is validated — a mistyped `LLM_PROVIDER` fails deep in `build_client`, a
nonsense `abstain_threshold` fails silently.

**Fix:** `pydantic-settings BaseSettings` with validators, `Literal`/enum types for provider and
mode, per-environment profiles, secrets from a real manager (Vault / AWS Secrets Manager / Doppler)
with rotation. Move tuned thresholds (abstain, support, grounding, confidence edges) out of code into
a **versioned config artifact** behind a feature-flag layer (OpenFeature/Unleash) so retuning and A/B
do not need a deploy.

### 15. Model inference lives in the API process
bge + mxbai reranker + BLIP all load into each API replica — hundreds of MB to GB of RAM per replica,
CPU-bound forward passes on the request path, no batching across concurrent requests.

**Fix:** **HuggingFace TEI** for embedder and reranker (dynamic batching, ONNX/int8, small footprint,
HTTP API); Triton or Ray Serve for BLIP; vLLM/TGI if you self-host generation. Quantize to int8 via
ONNX Runtime or OpenVINO for CPU. API pods become stateless and cheap; GPU nodes scale on their own.

### 16. Security hardening gaps
- CORS hardcoded to `localhost:5173` (`src/api/router.py:35`) — make it env-driven; add CSP, HSTS,
  `X-Content-Type-Options` middleware.
- **Prompt injection**: document text enters the prompt. Add spotlighting (explicit delimiters plus
  "content between markers is data"), keep the citation verifier as the output guard, add an output
  scanner for instruction-following leakage. The cross-encoder grounding check is already a decent
  defense — measure it against an injected-document test set.
- **PII**: uploads go to Gemini. Needs a documented data-processing story, optional PII redaction
  (Microsoft **Presidio**) before egress, and a "no third-party egress" mode (Ollama/vLLM on-prem)
  for regulated customers. Vertex AI with data residency if staying on Google.
- **Untrusted file parsing**: docling + pypdf on arbitrary PDFs is a real attack surface
  (decompression bombs, malformed object graphs, external resource fetches). Parse in a locked-down
  worker: no network egress, seccomp profile, memory/CPU/time limits, non-root, read-only rootfs.
- Supply chain: pin frontend deps (drop `^`), Dependabot/Renovate, `pip-audit`, SBOM (Syft), image
  signing (cosign).
- GDPR erasure: deletion must verifiably purge vector store + lexical index + answer cache + object
  store + logs. Today `ANSWER_CACHE.clear()` covers one process only.

### 17. Evaluation exists but does not gate anything
`evaluation/run_eval.py` is genuinely good — recall@k, citation validity, numeric grounding,
faithfulness, abstain sweep. The problems: it runs manually, results are committed as JSON, and the
golden set rides on a single sample doc.

**Fix:** expand the golden set (multi-hop, table lookups, cross-document conflicts, explicit
**unanswerable negatives** to measure abstain precision/recall); run in CI on every PR against fixed
thresholds and fail on regression; adopt **RAGAS** or DeepEval/promptfoo for standard metric
definitions so numbers are comparable to the literature; LLM-as-judge with a stronger model plus
human spot-checks on a sampled slice; per-commit metrics on a dashboard; close the loop online —
thumbs up/down in the UI, production traces auto-curated into eval datasets, shadow-mode and canary
A/B for retrieval config changes.

### 18. Test suite is thin for the blast radius
676 lines, no coverage gate, no frontend tests, no SSE contract test, no load test.

**Fix:** `pytest-cov` floor in CI; `respx`/VCR cassettes for provider calls; **hypothesis** for
chunker invariants (spans within parent, no lost characters, offsets round-trip); **schemathesis**
fuzzing the OpenAPI schema; **k6** or Locust asserting p95 SLOs under concurrent ingest+query;
**Vitest** + React Testing Library for hooks; **Playwright** E2E for upload → ask → citation-click.

---

## P2 — Product and retrieval-quality depth

### 19. Retrieval / ML roadmap
- **OCR is missing entirely** — scanned PDFs are rejected outright (`src/core/errors.py:18`). That is
  a large fraction of real enterprise documents. Add Surya or PaddleOCR (or Azure Document
  Intelligence) as a fallback when the text layer is empty.
- **Embeddings**: evaluate `bge-m3` (dense + sparse + multi-vector in one model, multilingual) or
  Voyage-3 / embed-v4. Matryoshka truncation + int8 quantization for a cheap ANN pass with
  full-precision rescoring.
- **Late interaction**: ColBERT/PLAID or SPLADE gives lexical precision without a separate BM25
  index — which also deletes the worst scaling problem (#1).
- **Reranking**: mxbai-base is fine; benchmark `bge-reranker-v2-m3` and Cohere Rerank 3.5 against the
  golden set, then distill the winner for latency.
- **Contextual retrieval**: prepend an LLM-generated document-context sentence to each chunk before
  embedding (the Anthropic technique) — typically a large recall win for one cheap pass at ingest.
- **RAPTOR / hierarchical summaries** so "what is this document about" works; parent-child chunking
  alone cannot answer it.
- **Agentic retrieval**: retrieve → critique → re-retrieve loop for multi-hop questions, with a
  budget cap. `hyde` is present but off and query expansion exists — make mode selection a learned or
  heuristic router on query intent instead of a user dropdown.
- **Tables**: docling gives structure; preserve it and route table questions to table-QA or
  text-to-SQL instead of flattening to prose.
- **GraphRAG** for cross-document entity questions, once the corpus justifies it.

### 20. API design
No `/v1` prefix, no pagination on `/documents`, no RFC 9457 `problem+json` errors, no idempotency
keys, no `ETag`s, no bulk ingest, no non-streaming JSON variant of `/query` for programmatic clients,
no webhooks for ingest completion. Add it before external consumers exist — versioning after the fact
is the expensive kind of migration.

### 21. Frontend
No error boundary, no SSE reconnect/backoff (raw fetch stream), no auth UI, no a11y audit, no i18n,
no bundle budget, no source maps wired to error tracking, no analytics. Add TanStack Query for server
state, Zod for response validation at the boundary, and consider TypeScript — `@types/react` is
installed but the app is `.jsx`, so you pay for types and get none.

### 22. Data lifecycle
`ingest_version` is a hash with no re-index job behind it — change the chunker and old docs silently
vanish from `list_indexed` with no migration. Add a backfill/reindex worker, blue-green collection
with an alias for zero-downtime reindex, backup plus **restore drills** (an untested backup is not a
backup), retention policies, soft delete with a purge window, document versioning.

### 23. Engineering process
ADRs for the load-bearing decisions (parent-child chunking, RRF fusion, abstain thresholds, provider
choice), SLOs with error budgets (p95 answer latency, abstain-rate ceiling, citation-validity floor),
runbooks for the top five alerts, on-call rotation, graceful shutdown that drains in-flight SSE
streams, 12-factor compliance throughout.

---

## Suggested order

1. ~~**Postgres + S3 + Redis** — kill process-local state (unblocks everything)~~ **done**
2. ~~**pgvector or Qdrant** — retire embedded Chroma and in-memory BM25~~ **done** (pgvector)
3. ~~**Async ingest workers** — get parsing off the request path~~ **done** (arq)
4. ~~**Auth + tenancy** — with the cross-tenant isolation test~~ **done** (local JWT)
5. ~~**Docker + CI + Helm** — reproducible deploys~~ **done**
6. **OpenTelemetry + Prometheus + Langfuse** — so it can be seen
7. **Eval gating in CI** — so quality cannot silently regress
8. **TEI inference service**, then retrieval-quality work against gated evals

Steps 1-5 turn this from a prototype into a service, and are done. 6-7 let a team own it. 8 and the P2 list are
where ML differentiation compounds — and they are only safe after 7, because without gated evals
every retrieval change is a coin flip.
