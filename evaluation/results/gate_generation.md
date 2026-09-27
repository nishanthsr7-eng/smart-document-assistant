# Eval gate: FAIL

Profile `generation` (thresholds v3), mode `hybrid_rerank`, generate=True, 33 items.

| Metric | Value | Bound | Threshold | |
|---|---|---|---|---|
| retrieval.hit_at_k | 1.000 | min | 0.950 | pass |
| retrieval.mrr | 1.000 | min | 0.950 | pass |
| retrieval.context_recall | 0.960 | min | 0.850 | pass |
| retrieval.context_precision | 0.680 | min | 0.550 | pass |
| abstention.refusal_recall | 1.000 | min | 0.900 | pass |
| abstention.false_refusal_rate | 0.080 | max | 0.100 | pass |
| generation.must_contain_accuracy | 0.913 | min | 0.700 | pass |
| generation.citation_validity | 0.447 | min | 0.650 | FAIL |
| generation.numeric_grounding_pass_rate | 0.414 | min | 0.800 | FAIL |
| generation.faithfulness | 0.907 | min | 0.750 | pass |
| generation.injection_resistance | 1.000 | min | 1.000 | pass |

| Type | n | correct abstention | hit@k | must_contain |
|---|---|---|---|---|
| conflict | 2 | 1.000 | 1.000 | 1.000 |
| exact_term | 3 | 1.000 | 1.000 | 1.000 |
| injection | 2 | 1.000 | 1.000 | 1.000 |
| lookup | 6 | 1.000 | 1.000 | 0.833 |
| multi_doc | 5 | 0.600 | 1.000 | 1.000 |
| paraphrase | 5 | 1.000 | 1.000 | 0.800 |
| table | 2 | 1.000 | 1.000 | 1.000 |
| unanswerable | 8 | 1.000 | n/a | n/a |
