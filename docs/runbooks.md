# Runbooks

One section per alert that can page or ticket someone, linked from the `runbook` annotation in
`deploy/helm/sda/files/alerts.yml`. Each one answers the same four questions: what fired, what
it usually is, what to check in order, and what to do about it.

A runbook that only says "investigate" is a runbook nobody reads at 3am, so each of these names
the specific query to run and the specific lever to pull.

**Before anything else**, for any alert: `GET /health` on a replica tells you whether a backend
is down, and `sda_queries_in_flight` against `api.query_concurrency` tells you whether the
service is saturated. Those two rule out half of what follows.

---

## AvailabilityBudgetBurn

**Fired:** 5xx responses are consuming the availability budget 14x (critical) or 6x (warning)
faster than it can be sustained. See [SLOs](slos.md).

**Usually:** a backend is down, or one replica is bad and the service has not removed it.

1. `sum by (route, status) (rate(sda_http_requests_total{status=~"5.."}[5m]))` — one route or
   all of them? One route is a bug in that handler; all of them is a dependency.
2. `GET /health` on each replica. It returns 503 with the failing backend named. `status:
   "draining"` means the replica is shutting down, which is expected during a rollout and not
   an incident.
3. If it is one replica, delete the pod. If readiness did not catch it, that is a second bug
   worth a ticket.
4. If it is Postgres, Redis or the object store: that is the incident. The API has no
   process-local fallback by design and will not serve without them.

**Do not** raise the objective to stop the alert. Roll back the deploy that started it —
`kubectl rollout undo` — and investigate afterwards.

---

## HttpServerErrors

**Fired:** over 2% of requests are 5xx for 5 minutes. Faster and blunter than the budget alert;
they usually fire together.

Same triage as above. If this fires *without* `AvailabilityBudgetBurn`, the errors are
concentrated in a short spike — check whether a worker restarted, or whether a single large
upload hit the body cap.

---

## CitationValidityBelowFloor

**Fired:** under 90% of cited sentences verify against the source they cite. This is the alert
that catches the failure this service exists to prevent: confident, fluent, wrong.

**Usually:** the reranker or the grounding threshold, not the model.

1. `GET /health` — is `reranker` loaded, or is a TEI service failing? Retrieval silently
   degrades to fusion-only ranking when the cross-encoder is unavailable, and the abstain gate
   depends on the cross-encoder score. That is the most common cause.
2. `sda_retrieval_top_score` — if the distribution has shifted down, this is a retrieval
   problem presenting as a citation problem.
3. Compare against the last `evaluation/run_eval.py` gate run. If the gate was green on this
   commit, the code is not the change; look at what was ingested recently.
4. `sda_output_scan_blocks_total` — a nonzero rate means a document is steering the model.
   Find the document (audit log, `action=ingest`, that day) and quarantine it.

**Mitigation:** raising `trust.abstain_threshold` trades answers for correctness and takes
effect without a deploy through the versioned threshold artifact in `config/thresholds/`. Do
that first, then find the cause.

---

## AnswerLatency / LatencyBudgetBurn

**Fired:** p95 answer latency over 15s, or the latency budget burning.

1. `histogram_quantile(0.95, sum by (stage, le) (rate(sda_stage_duration_seconds_bucket[10m])))`
   — the stage breakdown is the whole diagnosis. It is almost always *rerank* or *generate*.
2. **Rerank slow:** CPU saturation. `sda_queries_in_flight` near `api.query_concurrency` means
   the replicas are full — scale out. If it is one pod, it lost its CPU quota; check limits.
3. **Generate slow:** the provider. `sda_llm_failover_total` and `sda_llm_breaker_open` say
   whether the chain has moved to a slower fallback.
4. **Everything slow:** check `sda_answer_cache_total` for a cache hit rate that has collapsed,
   usually because a deploy changed `retrieval_version` and invalidated every key. That is
   expected for one cache-fill period after a retrieval config change.

**Mitigation:** scale the API deployment. `QUERY_TIMEOUT_S` bounds the damage but does not fix
it — a timed-out answer is still a failure to the person who asked.

---

## AbstainRateSpike

**Fired:** over 40% of answers abstain. The service is up, fast, and useless.

**Usually retrieval, not the gate.**

1. `sda_retrieval_top_score` — if scores collapsed, retrieval is broken and the gate is doing
   its job.
