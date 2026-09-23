# Evaluation results — mode: dense

Items: 33 (generate=True)

| Metric | Value |
|---|---|
| retrieval.hit_at_k | 1.000 |
| retrieval.mrr | 0.980 |
| retrieval.context_recall | 0.980 |
| retrieval.context_precision | 0.450 |
| abstention.refusal_precision | 1.000 |
| abstention.refusal_recall | 0.625 |
| abstention.false_refusal_rate | 0.000 |
| generation.must_contain_accuracy | 0.840 |
| generation.citation_validity | 0.691 |
| generation.numeric_grounding_pass_rate | 0.917 |
| generation.faithfulness | 0.808 |

| Stage | p50 (s) | p95 (s) |
|---|---|---|
| Searching | 2.360 | 4.717 |
| Assembling | 0.053 | 0.164 |
| Generating | 4.682 | 6.237 |
| Validating | 6.159 | 11.948 |
