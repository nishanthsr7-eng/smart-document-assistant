# On-call

This document is the process, not a roster. A roster lives in the paging tool; what belongs in
the repository is what the person carrying the pager is expected to do, and — more importantly
— what they are expected *not* to do.

## Severity

| Severity | Means | Response |
|---|---|---|
| **SEV1** | The service is down, or answering wrongly at scale. No answers, or citation validity in free-fall. | Page. Acknowledge in 5 minutes, incident channel immediately. |
| **SEV2** | Degraded but working. Load shedding, a provider on its fallback, an ingest backlog. | Page during business hours, ticket overnight. |
| **SEV3** | Working, and something is accumulating. Retention stalled, a reindex not progressing, rate limits sustained. | Ticket. Next working day. |

A **wrong answer with a valid-looking citation is SEV1**, not SEV2. Everything else here
degrades visibly; that one does not, and it is the failure the product is supposed to make
impossible.

## What pages

Only two alerts page: `AvailabilityBudgetBurnFast` and `CitationValidityBelowFloor`. Everything
else in `alerts.yml` is a ticket. That ratio is deliberate — an on-call rotation that is woken
by an ingest backlog stops reading the alerts, and then misses the one that mattered.

Adding a paging alert requires a runbook section in [runbooks.md](runbooks.md) in the same pull
request. An alert without a runbook is a notification that someone is now awake with no idea
what to do.

## The first five minutes

1. Acknowledge, so the escalation timer stops.
2. `GET /health` on a replica. It names the failing backend, and `status: "draining"` means a
   rollout, not an incident.
3. Open the runbook named in the alert's `runbook` annotation. Do what it says before
   improvising — the runbooks encode the diagnosis order, and the usual cause really is the
   usual cause.
4. **Mitigate before diagnosing.** Rolling back a deploy, scaling a deployment, or pointing the
   index alias back at the previous build all restore service without knowing the cause.
5. Post what you did in the incident channel as you do it, not afterwards.

## Levers, in order of preference

| Lever | Command | Reverses |
|---|---|---|
| Roll back the deploy | `kubectl rollout undo deploy/sda-api` | Anything a release caused |
| Scale out | `kubectl scale deploy/sda-api --replicas=N` | Load shedding, latency |
| Roll back the index | `POST /admin/index/rollback` | A bad reindex cutover |
| Raise the abstain threshold | edit `config/thresholds/`, redeploy | Wrong answers, at the cost of more refusals |
| Reorder the provider chain | `LLM_FALLBACK_CHAIN` | A provider outage the breaker has not caught |

Prefer the lever that is reversible over the one that is correct. Correct can wait for morning.

## What not to do

- **Do not change a threshold to silence an alert.** The abstain threshold and the citation
  floor were swept against the golden set with false-refusal and false-accept rates attached.
  Moving one by hand at 3am trades away the
  guarantee the alert was protecting, and nobody will remember to move it back.
- **Do not raise an SLO to stop a burn alert.** The budget is the decision, not the number.
- **Do not run the restore drill against production.** It destroys both stores by design.
- **Do not delete a poison document without reading it.** A document that reliably breaks
  ingest, or that trips the output scanner, is evidence.

## Handover

At the end of a shift, in the channel: what fired, what was done, what is still open, and what
is armed to fire next. A silent shift still gets a handover saying it was silent — the absence
of a handover is indistinguishable from a forgotten one.

## Postmortems

Any SEV1, and any SEV2 that recurs twice in a week. Blameless, and the output is an action with
an owner, not a list of lessons. If the incident was caused by a decision that looked correct at
the time, the output is a written decision superseding it.

## Not done

There is no rotation, no paging integration, and no escalation policy: those depend on the team
running the service. What is real here is the alert-to-runbook mapping, the severity
definitions, and the levers, all of which are testable against the code as it stands.