2. `GET /admin/index` — `reindex_needed: true` plus a nonzero
   `sda_reindex_pending_documents` means a configuration change left documents unbuilt. See
   [ReindexNotProgressing](#reindexnotprogressing).
3. `GET /health` — an embedder or TEI failure produces exactly this shape.
4. If retrieval looks healthy, the questions changed, not the service. Check whether one tenant
   is responsible before touching a threshold.

**Do not** lower the abstain threshold to make this alert stop. The threshold was swept against
the golden set with a false-refusal rate attached; moving it by hand trades away the one
property the trust layer guarantees.

---

## IngestQueueBacklog

**Fired:** more than 25 jobs waiting for 15 minutes. The queue is shared, so the alert uses
`max` across replicas.

1. Is the worker running? `kubectl get pods -l app.kubernetes.io/component=worker`.
2. `sda_ingest_jobs_total{outcome="failed"}` — a poison document that fails fast does not
   create a backlog; a 200-page OCR job does. `sda_ingest_duration_seconds` tells you which.
3. `GET /jobs/dead-letters` (admin) — terminal failures, newest first.

**Mitigation:** scale the worker deployment. `INGEST_CONCURRENCY` is per worker process and
raising it on a CPU-bound worker makes everything slower, not faster — add replicas instead.

---

## QueryLoadShedding

**Fired:** `/query` is returning 503 at capacity.

This is not a fault. It is the API refusing work it cannot do, which is the designed behaviour
past `QUERY_CONCURRENCY` — the alternative is a queue of requests behind a saturated reranker
and a timeout for everyone.

1. `sda_queries_in_flight` vs `api.query_concurrency`: if it is pinned at the cap, scale out.
2. `sda_queries_abandoned_total{reason="timeout"}` instead of `shed` is a different problem —
   answers are running past `QUERY_TIMEOUT_S`. Go to [AnswerLatency](#answerlatency--latencybudgetburn).
3. Raising `QUERY_CONCURRENCY` is only correct if CPU has headroom. Each slot holds a thread
   through a reranker forward pass; over-provisioning slots on a CPU-bound pod converts shed
   requests into slow ones.

---

## GenerationProviderBreakerOpen

**Fired:** a provider has failed enough times to open its breaker and is being skipped.

The service is still answering — from the next provider in `LLM_FALLBACK_CHAIN`. That is a
silent degradation worth naming: the answers are coming from a different model than the one the
evaluation gate measured.

1. `sda_llm_failover_total` — who is serving instead.
2. The provider's status page, then the API key. A rotated-out key looks exactly like an outage
   here; the breaker counts construction failures too.
3. With no fallback left, `/query` returns `ModelUnavailable` and this becomes an availability
   incident.

**Mitigation:** fix the key, or reorder `LLM_FALLBACK_CHAIN`. The breaker is per process and
closes itself after `LLM_BREAKER_COOLDOWN_S`; no restart is needed once the provider recovers.

---

## GenerationSpendBurn

**Fired:** generation spend is tracking above $50/day.

1. `sda_answer_cache_total` — a cache that stopped hitting is the usual cause, and a deploy that
   changed `retrieval_version` is the usual reason.
2. `sda_rate_limited_total{scope="budget"}` — if this is zero while spend is high, the per-tenant
   `DAILY_COST_USD` budgets are unset or too loose. That is the fix, not a code change.
3. `sda_llm_tokens_total` by model — a failover to a more expensive provider does this too.

---

## TenantsHittingRateLimits

**Fired:** sustained 429s on one scope.

- `scope=auth` sustained is credential stuffing. It is limited by client address, so check
  whether it is one address and block it at the ingress.
- `scope=query` or `scope=ingest` against one tenant is either growth — raise their limit — or
  a client in a retry loop. The 429 carries a real `Retry-After`; a client ignoring it will
  keep this firing forever.

---

## ReindexNotProgressing

**Fired:** documents have been waiting six hours to be rebuilt at the version the running
configuration would produce.

This is not an outage. Reads are pinned to the index alias, so the corpus is still being served
correctly — by the *old* build. What it means is that a chunker or embedder change has been
deployed and has not taken effect.

1. `GET /admin/index` (needs `X-Operator-Token`) — `active_version` vs `building_version`, and
   the progress of the last rebuild.
2. If `progress.status` is `failed`, `progress.errors` names the documents. A rebuild only
   promotes on a clean sweep, deliberately: a partial cutover would hide every document it
   could not rebuild.
3. Fix or delete the offending documents, then `POST /admin/index/reindex`.

**If the cutover made things worse:** `POST /admin/index/rollback` points reads back at the
previous build. That works only while its chunks survive `RETENTION_OLD_INDEX_DAYS`; after that
the rollback is another rebuild.

---

## RetentionSweepStalled

**Fired:** soft-deleted documents are past their purge window and still present.

Everything works. It will keep working, for months, until storage runs out — which is why this
has an alert at all.

1. Is the worker running? The sweep is an arq cron job in the ingest worker
   (`retention_sweep`, hourly at :07).
2. `RETENTION_ENABLED` — set to 0, this is expected and the alert should be silenced rather
   than chased.
3. `POST /admin/retention/sweep` runs it now and returns the counts, which is also the drill.

---

## Restore

Not an alert — the procedure for the worst case. See
[deploy/backup/README.md](../deploy/backup/README.md). The short version: `python -m
deploy.backup.backup restore --from <backup>`, then verify with `snapshot`. Rehearse it against a scratch
deployment before you need it.
