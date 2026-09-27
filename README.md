# Smart Document Assistant

A RAG (Retrieval-Augmented Generation) application for uploading PDF and TXT documents, asking questions, and getting cited answers with calibrated confidence scores. Embeddings, vector store, and reranking run locally; generation defaults to a free-tier cloud API (see **Provider Choice** below), with a fully-offline Ollama fallback.

## Problem Understanding

The task is to build a document Q&A system that:
1. Accepts uploaded documents (PDF, TXT)
2. Extracts and indexes their content for retrieval
3. Answers user questions grounded in the uploaded documents
4. Provides source citations so the user can verify claims
5. Refuses to answer when the documents don't contain the needed information, rather than hallucinating

The core challenge is **grounding**: every claim in the answer must trace back to a specific passage, and the system must know when it doesn't know.

## Architecture

```
User
  |
  v
+---------------------------+
|  React UI (frontend/)     |  Upload / Ask / View answers
|  Vite SPA, React hooks    |
|  index.css, components/   |
+---------------------------+
  |                |
  v                v
+---------------------------+
|  FastAPI (src/api/)       |  REST endpoints
|  router.py, schemas.py    |
+---------------------------+
  |                |
  v                v
+------------+  +---------------------+
| Ingestion  |  | Answer pipeline     |
| pipeline   |  | (orchestrated by    |
| (src/      |  |  answerer.py)       |
| ingestion/)|  +---------------------+
  |                |         |        |
  v                v         v        v
+--------+  +---------+ +--------+ +--------+
| Parsers|  | Hybrid  | | Trust  | | LLM    |
| docling|  | search  | | layer  | | Gemini/|
| pypdf  |  | dense + | | abstain| | Groq/  |
| BLIP*  |  | tsvec + | | cite   | | Ollama |
+--------+  | rerank  | | confid.| +--------+
  |         +---------+ +--------+
  v              |
+--------+  +-------------------+
| Chunker|  | Postgres          |
| parent/|  | pgvector + tsvector
| child  |  | + docs/chunks     |
+--------+  +-------------------+
  |              |
  v              v
+--------+  +---------+
| MinIO  |  | Redis   |
| raw +  |  | cache + |
| parents|  | locks   |
+--------+  +---------+
```

**Data flow:**

1. **Ingest**: Upload -> parse (docling for complex PDFs, pypdf for simple ones) -> structure-aware chunking (parent/child, 800/200 tokens) -> embed with `bge-base-en-v1.5` -> chunks and vectors to Postgres/pgvector, raw file and parent blobs to S3/MinIO
2. **Retrieve**: Question -> embed -> hybrid search (dense cosine via pgvector + lexical via Postgres `tsvector`, reciprocal rank fusion) -> optional cross-encoder reranking (default mode) -> top-k context assembly with token budget
3. **Generate**: Abstention gate (is there enough evidence?) -> prompt with source blocks (injection-hardened) -> LLM answers in plain text with inline `[n]` citation markers -> citation validation -> confidence scoring
4. **Present**: Answer with inline citations, source cards with highlighted passages, trace viewer showing each stage's timing and data

### Package structure

```
frontend/                Presentation layer (React SPA)
  index.html             Entrypoint
  vite.config.js         Proxy config
  src/
    App.jsx              Main layout and state
    api.js               REST API client
    hooks/               State management
    components/          UI components

src/                     Business logic (UI-agnostic, testable)
  api/                   FastAPI REST layer
  auth/                  Principal, password hashing, JWT, user service, audit log
  core/                  Config, errors, tracing, metrics, structured logs, OTel setup
  ingestion/             Parse -> chunk -> index pipeline
  retrieval/             Embedder, pgvector store, tsvector lexical index, hybrid search, reranker
  generation/            LLM client, prompt templates, answer orchestration
  trust/                 Abstention gate, citation validation, confidence scoring
  storage/               Postgres engine/session, ORM models, object store, Redis cache and locks

migrations/              Alembic schema migrations
docker-compose.yml       Postgres (pgvector), Redis, object store; app and obs profiles
deploy/observability/    Prometheus scrape config, Grafana datasources and dashboard
deploy/helm/sda/         Helm chart (API, worker, frontend, migrations, ServiceMonitor, alerts)

evaluation/              Golden set (33 items) + metrics runner
data/sample_docs/        Three real public-domain U.S. government documents
```

