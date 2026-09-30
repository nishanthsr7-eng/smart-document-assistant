# Twelve-factor audit

Factor by factor, with the evidence and the gaps. The value of this document is the two rows
where the answer is "partly" — the ten that pass are cheap to claim and easy to verify.

| # | Factor | Status | Evidence |
|---|---|---|---|
| I | Codebase | Pass | One repo, one app, three roles from one image (`api`, `worker`, `migrate`) selected by entrypoint argument. |
| II | Dependencies | Pass | `requirements.txt` pinned, `requirements-dev.txt` for tooling, `package-lock.json` exact-pinned and installed with `npm ci`. The image declares its system packages; nothing is assumed present. |
| III | Config | Pass | Every setting is an environment variable read through `src/core/config.py` and validated at import. `.env.example` is the contract. No config file is read at runtime except the versioned threshold artifact, which is a *release artifact* rather than config — see below. |
| IV | Backing services | Pass | Postgres, Redis, the object store, the generation provider and the optional TEI services are all URLs in the environment. Swapping MinIO for S3 or Gemini for Groq is a variable, not a code change. |
| V | Build, release, run | Pass | Build is the image; release is image + Helm values + secret; run is the pod. The migration is a `pre-install,pre-upgrade` hook job, so schema and code move together as one release. |
| VI | Processes | Pass | Nothing is process-local since item 1. The one piece of in-process state is the five-second alias cache, which is derived and self-healing. |
| VII | Port binding | Pass | uvicorn binds `0.0.0.0:8000`; the worker exposes its own scrape port. No external web server is required to serve the app. |
| VIII | Concurrency | Pass | Scale out by process: `--workers` per API pod, replicas per deployment, and API and worker scale independently because their bottlenecks are different (event loop and connections vs CPU and memory). HPAs on both. |
| IX | Disposability | **Partly** | See below. |
| X | Dev/prod parity | Pass | `docker compose up -d` brings up the same backing services the chart points at; `--profile app` runs the same images. The gap that remains is hardware: no GPU anywhere, so CPU latency is what every number here was measured on. |
| XI | Logs | Pass | JSON to stdout, one event per line, no file handler and no rotation anywhere in the codebase. Correlation is `request_id` plus the OTel trace context. |
| XII | Admin processes | Pass | `migrate` runs the same image; reindex and the retention sweep are jobs on the same queue the app uses, run by the same worker; the backup CLI imports the same settings. Nothing operational runs from a developer's laptop against production by design. |

## IX. Disposability — what is missing

**Startup** is fast and does not block on much: model weights are baked into the image
(`HF_HUB_OFFLINE=1` makes a missed bake fail loudly rather than silently reaching for the
network), and models load lazily, so a replica is ready in seconds.

**Shutdown** is where the work went, and where a gap remains.

What is done: a SIGTERM handler fails readiness immediately, so a probe arriving during
shutdown is told the truth rather than finding out up to ten seconds later; the chart's preStop
sleep gives endpoint removal a head start over SIGTERM, which are dispatched together and where
SIGTERM usually wins; uvicorn's `--timeout-graceful-shutdown` exceeds `QUERY_TIMEOUT_S`, so an
answer already streaming is finished rather than cut off; and the query pool drains on lifespan
exit. `/query` refuses new work with a 503 once draining. The worker gets a 960-second grace
period so a 200-page parse is not killed mid-job.

An ingest job killed by `SIGKILL` used to leave a row behind that nothing collected --
`discard_failed` runs in the failure path, so a job that *raises* cleans up after itself, and
one whose process is killed does not. The retention sweep now reconciles those too: a document
still short of `live` after twice the job timeout is abandoned rather than in progress, because
a job still running at that point would have been killed by its own timeout.

What is still missing:

- **Jobs are not resumable.** A killed ingest is terminal and waits in the dead-letter list for
  a human, rather than being retried from its last state. `max_tries = 1` is deliberate —
  automatic retry of a poison document is a loop — but "failed because the pod moved" and
  "failed because the document is broken" are not distinguished, and they should be.

## III. Config — the one deliberate deviation

Trust thresholds are read from `config/thresholds/<version>.yaml` rather than from environment
variables, selected by a flag that *is* an environment variable. That is a deviation from strict
factor III and it is on purpose: these are a **swept set of related numbers**, and setting one of them independently through
the environment is exactly the mistake the artifact prevents. They are versioned with the code,
diffed in review, and folded into the answer cache key so a retune cannot serve answers computed
under the old values.
