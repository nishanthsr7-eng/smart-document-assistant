# Eval gate: PASS

Profile `retrieval` (thresholds v3), mode `hybrid_rerank`, generate=False, 33 items.

| Metric | Value | Bound | Threshold | |
|---|---|---|---|---|
| retrieval.hit_at_k | 1.000 | min | 0.950 | pass |
| retrieval.mrr | 1.000 | min | 0.950 | pass |
| retrieval.context_recall | 0.960 | min | 0.850 | pass |
| retrieval.context_precision | 0.680 | min | 0.550 | pass |
| abstention.refusal_recall | 1.000 | min | 0.900 | pass |
| abstention.false_refusal_rate | 0.000 | max | 0.100 | pass |

| Type | n | correct abstention | hit@k | must_contain |
|---|---|---|---|---|
| conflict | 2 | 1.000 | 1.000 | n/a |
| exact_term | 3 | 1.000 | 1.000 | n/a |
| injection | 2 | 1.000 | 1.000 | n/a |
| lookup | 6 | 1.000 | 1.000 | n/a |
| multi_doc | 5 | 1.000 | 1.000 | n/a |
| paraphrase | 5 | 1.000 | 1.000 | n/a |
| table | 2 | 1.000 | 1.000 | n/a |
| unanswerable | 8 | 1.000 | n/a | n/a |