## Technology Choices

| Component | Choice | Why |
|---|---|---|
| **LLM** | Gemini (`gemini-3.6-flash`) by default; Groq or Ollama `qwen3:8b` as swappable providers | Free-tier cloud inference is far faster than CPU-only local generation; see **Provider Choice** below. Answers are plain text with inline `[n]` citation markers, parsed by `prompts.parse_citations` — not schema-enforced JSON. |
| **Embeddings** | `BAAI/bge-base-en-v1.5` | Runs locally on CPU, strong retrieval quality for its size, well-tested for semantic search. |
| **Reranker** | `mxbai-rerank-base-v1` | Cross-encoder precision pass; used as a trust signal for confidence/citation scoring, and for reranking retrieval results in the default mode. |
| **Vector store** | Postgres + pgvector (HNSW, cosine) | One transactional store for metadata and vectors, shared by every API worker. Replaces embedded ChromaDB, which was single-writer, per-process and unreplicable. |
| **Lexical search** | Postgres `tsvector` + GIN | Complements dense search for keyword-heavy queries (policy numbers, proper nouns). Replaces in-process BM25, whose corpus lived in one worker's RAM. Ranking is `ts_rank_cd`; fusion is rank-based, so the change of scale is immaterial. |
| **System of record** | Postgres (`documents`, `chunks`) via SQLAlchemy 2.0 + Alembic | Replaces `manifest.json`, which was a read-modify-write with no lock. |
| **Blob storage** | S3/MinIO | Raw uploads and parent-chunk payloads. Survives a pod restart and is visible to every replica. |
| **Health probes** | `/livez` static; `/health` pings backends only, memoized 10s | Safe for a k8s probe: no embedding pass, no provider call, no collection scan. |
| **Cache and locks** | Redis | Answer cache and parent-payload cache are shared, so invalidation on ingest reaches every worker; the ingest lock serializes replace-by-filename across processes. |
| **PDF parsing** | docling + pypdf | docling handles complex layouts (tables, figures, sections); pypdf is a fast path for simple text PDFs, saving 3-10s per file. |
| **Figure captioning** | BLIP (`blip-image-captioning-base`) | `parsers.FigureCaptioner` runs behind the same lazy-singleton pattern as the embedder; the `/ingest` route constructs one and every extracted figure gets indexed as `[Figure, p.N: <caption>]` instead of a bare placeholder. |
| **UI** | React + Vite | Fast, modern SPA with custom CSS modules. Replaces the older Streamlit prototype. |
| **Chunking** | Parent/child (800/200 tokens) | Children are sized to the embedder's token window; parents provide full context to the LLM. Structure-aware splitting preserves section boundaries. |

**Trade-offs:**
- **Cloud LLM vs. fully local**: Faster generation and no CPU inference cost, at the price of needing a free-tier API key and network access. See **Provider Choice**.
- **Hybrid search vs. dense-only**: Adds ~2ms but catches keyword queries that dense search misses (e.g., specific section numbers).
- **Cross-encoder reranking**: The default mode (`hybrid_rerank`). Adds latency on CPU; `hybrid` (no rerank) and `dense` are available for faster, lower-precision answers.

## Provider Choice

Generation is pluggable via `LLM_PROVIDER` (`src/generation/client.py`): `gemini` (default), `groq`, `ollama`, or `none`.

The default is a cloud API, not fully-offline local inference, because CPU-only generation with a model capable enough to follow citation instructions reliably is slow (multi-second per answer) on typical grading hardware. Gemini and Groq both have generous free tiers that need only a key pasted into `.env` — no payment method, no account beyond the API signup. `ollama` remains available as a genuinely offline fallback (`qwen3:8b`) for anyone who'd rather not use a cloud key at all; set `LLM_PROVIDER=ollama` and follow the Ollama setup steps below.

## How to Run

