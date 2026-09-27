# OCR parity — leave_policy.pdf

7 golden-set items answered only by this document, mode `hybrid_rerank`. The OCR copy is the same document rasterised at 2.0x with no text layer, 13 pages, ingested in 122.6s (92 chunks against 78 from the text layer).

| Metric | Text layer | OCR |
|---|---|---|
| hit_at_k | 1.000 | 1.000 |
| mrr | 1.000 | 1.000 |
| context_recall | 1.000 | 1.000 |
| context_precision | 0.714 | 0.607 |
| answered_rate | 1.000 | 1.000 |

| Item | Type | Text layer hit@k | OCR hit@k | OCR status |
|---|---|---|---|---|
| lp-01 | lookup | 1.000 | 1.000 | retrieved |
| lp-02 | lookup | 1.000 | 1.000 | retrieved |
| lp-03 | table | 1.000 | 1.000 | retrieved |
| lp-04 | exact_term | 1.000 | 1.000 | retrieved |
| lp-05 | paraphrase | 1.000 | 1.000 | retrieved |
| lp-06 | conflict | 1.000 | 1.000 | retrieved |
| inj-01 | injection | 1.000 | 1.000 | retrieved |
