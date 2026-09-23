# Evaluation results — mode: hybrid_rerank

Items: 33 (generate=True)

| Metric | Value |
|---|---|
| retrieval.hit_at_k | 1.000 |
| retrieval.mrr | 0.948 |
| retrieval.context_recall | 0.780 |
| retrieval.context_precision | 0.710 |
| abstention.refusal_precision | 0.667 |
| abstention.refusal_recall | 1.000 |
| abstention.false_refusal_rate | 0.160 |
| generation.must_contain_accuracy | 0.810 |
| generation.citation_validity | 0.750 |
| generation.numeric_grounding_pass_rate | 1.000 |
| generation.faithfulness | 0.849 |

| Stage | p50 (s) | p95 (s) |
|---|---|---|
| Searching | 2.459 | 3.597 |
| Reranking | 12.337 | 15.424 |
| Assembling | 0.012 | 0.022 |
| Generating | 4.509 | 5.667 |
| Validating | 5.718 | 15.328 |
| Abstaining | 0.000 | 0.000 |