### Prerequisites
- Python 3.10+
- Docker (for Postgres, Redis and MinIO — the service has no local-disk fallback)
- A free API key from [Google AI Studio](https://aistudio.google.com/apikey) (default provider) — or [Ollama](https://ollama.com) installed if you'd rather run fully offline

### Setup

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/Mac

# 2. Install CPU-only PyTorch (avoids pulling a ~2GB CUDA build). torchvision from the same
#    index: docling pulls it in, and PyPI's build fails to register its ops against a CPU torch.
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy the environment file and add your key
copy .env.example .env        # Windows
# cp .env.example .env        # Linux/Mac
# edit .env and set GEMINI_API_KEY (or switch LLM_PROVIDER=ollama, see Provider Choice)
# the DATABASE_URL / REDIS_URL / S3_* defaults already match docker-compose.yml
# set JWT_SECRET -- the API will not start without it:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"

# 5. Start the shared state backends and apply the schema
docker compose up -d
alembic upgrade head

# 6. Run the FastAPI backend (now safe to run with multiple workers)
uvicorn src.api.router:app --host 127.0.0.1 --port 8000 --workers 4

# 7. In a second terminal, start the ingest worker (uploads are queued, not parsed in the API)
python -m arq src.ingestion.worker.WorkerSettings

# 8. In a third terminal, start the React frontend
cd frontend
npm install
npm run dev
```

If using `LLM_PROVIDER=ollama` instead, pull the model and start a project-scoped server before step 5:

```bash
set OLLAMA_HOST=127.0.0.1:11435
set OLLAMA_MODELS=data\models\ollama
ollama pull qwen3:8b
ollama serve
# (keep this terminal open)
```

The app opens at `http://localhost:5173`. Create a workspace on the sign-in screen (the first user
of a workspace is its admin), then upload a PDF or TXT file, wait for indexing, and ask a question.

### Running in containers

The dev loop above keeps the code on the host. To run the whole thing in containers instead:

```bash
docker compose --profile app up -d --build
```

That builds two images from `docker/`, applies migrations as a one-shot `migrate` service, then
starts the API, the ingest worker and an nginx-served SPA on `http://localhost:8080`. The API and
the worker are the *same* image with a different entrypoint argument (`api` / `worker` /
`migrate`), so they cannot drift apart on dependencies.

The backend image bakes the model weights in: bge, mxbai, BLIP and the docling artifacts are
~1.5 GB, and pulling them at boot would mean minutes of downloading before a fresh replica is
ready. `HF_HUB_OFFLINE=1` in the image makes a missed bake fail loudly instead of silently
reaching for HuggingFace at runtime. The first build is therefore slow; layers cache afterwards.

Both images run as a non-root user with a read-only root filesystem.

### Deploying to Kubernetes

`deploy/helm/sda` is a Helm chart for the three workloads. Postgres, Redis and the object store
are not in the chart -- point at managed services through `config` and `secrets`:

```bash
helm upgrade --install sda deploy/helm/sda   --set ingress.host=sda.example.com   --set existingSecret=sda-secrets
```

- API and worker scale independently: the API is IO-bound, the worker is CPU-bound and holds
  model weights through a parse. Separate deployments, resource envelopes, HPAs and PDBs.
- `alembic upgrade head` runs as a `pre-install,pre-upgrade` hook Job, so the schema is never
  behind the code that reads it.
- Probes match what item 7 of the gap analysis rebuilt: `/livez` for liveness (static),
  `/health` for readiness (backend pings, memoized, no model construction and no provider call).
- The worker's grace period is 16 minutes -- long enough that a 200-page parse is not torn out
  from under the job -- and the API's is 2 minutes so in-flight SSE answers drain.
- `existingSecret` is the production path: point at a Secret written by an external secrets
  operator so nothing lands in `values.yaml` or in Helm release history.

### Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request:

| Job | Gates on |
|---|---|
| `backend` | `ruff check`, `mypy src`, `alembic upgrade head`, `pytest --cov-fail-under=65` against real Postgres/Redis/SeaweedFS |
| `frontend` | `oxlint`, `vite build` |
| `audit` | `pip-audit` on `requirements.txt`, Trivy filesystem scan (HIGH/CRITICAL) |
| `chart` | `helm lint` and `helm template` |
| `images` | Builds both images with buildx cache, then Trivy-scans each |

