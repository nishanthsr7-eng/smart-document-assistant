# Eval gate: PASS

Profile `retrieval` (thresholds v2), mode `hybrid_rerank`, generate=False, 33 items.

| Metric | Value | Bound | Threshold | |
|---|---|---|---|---|
| retrieval.hit_at_k | 1.000 | min | 0.950 | pass |
| retrieval.mrr | 0.948 | min | 0.850 | pass |
| retrieval.context_recall | 0.780 | min | 0.700 | pass |
| retrieval.context_precision | 0.706 | min | 0.550 | pass |
| abstention.refusal_recall | 1.000 | min | 0.750 | pass |
| abstention.false_refusal_rate | 0.160 | max | 0.250 | pass |

| Type | n | correct abstention | hit@k | must_contain |
|---|---|---|---|---|
| conflict | 2 | 1.000 | 1.000 | n/a |
| exact_term | 3 | 1.000 | 1.000 | n/a |
| injection | 2 | 0.000 | 1.000 | n/a |
| lookup | 6 | 1.000 | 1.000 | n/a |
| multi_doc | 5 | 0.800 | 1.000 | n/a |
| paraphrase | 5 | 1.000 | 1.000 | n/a |
| table | 2 | 0.500 | 1.000 | n/a |
| unanswerable | 8 | 1.000 | n/a | n/a |
