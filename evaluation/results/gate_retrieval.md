# Eval gate: PASS

Profile `retrieval` (thresholds v5), mode `hybrid_rerank`, generate=False, 60 items.

| Metric | Value | Bound | Threshold | |
|---|---|---|---|---|
| retrieval.hit_at_k | 1.000 | min | 0.950 | pass |
| retrieval.hit_at_1 | 0.956 | min | 0.880 | pass |
| retrieval.mrr | 0.978 | min | 0.920 | pass |
| retrieval.context_recall | 0.933 | min | 0.850 | pass |
| retrieval.context_precision | 0.600 | min | 0.520 | pass |
| retrieval.distractor_leak | 0.102 | max | 0.200 | pass |
| abstention.refusal_recall | 1.000 | min | 0.900 | pass |
| abstention.hard_negative_refusal | 0.286 | min | 0.200 | pass |
| abstention.false_refusal_rate | 0.022 | max | 0.100 | pass |

| Type | n | correct abstention | hit@k | hit@1 | must_contain |
|---|---|---|---|---|---|
| conflict | 2 | 1.000 | 1.000 | 1.000 | n/a |
| cross_doc_conflict | 3 | 1.000 | 1.000 | 1.000 | n/a |
| exact_term | 3 | 1.000 | 1.000 | 1.000 | n/a |
| hard_negative | 7 | 0.286 | n/a | n/a | n/a |
| injection | 2 | 1.000 | 1.000 | 1.000 | n/a |
| lookup | 6 | 1.000 | 1.000 | 1.000 | n/a |
| multi_doc | 5 | 1.000 | 1.000 | 1.000 | n/a |
| multi_hop | 5 | 1.000 | 1.000 | 0.800 | n/a |
| paraphrase | 5 | 1.000 | 1.000 | 1.000 | n/a |
| scope | 12 | 0.917 | 1.000 | 0.917 | n/a |
| table | 2 | 1.000 | 1.000 | 1.000 | n/a |
| unanswerable | 8 | 1.000 | n/a | n/a | n/a |