`ruff.toml` selects `E,F,I,B,C4,SIM,T20`. `UP` (pyupgrade) is deliberately excluded for now:
turning it on would make this a repo-wide `Optional[X]` to `X | None` rewrite, which belongs in
its own commit.

### Observability

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

### Accounts and tenancy

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

### Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | Generation backend: `gemini`, `groq`, `ollama`, or `none` (retrieval only) |
| `GEMINI_API_KEY` | _(empty)_ | Required when `LLM_PROVIDER=gemini` |
| `GEMINI_MODEL` | `gemini-3.6-flash` | Gemini model name |
| `GROQ_API_KEY` | _(empty)_ | Required when `LLM_PROVIDER=groq` |
| `GROQ_MODEL` | `llama-3.3-70b-versatile` | Groq model name |
| `OLLAMA_HOST` | `127.0.0.1:11435` | Ollama server address, used when `LLM_PROVIDER=ollama` |
| `OLLAMA_MODEL` | `qwen3:8b` | Ollama model name |
| `DATABASE_URL` | _(required)_ | Postgres DSN, e.g. `postgresql+psycopg://sda:sda@127.0.0.1:5433/sda` |
| `REDIS_URL` | _(required)_ | Redis DSN for the answer cache and ingest locks |
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
| `SENTRY_DSN` | _(empty)_ | Enable Sentry error reporting |
| `MODEL_PRICING_JSON` | _(empty)_ | Override generation pricing without a deploy: `{"model": [usd_per_mtok_in, usd_per_mtok_out]}` |
| `HF_TOKEN` | _(empty)_ | Optional; only needed for gated HF models or to avoid anonymous rate limits on embedder/reranker downloads |

### Running Tests

```bash
pytest tests/ -v
```

### Running Evaluation

```bash
python -m evaluation.run_eval --mode hybrid --generate
```

## Hallucination Handling

The system uses a multi-layer defense against hallucination:

### 1. Abstention Gate (pre-generation)
Before calling the LLM, the system checks whether retrieved passages are relevant enough to answer the question. If the best retrieval score is below the calibrated threshold (0.3, swept against the golden set), it refuses to answer rather than generating from weak evidence. A retrieval-consensus override prevents false refusals when both dense and lexical search independently rank the same chunk highly.

### 2. Source-Constrained Prompting
The LLM is prompted to answer only from the numbered source blocks and to cite each sentence with inline `[n]` markers tied to source IDs, which `prompts.parse_citations` extracts from the plain-text response. The prompt explicitly instructs the model to say it cannot answer if no source covers the question.

### 3. Citation Validation (post-generation)
After generation, each sentence's cited sources are validated using the cross-encoder reranker. If a citation doesn't match its claimed source passage above a support threshold, it's flagged as "unverified" in the UI. Sentences with no valid citations are marked "unsupported."

### 4. Confidence Scoring
A calibrated confidence score combines retrieval quality and citation validity. The score and label (High/Medium/Low) are shown to the user, so they can judge how much to trust the answer.

### 5. Conflict Detection
When different sources provide conflicting information (e.g., different numeric values for the same claim), the system surfaces the conflict explicitly rather than silently picking one.

### 6. Prompt Injection Defense
Uploaded documents are untrusted input. Source blocks in the prompt are wrapped in random-nonce XML tags, and any tag-like text inside the document is stripped, preventing a malicious document from forging citation boundaries or injecting instructions.

## Additional Features

Beyond the minimum requirements, the assistant implements:

### Conversation Memory / Follow-Up Questions
The chat retains history per document session (`frontend/src/hooks/useChat.js`). A follow-up like "what about the second one?" is resolved into a standalone question before retrieval, using conversation history (`CONDENSE_SYSTEM`/`CONDENSE_EXPAND_SYSTEM` in `src/generation/prompts.py`). After each answer, the system also proposes three follow-up questions drawn from retrieved-but-unused passages (`FOLLOWUP_SYSTEM`), so the user can keep exploring the document without guessing what else is in it.

### Answer Confidence / Evidence Indicator
Every answer carries a calibrated confidence score and High/Medium/Low label (`src/trust/confidence.py`), combining retrieval quality with per-sentence citation validation. Sentences whose cited source doesn't actually support them are flagged "unverified" or "unsupported" in the UI (`AnswerCard.jsx`), so the user can see which specific claims to double-check rather than trusting or distrusting the whole answer.

