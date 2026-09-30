# Service level objectives

Four objectives, a 30-day rolling window, and an error budget for each. The rules that measure
them are in `deploy/helm/sda/files/alerts.yml`; the `sda-sli` group defines each SLI exactly
once so that a dashboard and a page can never disagree about what "good" meant.

| SLO | Objective | Budget over 30 days | SLI |
|---|---|---|---|
| Availability | 99.5% of requests are not 5xx | 3h 39m of total failure | `sda:availability:ratio_rate5m` |
| Answer latency | 95% of answers complete within 15s | 1 in 20 answers may be slow | `sda:latency_slo:ratio_rate5m` |
| Citation validity | ≥ 90% of cited sentences verify against their source | 1 in 10 | `sda:citation_validity:ratio_rate30m` |
| Abstention ceiling | ≤ 40% of answers abstain | — (a ceiling, not a budget) | `sda:abstain:ratio_rate15m` |

## Why these four

The first two are the ordinary ones. The last two exist because **this service can fail while
returning 200 OK, quickly**. A retrieval system that answers confidently from the wrong passage
and one that refuses every question are both, to every HTTP-level signal, a perfectly healthy
service. Citation validity catches the first and the abstention ceiling catches the second, and
without them the availability SLO would say the product is fine while it is useless.

Abstention has a ceiling rather than a budget because abstaining is the *correct* behaviour on
an unanswerable question — the golden set's refusal recall is 1.000 and should stay there. What
is not correct is abstaining on 40% of real traffic, which means retrieval has broken, not that
the gate is working.

## What counts as a failure

- A **5xx** is a failure. A **429** (rate limit or budget) and a **503 from load shedding** are
  not: they are the service working as designed under pressure, and counting them would page
  someone for a correctly-shedding service.
- `/metrics` is excluded from availability. It is scraped far more often than anything else and
  would dominate the ratio.
- **Latency is measured on cache misses only** (`sda_answer_duration_seconds`), because a
  served-from-cache answer says nothing about whether the pipeline is fast.
- A **client disconnect** is not a latency failure. It is counted separately in
  `sda_queries_abandoned_total{reason="disconnect"}`.

## Error budget policy

The budget is what makes the objective a decision rather than a number on a dashboard.

| Budget remaining | What changes |
|---|---|
| > 50% | Nothing. Ship. |
| 10–50% | Risky changes (retrieval, thresholds, provider, schema) need a rollback plan written down before merge. |
| < 10% | Feature work stops. Only reliability fixes, and every change needs a second reviewer. |
| Exhausted | Freeze except for fixes to the cause. The freeze lifts when the budget is back above 10%, not when the incident closes. |

The point of the second row is that spending budget is *allowed*. An unspent budget at the end
of every month means the objective is too loose or the team is shipping too cautiously; both are
worth noticing.

## Burn-rate alerting

Alerting on "the SLI is below the objective right now" pages on every blip and misses slow
bleeds. The alerts instead fire on **burn rate**, with two windows each — a long one that says
the budget is being consumed and a short one that confirms it still is:

- **14.4x over 1h, confirmed over 5m** → critical. At that rate the month's budget is gone in
  about two days. Page.
- **6x over 6h, confirmed over 30m** → warning. A week to exhaustion. Ticket.

Latency uses the 6x pair only. A minute of slow answers is not worth waking anyone.

## Not done

Per-tenant SLOs (everything here is service-wide, so one very large tenant's bad experience can
hide inside a good aggregate), a user-journey SLO spanning upload → first answer, and any
formal review cadence. The eval gate in CI is the quality equivalent of these numbers
at merge time; nothing yet ties the two together, so a release can pass the gate and still burn
the citation-validity budget in production.
