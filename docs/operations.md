# Operations

Deployment, scaling, configuration and operational behaviour. For how to run the project locally,
see [setup.md](setup.md); for the internals, see [architecture.md](architecture.md).

## Inference service

Embedding and reranking are the two CPU-bound forward passes on the request path. By default they
run in-process, which is simplest and is what the dev loop uses. Setting either URL moves that
pass onto a [HuggingFace TEI](https://github.com/huggingface/text-embeddings-inference) service
instead, which batches across concurrent requests and scales on its own:

```bash
docker compose --profile tei up -d
export TEI_EMBED_URL=http://127.0.0.1:8081
export TEI_RERANK_URL=http://127.0.0.1:8082
```

The models are the ones the in-process backend loads, and TEI applies the same sigmoid to a
`num_labels=1` cross-encoder, so scores stay on the scale every tuned threshold assumes -- the
backends are interchangeable without a retune. `GET /health` gains a `tei_embed` / `tei_rerank`
probe when a URL is set: unlike lazily loaded local weights, a remote dependency being down means
the replica cannot serve, so it gates readiness.

In the chart, `tei.embed.enabled` / `tei.rerank.enabled` add a deployment, a service and an
optional weights PVC each, and wire the two URLs into the backend ConfigMap. This is the piece
that makes API pods cheap and stateless: the weights, the memory and the GPU node pool (swap
`teiImage.tag` for a CUDA tag) belong to a workload that scales on inference load rather than on
request count.

## Deploying to Kubernetes

`deploy/helm/sda` is a Helm chart for the three workloads. Postgres, Redis and the object store
are not in the chart -- point at managed services through `config` and `secrets`:

```bash
helm upgrade --install sda deploy/helm/sda   --set ingress.host=sda.example.com   --set existingSecret=sda-secrets
```

- API and worker scale independently: the API is IO-bound, the worker is CPU-bound and holds
  model weights through a parse. Separate deployments, resource envelopes, HPAs and PDBs.
- `alembic upgrade head` runs as a `pre-install,pre-upgrade` hook Job, so the schema is never
  behind the code that reads it.
- Probes are split by purpose: `/livez` for liveness (static),
  `/health` for readiness (backend pings, memoized, no model construction and no provider call).
- The worker's grace period is 16 minutes -- long enough that a 200-page parse is not torn out
  from under the job -- and the API's is 2 minutes so in-flight SSE answers drain.
- `existingSecret` is the production path: point at a Secret written by an external secrets
  operator so nothing lands in `values.yaml` or in Helm release history.

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request:

| Job | Gates on |
|---|---|
| `backend` | `ruff check`, `mypy src`, `alembic upgrade head`, `pytest --cov-fail-under=75` against real Postgres/Redis/SeaweedFS |
| `eval` | The `retrieval` golden-set profile, so retrieval quality cannot regress silently |
| `frontend` | `oxlint`, `tsc --noEmit`, Vitest, `vite build` including the gzipped bundle budget |
| `audit` | `pip-audit` on `requirements.txt`, Trivy filesystem scan (HIGH/CRITICAL) |
| `chart` | `helm lint` and `helm template` |
| `images` | Builds both images with buildx cache, then Trivy-scans each |

`.github/workflows/supply-chain.yml` runs on every push to `main` and on `v*` tags. It publishes
both images to GHCR, signs them with keyless cosign, builds an SBOM, attaches it to the image, and
scans it with Grype. Findings that are fixed upstream but unreachable from this image are listed,
each with a reason, in `.grype.yaml`.

`pyproject.toml` (`[tool.ruff.lint]`) selects `E,F,I,B,C4,SIM,T20`. `UP` (pyupgrade) is deliberately excluded for now:
turning it on would make this a repo-wide `Optional[X]` to `X | None` rewrite, which belongs in
its own commit.

## Observability

Every exporter is opt-in: with nothing configured the processes still record spans and serve
metrics, they just ship nothing outward. Bring up a local stack and point the app at it:

```bash
docker compose --profile obs up -d
```

Jaeger on http://localhost:16686, Prometheus on http://localhost:9090, Grafana (anonymous admin)
on http://localhost:3001 with the **SDA overview** dashboard already provisioned. Set
`OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318` in `.env` and restart the API and worker.

| Signal | Where |
|---|---|
| Traces | One span tree per request: `POST /query` -> `answer` -> `searching` / `reranking` / `assembling` / `generating`, with SQL spans nested inside the stage that issued them. The ingest worker emits its own `ingest` trace. |
| Metrics | `GET /metrics` on the API (aggregated across its uvicorn workers), port 9100 on the worker. Stage and answer latency, abstain rate, confidence distribution, citation validity, retrieval top score, tokens and USD spend per model, cache hit rate, ingest outcomes and queue depth. |
| Logs | JSON on stdout, every line carrying `request_id`, `query_id`, `tenant_id` (or `job_id`/`doc_id` in the worker) plus `trace_id` and `span_id`, so a log line and its span find each other. |
| Prompt traces | Langfuse reads the same span tree: set `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` and each query arrives with its prompt, retrieved context, usage and cost. |
| Exceptions | `SENTRY_DSN`, if set. |
| Alerts | `deploy/helm/sda/files/alerts.yml`, loaded by the compose Prometheus and rendered as a `PrometheusRule` by the chart -- one file, both paths. |

The two alerts worth naming: citation validity below 90% and an abstain rate over 40%. Neither
shows up in an HTTP error rate, because a confidently wrong answer and a service that abstains on
everything both return 200.

## Accounts and tenancy

Documents, chunks, answers and the audit log are scoped to a workspace (tenant). A caller's tenant
is read from their bearer token, never from a request parameter, and the tenant filter is applied
inside the retrieval layer, so passing another workspace's `doc_id` returns nothing rather than
erroring. Roles are ordered:

| Role | Can |
|---|---|
| `viewer` | List documents, ask questions |
| `editor` | ...and upload and delete documents |
| `admin` | ...and create users, read the audit log and the dead-letter queue |

`POST /auth/register` creates a workspace and its first admin; that admin adds the rest with
`POST /auth/users`. Uploads, queries and deletions are written to `audit_log` and readable by the
workspace's admins at `GET /audit`. `tests/test_tenancy.py` asserts that no retrieval path,
delete, job read or cache entry crosses a workspace boundary.

## API surface

| Method | Path | Role | Purpose |
|---|---|---|---|
| `POST` | `/auth/register` | — | Create a workspace and its first admin |
| `POST` | `/auth/login` | — | Exchange credentials for a bearer token |
| `GET` | `/auth/me` | viewer | Current principal and role |
| `GET` `POST` | `/auth/users` | admin | List and create workspace users |
| `DELETE` | `/auth/users/{user_id}/data` | admin | Right-to-erasure, with a verified receipt |
| `POST` | `/ingest` | editor | Queue one upload; returns 202 and a `job_id` |
| `POST` | `/ingest/batch` | editor | Queue several uploads in one request |
| `GET` | `/jobs/{job_id}` | viewer | Ingest job state |
| `GET` | `/jobs/{job_id}/events` | viewer | Ingest progress as SSE |
| `GET` | `/jobs/dead-letters` | admin | Jobs that exhausted their retries |
| `GET` | `/documents` | viewer | Paginated document list, ETag-cached |
| `GET` | `/documents/{doc_id}/versions` | viewer | Version history for a filename |
| `DELETE` | `/documents/{doc_id}` | editor | Soft delete: unanswerable at once, recoverable |
| `POST` | `/documents/{doc_id}/restore` | editor | Undo a soft delete inside the window |
| `POST` | `/query` | viewer | Ask a question; SSE by default |
| `GET` `POST` | `/webhooks` | admin | List and register ingest callbacks |
| `DELETE` | `/webhooks/{webhook_id}` | admin | Remove a subscription |
| `GET` | `/audit` | admin | Workspace audit log |
| `GET` | `/admin/index` | admin + operator | Live and pending index builds |
| `POST` | `/admin/index/reindex` | admin + operator | Build a new index, then cut over |
| `POST` | `/admin/index/rollback` | admin + operator | Return the alias to the previous build |
| `POST` | `/admin/retention/sweep` | admin + operator | Run the retention purge now |
| `GET` | `/livez` `/health` `/config` `/metrics` | — | Probes, client bootstrap, scrape endpoint |

Conventions that apply to all of it:

- **Errors** are RFC 9457 `application/problem+json`, with a stable `type` URI a client can branch
  on instead of matching on prose.
- **`Idempotency-Key`** on unsafe requests, shared across replicas through Redis. A replay of a
  finished request replays its response; a replay while the first is still in flight is a 409
  rather than a second execution; the same key with a different body is a 422.
- **Pagination** is keyset, not offset: follow `next_cursor`. A document inserted mid-page shifts
  every offset and makes an offset client skip a row; `(created_at, doc_id) > last` does not.
- **`ETag` / `If-None-Match`** on `/documents`, which the SPA re-reads after every ingest and
  reconnect. An unchanged list costs a 304 with no body.
- **Webhooks** are signed per subscription, retried with backoff, and disabled after repeated
  failure. A webhook URL is caller-controlled input this service then fetches, so registration
  validates the target and private network ranges are rejected unless explicitly allowed.
- **Overload** is a 503 with `Retry-After`, never a silent queue.

## Rate limits and spend budgets

Two independent controls (`src/core/limits.py`), both in Redis so every replica enforces one
number rather than one each. Rate limits are per-tenant token buckets on the query, ingest and
auth paths — they bound how fast the service is asked to work and refill on their own; the auth
path buckets by client address, since the caller has no tenant yet. Budgets are daily counters of
generation tokens and USD — they bound what a tenant can spend, and only a new day clears them.
Budgets of 0 are off, which is the default; a breach answers 429 with the retryable reason.

## Index rebuilds

`ingest_version` is a hash of the chunker settings and the embedder name. Reads do not use it
directly — they follow an alias in Postgres that only a cutover moves. That indirection is what
makes changing a chunk size safe: `POST /admin/index/reindex` builds every live document under the
new version while the old build keeps serving, then moves the alias; `POST /admin/index/rollback`
moves it back. Without it, a configuration change silently orphans the entire corpus.

## Data lifecycle and privacy

- **Soft delete.** Chunks are removed immediately, because a deleted document must stop being
  answerable the moment it is deleted. The row and the stored bytes outlive the index by
  `RETENTION_SOFT_DELETE_DAYS`, so a delete made in error is recoverable via
  `POST /documents/{doc_id}/restore`.
- **Versions.** Replacing a file by name no longer destroys what it replaced: the old row points
  at the new one, so "which version answered that question last month" has an answer.
- **Retention sweep.** A periodic purge clears soft-deleted documents, superseded versions, old
  index builds and (optionally) audit rows past their windows. Two metrics track work the sweep
  still owes: `sda_retention_overdue_documents` and `sda_reindex_pending_documents`.
- **Erasure.** `DELETE /auth/users/{user_id}/data` walks Postgres, the object store and Redis,
  then re-reads each one and returns a receipt counting what is left. Erasure that cannot be
  verified is not erasure.
- **PII in telemetry.** `src/core/redaction.py` scrubs log messages, span attributes and the
  prompt captures shipped to Langfuse — the text that gets read when something is already wrong.

## Security hardening

| Control | Where |
|---|---|
| Output-side injection scan; a hit abstains rather than editing the answer | `src/trust/scanner.py`, `SCAN_OUTPUT` |
| PII redaction over logs, spans and prompt captures | `src/core/redaction.py`, `REDACT_PII` |
| Security headers, CSP, optional HSTS, explicit CORS allowlist | `src/api/router.py`, `CSP_CONNECT_SRC` / `HSTS_MAX_AGE_S` |
| Deployment-level gate on admin index and retention routes | `OPERATOR_TOKEN` |
| Outbound egress block for air-gapped deploys | `NO_EGRESS` |
| Non-root containers with a read-only root filesystem | `docker/*.Dockerfile` |
| Signed images with an attached SBOM | `.github/workflows/supply-chain.yml` |
| Dependency and image scanning on every pull request | `.github/workflows/ci.yml` |

Still open: secrets come from the environment rather than a managed secret store, and PII
detection is pattern-based rather than a model such as Presidio.

## Backup and restore

`deploy/backup/` dumps Postgres with `pg_dump --format=custom` and mirrors the object store key
for key, both read through the same configuration the application uses. Redis is deliberately not
backed up: it holds cache, job state and rate-limit windows, all derived or ephemeral. The targets
are RPO 24h and RTO 1h, and they are met by the drill passing rather than by the scripts existing
— a weekly GitHub Actions job seeds a corpus, backs it up, destroys both stores, restores, and
checks the corpus is answerable again. Full rationale in
[deploy/backup/README.md](../deploy/backup/README.md).

## Load testing

`tests/load/query.js` is a k6 profile for the answer path, kept out of CI because the numbers only
mean something on hardware you intend to deploy on:

```bash
k6 run -e BASE_URL=http://127.0.0.1:8000 -e EMAIL=... -e PASSWORD=... tests/load/query.js
```

It checks the shape of overload rather than a throughput number: that the API sheds with 503
instead of queueing, and that the answers it does serve stay inside their latency budget while it
does.

## Frontend resilience

The SPA is a separate deployable, so it is hardened as one:

- **One HTTP boundary.** `api.js` is the only module that talks to the API, and every response is
  validated against a Zod schema there. The SPA and the API deploy separately, so a renamed field
  should surface as one named error at the boundary rather than as `undefined` three components
  deep, where the stack trace no longer says which call produced it.
- **Error boundary.** A render error anywhere below it shows a recoverable state instead of the
  blank page React leaves by default, which is indistinguishable from an outage to the person
  looking at it.
- **Translation.** Every user-visible string goes through a small catalogue (`src/i18n.js`), so
  adding a locale is a file rather than a sweep through the components.
- **Bundle budget.** `npm run build` fails if the gzipped first-visit payload grows past its
  budget. Error reporting and analytics are opt-in and lazily loaded, so a default build ships
  neither — otherwise the budget quietly stops meaning anything.
- **Cold-start handling.** `useBootstrap` retries health forever: a first boot loads models and can
  take a minute, and a client that gives up in ten seconds reports an outage that is not one.

## Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | Generation backend: `gemini`, `groq`, `ollama`, or `none` (retrieval only) |
| `GEMINI_API_KEY` | _(empty)_ | Required when `LLM_PROVIDER=gemini` |
| `GEMINI_MODEL` | `gemini-3.6-flash` | Gemini model name |
| `GROQ_API_KEY` | _(empty)_ | Required when `LLM_PROVIDER=groq` |
| `GROQ_MODEL` | `openai/gpt-oss-120b` | Groq model name |
| `OLLAMA_HOST` | `127.0.0.1:11435` | Ollama server address, used when `LLM_PROVIDER=ollama` |
| `OLLAMA_MODEL` | `qwen3:8b` | Ollama model name |
| `DATABASE_URL` | _(required)_ | Postgres DSN, e.g. `postgresql+psycopg://sda:sda@127.0.0.1:5433/sda` |
| `REDIS_URL` | _(required)_ | Redis DSN for the answer cache and ingest locks |
| `TEI_EMBED_URL` / `TEI_RERANK_URL` | _(empty)_ | Serve embeddings/reranking from a TEI service instead of in-process weights |
| `TEI_TIMEOUT_S` / `TEI_MAX_CONNECTIONS` | `60` / `16` | TEI request timeout and connection pool size |
| `S3_ENDPOINT` / `S3_BUCKET` | _(required)_ | Object store for raw uploads and parent blobs |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | _(required)_ | Object store credentials |
| `S3_REGION` | `us-east-1` | Object store region |
| `DB_POOL_SIZE` / `DB_POOL_MAX_OVERFLOW` | `5` / `10` | SQLAlchemy connection pool sizing |
| `JWT_SECRET` | _(required)_ | HS256 signing key for access tokens |
| `ACCESS_TOKEN_TTL_S` | `43200` | Access token lifetime in seconds |
| `INGEST_CONCURRENCY` | `1` | Ingest jobs in flight per worker process (CPU-bound; keep low) |
| `API_WORKERS` | `4` | uvicorn workers per API container; container images only |
| `DEPLOY_ENV` | `development` | Environment label on spans, metrics and Sentry events |
| `LOG_JSON` / `LOG_LEVEL` | `1` / `INFO` | JSON logs (`0` for the console renderer) and level |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(empty)_ | OTLP/HTTP base URL, e.g. `http://127.0.0.1:4318`; empty disables trace export |
| `OTEL_TRACE_SAMPLE_RATIO` | `1.0` | Head sampling ratio, parent-based |
| `OTEL_DB_SPANS` | `1` | Emit a span per SQL statement |
| `METRICS_TOKEN` | _(empty)_ | Bearer token for `GET /metrics`; empty leaves the scrape endpoint open |
| `WORKER_METRICS_PORT` | `9100` | Port the ingest worker serves `/metrics` on |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | _(empty)_ | Enable Langfuse prompt traces |
| `LANGFUSE_HOST` | `https://cloud.langfuse.com` | Langfuse endpoint |
| `SENTRY_DSN` / `SENTRY_TRACES_SAMPLE_RATE` | _(empty)_ / `0.0` | Enable Sentry error reporting, and its own trace sampling |
| `OTEL_SERVICE_NAME` | `sda` | Service name on spans and metrics |
| `LANGFUSE_SAMPLE_RATE` | `1.0` | Fraction of queries shipped to Langfuse |
| `MODEL_PRICING_JSON` | _(empty)_ | Override generation pricing without a deploy: `{"model": [usd_per_mtok_in, usd_per_mtok_out]}` |
| `HF_TOKEN` | _(empty)_ | Optional; only needed for gated HF models or to avoid anonymous rate limits on embedder/reranker downloads |

**Request handling and limits**

| Variable | Default | Purpose |
|---|---|---|
| `QUERY_CONCURRENCY` | `8` | Answers in flight per replica; past this the API answers 503 |
| `QUERY_TIMEOUT_S` | `180` | Hard bound on one answer |
| `API_PAGE_SIZE` / `API_MAX_PAGE_SIZE` | `50` / `200` | Default and maximum page size for cursor-paginated collections |
| `IDEMPOTENCY_TTL_S` | `86400` | How long an `Idempotency-Key` stays replayable |
| `MAX_BULK_FILES` | `10` | Files accepted by `POST /ingest/batch` |
| `RATE_LIMIT_ENABLED` | `1` | Master switch for the token buckets |
| `QUERY_RATE_PER_MINUTE` / `QUERY_BURST` | `30` / `10` | Per-tenant query bucket |
| `INGEST_RATE_PER_MINUTE` / `INGEST_BURST` | `6` / `3` | Per-tenant ingest bucket |
| `AUTH_RATE_PER_MINUTE` / `AUTH_BURST` | `10` / `5` | Auth bucket, keyed by client address |
| `DAILY_TOKEN_QUOTA` / `DAILY_COST_USD` | `0` / `0` | Daily generation budgets per tenant; `0` disables |

**Generation failover**

| Variable | Default | Purpose |
|---|---|---|
| `LLM_FALLBACK_CHAIN` | _(empty)_ | Providers to try after the primary, e.g. `groq,ollama` |
| `LLM_BREAKER_FAILURES` / `LLM_BREAKER_COOLDOWN_S` | `3` / `60` | Consecutive failures that open a provider's circuit, and how long it stays open |

**Thresholds and flags**

| Variable | Default | Purpose |
|---|---|---|
| `THRESHOLDS_VERSION` | `v1` | Which `config/thresholds/<version>.yaml` this deploy runs |
| `THRESHOLD_OVERRIDES_JSON` | _(empty)_ | Per-deploy overrides by dotted path, e.g. `{"trust.abstain_threshold": 0.35}` |

**Ingestion**

| Variable | Default | Purpose |
|---|---|---|
| `OCR_ENABLED` | `1` | Read scanned PDFs with OCR instead of rejecting them |
| `OCR_MAX_PAGES` | `50` | Page cap on an OCR run; OCR measures ~9s per page on CPU |

**Retention and lifecycle**

| Variable | Default | Purpose |
|---|---|---|
| `RETENTION_ENABLED` | `1` | Run the purge sweep |
| `RETENTION_SOFT_DELETE_DAYS` | `30` | Window in which a deleted document can be restored |
| `RETENTION_SUPERSEDED_DAYS` | `30` | How long a replaced version is kept |
| `RETENTION_OLD_INDEX_DAYS` | `7` | How long a superseded index build is kept for rollback |
| `RETENTION_AUDIT_DAYS` | `0` | Audit log retention; `0` keeps it indefinitely |

**Webhooks**

| Variable | Default | Purpose |
|---|---|---|
| `WEBHOOKS_ENABLED` | `1` | Accept subscriptions and deliver callbacks |
| `WEBHOOKS_MAX_PER_TENANT` | `5` | Subscriptions one workspace may hold |
| `WEBHOOK_TIMEOUT_S` / `WEBHOOK_MAX_ATTEMPTS` / `WEBHOOK_BACKOFF_S` | `5` / `3` / `1` | Delivery timeout, retries and backoff |
| `WEBHOOK_MAX_FAILURES` | `20` | Consecutive failures after which a subscription is disabled |
| `WEBHOOK_ALLOW_PRIVATE_TARGETS` | `0` | Permit private-range callback URLs; leave off outside testing |

**Security**

| Variable | Default | Purpose |
|---|---|---|
| `CORS_ALLOW_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | Browser origins allowed to call the API |
| `CSP_CONNECT_SRC` | `'self'` | `connect-src` for the SPA's CSP when the API is on another origin |
| `HSTS_MAX_AGE_S` | `0` | Enable HSTS behind TLS; `0` omits the header |
| `SCAN_OUTPUT` | `1` | Output-side injection scan on generated answers |
| `REDACT_PII` | `1` | Scrub PII from logs, spans and prompt captures |
| `NO_EGRESS` | `0` | Block outbound HTTP for air-gapped deploys |
| `OPERATOR_TOKEN` | _(empty)_ | Deployment-level credential for the admin index and retention routes; unset disables them |
| `SPOTLIGHT_DOCUMENTS` | `1` | Allow the UI to scope a question to selected documents |

**Frontend build**

| Variable | Default | Purpose |
|---|---|---|
| `VITE_SENTRY_DSN` | _(empty)_ | Enable SPA error reporting; the SDK is lazily loaded and excluded from the bundle budget |
| `VITE_ANALYTICS_ENDPOINT` | _(empty)_ | Endpoint for web-vitals and product events |
| `VITE_RELEASE` / `VITE_DEPLOY_ENV` | `dev` / `development` | Release and environment labels on SPA telemetry |