### Multi-Document Reasoning
Retrieval spans the full corpus, not a single selected file, and the prompt instructs the model to synthesize across sources rather than copy from one. When sources disagree on a value, the system surfaces each conflicting value with its own citation instead of silently picking one (see Hallucination Handling §5).

### Query Suggestions ("Did You Mean")
When a question can't be answered from the corpus, the system finds the closest-matching passage anyway and proposes a related question that passage *can* answer (`DIDYOUMEAN_SYSTEM`), turning a dead-end "no answer" into a useful next step.

## Edge Cases

| Case | Behavior | Where |
| --- | --- | --- |
| Encrypted / scanned / zero-text PDF | Reason-specific error message, upload rejected | `src/ingestion/parsers.py` |
| Wrong extension / spoofed type / 0-byte / oversize / page cap / non-UTF-8 TXT | Rejected with a specific message; TXT falls back to cp1252 before giving up | `src/ingestion/parsers.py` |
| Question before upload | Send disabled, hint explains why | `frontend/src/components/Composer.jsx` |
| Empty / over-length question | Send disabled past 2000 chars, hint shown | `frontend/src/components/Composer.jsx` |
| Duplicate content (same bytes) | Re-ingest short-circuits to the existing index | `src/ingestion/pipeline.py` |
| Duplicate filename, new content | Old doc's index, vectors and chip are removed before the new one is added | `src/ingestion/pipeline.py` |
| LLM provider down / model missing / timeout | Plain-text error, composer re-enabled, no stack trace | `src/generation/client.py` |
| Double-click Send / upload during an answer | `busy` state blocks re-entrant calls | `frontend/src/hooks/useChat.js` |
| Browser refresh | Chat history resets, but the on-disk index persists; doc list is reloaded from the ingest manifest on bootstrap | `frontend/src/App.jsx` |
| Prompt injection in uploaded document | Source blocks wrapped in per-request random-nonce tags; tag-shaped text inside documents is stripped | `src/generation/prompts.py` |
| Huge table chunk retrieved | Source truncated to remaining context budget, centered on the matched span | `src/generation/answerer.py` |

## Evaluation Results

Evaluated on a 33-item golden set covering single-doc factual, multi-doc comparison, unanswerable, and
prompt-injection queries against three real public-domain U.S. government documents. Generated with
`LLM_PROVIDER=ollama` (`qwen3:8b`); re-run after the `hybrid` score-scale fix (see
`docs/improvement-plan.md` step 1). Re-run with `python -m evaluation.run_eval --mode <mode>` (see
`evaluation/results/*.md`).

**Default mode: `hybrid_rerank`** (dense + BM25 fusion, cross-encoder reranked)

| Metric | `dense` | `hybrid` | `hybrid_rerank` |
|---|---|---|---|
| Retrieval hit@k | 1.000 | 1.000 | 1.000 |
| Retrieval MRR | 0.980 | 0.943 | 0.948 |
| Context recall | 0.980 | 0.960 | 0.780 |
| Context precision | 0.450 | 0.420 | 0.710 |
| Abstention: refusal precision | 1.000 | 1.000 | 0.667 |
| Abstention: refusal recall | 0.625 | 0.750 | 1.000 |
| Abstention: false refusal rate | 0.000 | 0.000 | 0.160 |
| Must-contain accuracy | 0.840 | 0.760 | 0.810 |
| Citation validity | 0.691 | 0.704 | 0.750 |
| Numeric grounding | 0.917 | 0.833 | 1.000 |
| Faithfulness | 0.808 | 0.875 | 0.849 |

Key results for the default mode (`hybrid_rerank`):
- **100% retrieval hit rate**: every answerable question's target passage is in the top-k.
- **100% refusal recall**: every unanswerable question is correctly refused (no hallucinated answers), at
  the cost of a 16% false-refusal rate — the conservative abstention threshold trades recall for
  over-caution on borderline answerable questions.
- **75% citation validity, 84% faithfulness**: most cited claims check out against their source passage,
  but this is via a local `qwen3:8b` generation, not the default cloud provider; Gemini/Groq numbers
  will likely differ and haven't been benchmarked here.
