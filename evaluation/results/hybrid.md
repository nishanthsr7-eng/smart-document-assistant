# Evaluation results — mode: hybrid

Items: 33 (generate=True)

| Metric | Value |
|---|---|
| retrieval.hit_at_k | 1.000 |
| retrieval.mrr | 0.943 |
| retrieval.context_recall | 0.960 |
| retrieval.context_precision | 0.420 |
| abstention.refusal_precision | 1.000 |
| abstention.refusal_recall | 0.750 |
| abstention.false_refusal_rate | 0.000 |
| generation.must_contain_accuracy | 0.760 |
| generation.citation_validity | 0.704 |
| generation.numeric_grounding_pass_rate | 0.833 |
| generation.faithfulness | 0.875 |

| Stage | p50 (s) | p95 (s) |
|---|---|---|
| Searching | 2.358 | 3.617 |
| Assembling | 0.157 | 0.289 |
| Generating | 4.311 | 6.725 |
| Validating | 6.227 | 15.448 |