- `hybrid`'s false-refusal rate dropped from a near-universal abstain (pre-fix) to 0.000 — confirms the
  RRF score normalization fix (step 1) put it on a threshold scale that actually works.

### Eval gating

The golden set gates CI. `evaluation/thresholds.yaml` holds the floors and ceilings, versioned so a
retune is a reviewable diff; `python -m evaluation.run_eval --gate <profile>` runs the set, prints a
pass/fail table with a per-question-type breakdown, and exits non-zero on a breach. A metric the run
did not produce fails rather than passes.

Two profiles, because CI has no API key:

| Profile | Runs | Gates | Provider |
|---|---|---|---|
| `retrieval` | every PR (`ci.yml`) | hit@k, MRR, context recall/precision, refusal recall, false-refusal rate | `LLM_PROVIDER=none` |
| `generation` | nightly (`eval-nightly.yml`) | the above plus must-contain, citation validity, numeric grounding, faithfulness, injection resistance | `GEMINI_API_KEY` |

```bash
LLM_PROVIDER=none python -m evaluation.run_eval --gate retrieval
```

The per-type breakdown found a live defect on its first run: all four false refusals had the target
passage in the top-k, and the two prompt-injection questions scored lowest of all (0.002 and 0.005
against a 0.3 abstain threshold). The injected preamble dominates the cross-encoder pair, so the
abstain gate judges the attack text rather than the question. The system currently survives an
injected question by refusing it, which is not the same as resisting it — see
`docs/production-readiness.md` item 17.

## AI Tools Used

**Claude Code** (Anthropic's CLI) was used as the coding assistant throughout development. Claude wrote code, suggested architectures, debugged issues, and helped iterate on the design. Claude itself is never called by the running application — generation at runtime goes through the configured `LLM_PROVIDER` (Gemini, Groq, or Ollama), not Claude.

Specific uses:
- Scaffolding the project structure and writing initial implementations
- Debugging structured output issues with an early qwen3:8b JSON-schema prompt (discovered a schema regression where the model returned empty sentences with a complex schema; the prompt/parsing approach was later changed to plain text with inline `[n]` markers)
- Writing and running the evaluation harness
- Iterating on the UI design (CSS, component layout)
- Code review and security review passes

## Known Limitations

1. **Reranker latency**: The cross-encoder reranker adds meaningful latency, especially on CPU-only hardware.
2. **PDF-only complex parsing**: Only PDF and TXT are supported. DOCX/XLSX could be added via docling but were out of scope.
3. **No OCR**: Fully scanned PDFs are rejected. Mixed PDFs (text pages + scanned forms) index the text pages only and caption figures via BLIP, but scanned text itself isn't recovered.
4. **Single-session memory**: Follow-up questions are resolved against the last few turns via a condense-and-expand prompt, but history lives only in the browser tab and is lost on refresh — nothing is persisted server-side.
5. **Unvalidated `doc_ids`**: `/query`'s `doc_ids` filter is client-supplied and not checked against the manifest. Harmless for a single-user local app, but not something to carry into a multi-user deployment as-is.
5. **Abstention calibration**: The abstention threshold (0.3) was swept on a 33-item golden set. A larger, more diverse evaluation set would produce more robust thresholds.

## Time Log

| Phase | Activity | Time |
|---|---|---|
| 0 | Problem understanding, plan, environment setup, model downloads | 1 hr |
| 1 | Ingestion pipeline (parsers, chunker, pipeline) + tests | 1.5 hr |
| 2 | Generation skeleton (embedder, vector store, LLM client, answerer) | 1 hr |
| 3 | Evaluation harness + golden set | 0.5 hr |
| 4 | Retrieval upgrade (BM25, hybrid fusion, reranker) | 1 hr |
| 5 | Trust layer (abstention, citations, confidence, conflict detection) | 1 hr |
| 6 | Answer UI, sources panel, trace viewer | 0.5 hr |
| 7 | Hardening (edge cases, session recovery, security) | 0.5 hr |
| 8 | Code review, security review, final eval | 0.5 hr |
| 9 | UI overhaul, speed fixes, README, architecture | 1 hr |
| **Total** | | **~8 hr** |
