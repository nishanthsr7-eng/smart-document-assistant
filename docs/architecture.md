# Architecture — Smart Document Assistant

Status date: 2026-09-23. Reviewed against commit `53e3ea6`.

This document is the single engineering reference for the system: what exists today, what is
wrong with it, and what an organisation-grade version needs. Part A is as-built. Part B is the
defect register. Parts C–F are the forward plan. Part G is the deadline-scoped execution order.

The product thesis, stated once so every decision below can be checked against it:

> A free general chat assistant answers from the model's memory and treats attached files as a
> courtesy. This tool inverts that: **no claim exists unless a passage in the user's corpus
> supports it, and every claim is traceable to a page.** Our differentiators are *provenance,
> refusal, calibration, and auditability* — not fluency. We will lose a fluency contest with a
> frontier model and win a "can you prove it, and would you sign off on it" contest.

---

## Part A — As-built system

### A1. Shape

```
                      ┌──────────────────────────────────────────┐
  upload (pdf/txt) ──▶│ ingestion                                │
                      │  parsers.py   → Element[] (kind,page,     │
                      │                 section_path)             │
                      │  chunker.py   → ParentChunk[] (~800 tok)  │
                      │                 ChildChunk[]  (~200 tok)  │
                      │  pipeline.py  → manifest + parents/*.json │
                      └───────────┬──────────────────────────────┘
                                  │ embed child.embed_text (BGE-base)
                                  ▼
                      ┌──────────────────────────────────────────┐
                      │ stores                                   │
                      │  ChromaDB  "chunks"  (cosine, persisted) │
                      │  BM25Okapi in-memory (rank_bm25)         │
                      │  data/parents/<doc_id>.json (parent text)│
                      └───────────┬──────────────────────────────┘
                                  │
  question ──▶ condense ──▶ expand ──▶ dense ∥ lexical ──▶ RRF ──▶ rerank
                                  │                                   │
                                  ▼                                   ▼
                      ┌──────────────────────────────────────────┐  abstention gate
                      │ context assembly                         │
                      │  parent lookup → MMR → per-doc cap →     │
                      │  span-centred window → token budget      │
                      └───────────┬──────────────────────────────┘
                                  │  4 numbered sources, ~3000 tok
                                  ▼
                      ┌──────────────────────────────────────────┐
                      │ generation (Gemini | Groq | Ollama)      │
                      │  streamed, inline [n] citations required │
                      └───────────┬──────────────────────────────┘
                                  ▼
                      ┌──────────────────────────────────────────┐
                      │ trust                                    │
                      │  sentence split → numeric grounding →    │
                      │  cross-encoder support → conflicts →     │
                      │  confidence (4 weighted signals)         │
                      └───────────┬──────────────────────────────┘
                                  ▼
                        React SPA answer card + sources + trace
```

### A2. Module inventory

| Layer | Module | Responsibility as built |
|---|---|---|
| core | `config.py` | Frozen dataclass settings tree; `ingest_version` hash over chunk params + embedder name |
| core | `tracing.py` | Per-stage timing context manager, JSON log of whitelisted scalar keys |
| core | `tokens.py` | Token count via the **embedder's** HF tokenizer |
| core | `cache.py` | Thread-safe TTL+LRU answer cache, cleared on ingest/delete |
| core | `errors.py` | Typed document/model/generation errors with user-facing messages |
| core | `health.py` | Probes embedder, vector store, LLM; returns degraded/ok |
| ingestion | `parsers.py` | PDF preflight (pypdf) → fast text path or Docling layout path; table linearisation; BLIP figure captions; running header/footer strip; NFKC + de-hyphenation |
| ingestion | `chunker.py` | Section-aware parent grouping; sentence-window children with 1-sentence overlap; atomic windows for tables/figures; `char_span_in_parent` kept for highlighting |
| ingestion | `pipeline.py` | Content-hash `doc_id`, manifest with `ingest_version`, replace-by-filename, duplicate short-circuit, parent persistence |
| retrieval | `embedder.py` | BGE-base-en-v1.5, normalised, asymmetric query prefix |
| retrieval | `vector_store.py` | Chroma persistent client, `doc_id` `$in` filter, `Hit` dataclass |
| retrieval | `keyword_index.py` | BM25Okapi over `header + text`, stopword-filtered tokens, lock-guarded rebuild |
| retrieval | `hybrid.py` | Multi-variant dense merge (best cosine per chunk) + Reciprocal Rank Fusion |
| retrieval | `reranker.py` | mxbai-rerank-base cross-encoder over top-10 fused; rank-based `calibrate()` |
| retrieval | `query.py` | LLM query expansion (3 variants) and HyDE (off) |
| generation | `client.py` | `Provider` protocol; Gemini / Groq / Ollama implementations, stream + generate + health |
| generation | `prompts.py` | Grounding system prompt, nonce-fenced source blocks, query-type format hints, citation parsing |
| generation | `answerer.py` | The orchestrator: cache → condense → expand → retrieve → fuse → rerank → gate → assemble → generate → validate → confidence |
| trust | `abstention.py` | Hard threshold 0.30, soft band to 0.45 with confidence modifier, consensus override |
| trust | `citations.py` | Numeric grounding prefilter, cross-encoder support scoring, numeric-disagreement conflict detection, optional NLI (disabled) |
| trust | `confidence.py` | 4-signal weighted score → High/Medium/Low |
| auth | `principal.py`, `passwords.py`, `tokens.py`, `service.py`, `audit.py` | Tenant-scoped principal, scrypt hashing, HS256 tokens, registration and user management, audit log |
| api | `router.py`, `schemas.py`, `deps.py` | FastAPI: `/auth/*`, `/health`, `/documents`, `/ingest`, `/documents/{id}`, `/query`, `/jobs/*`, `/audit`; bearer auth and role gates in `deps.py` |
| frontend | `App.jsx`, `api.js`, `components/*` | React SPA layout, REST API integration, state management, Chat/Composer/Sources/Trace UI |
| evaluation | `run_eval.py`, `golden_set.yaml` | 26-item golden set; retrieval, abstention and generation metrics + per-stage latency percentiles |

### A3. Hallucination controls that exist

1. **Pre-generation gate** — abstain when best rerank score < 0.30 and dense/lexical do not agree in the top 2.
2. **Closed-book prompt** — sources are nonce-fenced, declared untrusted data; `NO_ANSWER` sentinel is the only permitted response when unsupported.
3. **Numeric grounding** — every number in a sentence must appear (normalised) in the cited text before semantic scoring is even attempted.
4. **Cross-encoder entailment proxy** — each cited sentence is scored against each cited source; < 0.50 downgrades to `weak`.
5. **Conflict detection** — cross-document numeric disagreement on same-shaped claims.
6. **Confidence** — weighted blend surfaced as a label plus a component breakdown.
7. **Injection defence** — random per-request nonce on source tags, `<...source...>` tags stripped from source text, explicit instruction-ignoring clause.

### A4. Measured state (stale — see D1)

`evaluation/results/hybrid_rerank.md`, 26 items: hit@k 1.00, MRR 0.911, context recall 0.875,
context **precision 0.460**, refusal precision 0.600, false-refusal rate 0.200, must-contain
0.938, citation validity 1.00, numeric grounding 0.955. Latency p50/p95 by stage: scoring
24.0/32.7 s, validating 25.0/75.0 s, generating 3.1/7.6 s.

Those latencies were captured on the local-CPU configuration and the file references a
`Self-correction` stage that no longer exists in `answerer.py`. **The results are not
reproducible against current code and must be regenerated before submission.**

---

## Part B — Defect register

Ranked by damage. Each entry is a defect I can point at in the code, not a preference.

### B1. Confidence is half constant — the calibration is a no-op

`reranker.py:32` `calibrate()` is rank normalisation: it returns `(n - rank) / n`. So in
`answerer.py:170-171`:

- `calibrated_top` is **always exactly 1.0** (top item, rank 0).
- `calibrated_margin` is **always exactly 1/n**.

`confidence.compute()` weights those at 0.35 and 0.15. So 50% of the confidence score is a
constant for every answered question, and the "Retrieval score" row the UI shows in *How this
was scored* is a lie — it reads 1.00 regardless of whether the top passage scored 0.92 or 0.31.
The only live signals are `supported_fraction` (0.35) and `agreement` (0.15), which means the
score collapses into a 4-value lattice and the High/Medium/Low edges (0.75 / 0.45) are
effectively thresholding "did every cited sentence pass the cross-encoder".

This is the highest-value fix in the codebase: it is a correctness bug in the feature the
project is *about*.

**Fix:** keep the raw cross-encoder probability as the retrieval signal, and replace the
rank-normalisation with a real monotone calibration fitted offline on the golden set
(isotonic regression, or a 2-parameter logistic `1/(1+exp(-(a·s+b)))`, coefficients stored in
`config.py`). Margin must be the raw top1−top2 gap, clipped to [0, 1].

### B2. Retrieval leaks across the document selection

`vector_store.py:46` — `where = ... if doc_ids else None`. When `doc_ids` is empty, the filter is
dropped and the query runs against **the entire persisted collection**. `state.ready_doc_ids()`
returns `[]` when a session has no ready documents (fresh session, or all docs failed to parse),
and the composer does not block submission on that. Consequence: a user with no documents
selected silently gets answers from every document ever ingested into `data/chroma`, including
another session's uploads. Same hole in `keyword_index.py:47` (`not doc_ids or ...`).

In a single-user take-home this is a bug. In an organisation deployment it is a data-leak
incident. Empty selection must mean "no results", never "everything".

### B3. Three sequential LLM round trips before the first byte of retrieval

`answerer.py:107-121`: condense (if history) → expand_query → *then* embed → *then* search.
Both are blocking network calls to Gemini. On a follow-up question that is ~1.5–3 s of dead
time before retrieval starts, and neither call is on the critical correctness path.

**Fix (three parts):** (a) fold condense + expansion into **one** call that returns a standalone
question plus variants; (b) fire dense retrieval on the *raw* question immediately, in parallel
with that call, and union the results when the variants land (speculative retrieval — the raw
query result is almost always a superset contributor); (c) skip expansion entirely for short
keyword/factoid queries, where it measurably adds nothing.

### B4. Citation validation is the dominant cost and re-scores what was already scored

`citations.validate_batch` builds one cross-encoder pair per (sentence × cite) and the eval
shows p95 = 75 s. Meanwhile `reranker.rerank` already scored (query, chunk) for the same chunks.

**Fix:** cap pairs per sentence to its best-scoring cite; run the validation cross-encoder on an
ONNX/quantised export (`optimum` INT8 gives ~3–4× on CPU); memoise `(sentence_hash, source_hash)`
scores in a process cache; run validation **concurrently with streaming** so it costs zero
perceived latency and lands as inline markers when the stream ends.

### B5. Reranker sees only 10 of ~60 fused candidates

`config.py` `rerank_candidates: 10`, fed from `dense_k 30` + `lexical_k 30`. RRF is a weak
prefilter — it has no query-document semantics — so the cross-encoder's recall ceiling is set by
whatever RRF happened to put in the top 10. Context precision 0.460 is the symptom.

**Fix:** rerank 24–32 candidates (cheap once B4's ONNX export lands), then select context from
the reranked pool.

### B6. Per-query O(corpus) work in the hot path

- `answerer.py:421` `_parent_index` reads and `json.loads` **every parent of every selected
  document on every query**. A 200-page corpus is a multi-megabyte JSON parse per question.
- `answerer.py:360` `_mmr_order` calls `embedder.encode()` on up to 10 parent spans **at query
  time**, re-computing embeddings that already exist in Chroma.
- `keyword_index.py:28` `_rebuild()` re-tokenises the entire corpus on **every** `add_doc`, and
  `__init__` pulls `store.all_chunks()` — the full corpus text — into memory at startup.

**Fix:** parent store keyed by `parent_id` (SQLite or one file per parent) with an LRU; retrieve
child vectors from Chroma via `include=["embeddings"]` and average them per parent instead of
re-encoding; incremental BM25 (maintain doc-frequency counters and append postings) or move
lexical search into SQLite FTS5/BM25 so it is disk-backed and incremental.

### B7. Token budget is computed with the wrong tokenizer

`tokens.py` counts with the **BGE** tokenizer, but the budget it enforces
(`context_token_budget: 3000`) governs a **Gemini/Llama** prompt. Wordpiece-vs-SentencePiece
divergence is routinely 10–25%, so the context window is either under-filled (lost recall) or
over-filled (provider-side truncation of the last source, silently). It is also called in a
`while` loop in `_truncate_around_span`.

**Fix:** two counters — `count_embed_tokens` (BGE, for chunk sizing) and `count_context_tokens`
(provider tokenizer, or a calibrated chars/token ratio per provider) — and a single pass that
computes the char budget analytically instead of shrinking by 15% in a loop.

### B8. Conflict detection can only see conflicts the model already reported

`citations.detect_conflicts` compares **answer sentences to each other**. If the model reads two
contradictory sources and silently picks one, there is nothing to compare, and we report
"agreement = 1.0" — which then *raises* confidence. The check is structurally incapable of
catching the failure mode it is named after.

**Fix:** detect conflicts between **retrieved sources** before generation (numeric
disagreement on same-shaped claims, plus NLI contradiction), pass the detected conflict to the
prompt so the model is *required* to report both values with separate citations, and surface it
in the UI as a first-class panel. Post-generation checking stays as a second net.

### B9. Streaming token counts are fabricated

`answerer.py:441` `_estimate_tokens` is `len(text) // 4`. The UI always streams, so the trace
always shows made-up prompt/completion counts — in the one panel whose whole purpose is
honesty. Fix: read `usage_metadata` from the final stream chunk (Gemini and Groq both provide
it); fall back to the provider tokenizer, never to a guess.

### B10. Business logic lives in the presentation layer

`app/components/composer.py:_index_doc` is a byte-for-byte duplicate of
`src/api/router.py:_index_doc`. Question validation exists twice
(`app/state.py:validate_question` and inline in `router.py:query`). Rate limiting lives in
Streamlit session state, so it is per-browser-tab and trivially reset.

This violates the project's own layering rule. Both call sites should delegate to
`src/ingestion/pipeline.index_document(doc_id)` and `src/core/validation.py`, and rate limiting
belongs in `src/core` keyed by principal, not by session.

### B11. The HTTP API is not actually usable

`router.py:ingest_document(filename: str, data: bytes)` — FastAPI will bind `data` as a body
field, not a file part; a real client cannot upload with this signature. There is no auth, no
tenancy, no streaming endpoint, and `/query` cannot express the UI's own features (history,
mode is validated but conversation is not accepted). Needs `UploadFile`, an API-key dependency,
and an SSE `/query/stream`.

### B12. Smaller, still real

| # | Issue | Where |
|---|---|---|
| a | `nli_enabled: False` — a whole code path (`_get_nli_encoder`, `_NLI_*`) is dead config | `citations.py`, `config.py` |
| b | Answer cache is skipped for **every** turn that has history, so conversations never benefit | `answerer.py:100` |
| c | Cache key omits `ingest_version` and retrieval params — a config change serves stale answers within the TTL | `answerer.py:35` |
| d | `_dedup_sentences` removes sentences *after* the user watched them stream in, so the final render silently differs from what was shown | `answerer.py:196` |
| e | Ingest is not transactional: parents are persisted before embedding, so an embed failure orphans `parents/<id>.json` with no manifest row | `pipeline.py:_persist` |
| f | Failed-parse documents are removed from session state without a Chroma delete | `composer.py:_render_doc_pills` |
| g | BM25 has no stemming — "policies" does not match "policy"; no field boost for the section header despite it being prepended | `keyword_index.py` |
| h | `section_path` is stored as a JSON **string** in Chroma metadata, so it cannot be filtered on; no metadata filtering is exposed at all | `vector_store.py:_metadata` |
| i | `warm_up()` loads only embedder + store; the reranker and provider client load on the first question, so question #1 pays 5–15 s of model load | `app/resources.py` |
| j | Reranker drops non-candidates, so MMR and the per-doc cap operate on a pool of ≤10 | `reranker.py:14` |
| k | Background image is fetched from a **remote CDN** at runtime, contradicting the offline claim and adding a render-blocking request | `app/styles.css:37` |
| l | No cancellation: a long generation cannot be stopped, and `state.busy` blocks the whole UI | `composer.py` |

---

## Part C — Logic yet to be built

Grouped by the axis it moves. Each item states the mechanism, not just the goal.

### C1. Retrieval quality

1. **Query routing with a real taxonomy.** `detect_query_type` exists but only picks a
   formatting hint. Promote it to a router that sets a per-query retrieval plan:
   *factoid* → skip expansion, `context_k 2`, tight budget; *comparison* → expansion on,
   `context_k 6`, `max_per_doc 3`, force table output; *enumeration* → widen `context_k`, disable
   MMR diversity (we want the *whole* list, not diverse samples); *summarisation* → bypass
   retrieval scoring entirely and enter the map-reduce path (C5); *meta* ("how many documents do
   I have") → answer from the manifest without retrieval at all.
2. **Structured-table retrieval.** Tables are currently linearised into text. Persist the
   dataframe at ingest and register each table as a queryable object; for numeric lookups, run
   cell-level matching (row header × column header) and return the cell plus its surrounding
   row/column as the citation. This is where a general assistant reliably fails and a document
   tool should not.
3. **Sentence-level anchoring.** We already carry `char_span_in_parent`. Extend it to a
   sentence index so a citation resolves to an exact sentence, enabling per-claim highlight
   rather than per-span.
4. **Late-interaction reranking (optional).** A ColBERT-style scorer over the top-100 is a
   strictly better prefilter than RRF; evaluate against the cross-encoder on the golden set
   before adopting.
5. **Sticky sources across turns.** A follow-up currently re-retrieves from zero. Carry the
   previous turn's source set as a boosted prior (RRF with a third list), so "and what about
   the second one?" resolves without depending entirely on the condense call.
6. **Stemming + field-boosted lexical search.** Porter stemmer on both sides, header tokens
   weighted ~1.5×, and exact-phrase detection for quoted queries (BM25 alone cannot honour
   quotes).

### C2. Trust and calibration

1. **Fit the calibration** (B1). Produce `data/calibration.json` from the golden set with
   isotonic regression over (raw rerank score → observed correctness), and report per-bucket
   accuracy in the eval output. Target: High bucket ≥ 0.90 precision, Low bucket ≤ 0.30.
2. **Partial-answer state.** Today the outcome is binary: answer or abstain. Add `partial`:
   the model answers what *is* supported and explicitly names what is missing
   ("The retention period is 7 years [2]; the sources do not state the exception for
   litigation holds."). This is both more useful and more honest than a bare refusal, and the
   0.200 false-refusal rate says we are currently refusing things we could partly answer.
3. **Source-side conflict detection** (B8) with the conflict passed into the prompt.
4. **Self-consistency for high-stakes answers.** For low-confidence numeric answers, re-ask at
   `temperature 0` with the sources shuffled; disagreement between the two runs downgrades
   confidence and is shown as "the model was not stable on this answer".
5. **Claim-level provenance object.** Replace the flat `Sentence` with
   `Claim{text, cites, support_score, grounding, span_in_source}` so the UI can highlight the
   supporting sentence inside the source, not just the retrieved window.
6. **Answer-vs-question relevance check.** Nothing currently verifies that the answer addresses
   the question — only that it is grounded. A cheap cross-encoder (question, answer) pass
   catches the "grounded but off-topic" failure.

### C3. Latency

Current perceived latency is dominated by two CPU cross-encoder passes and three LLM round
trips. Target: **first token < 1.2 s, complete verified answer < 4 s** on the hosted provider.

| Change | Mechanism | Expected |
|---|---|---|
| Merge condense+expand, speculate on raw query | B3 | −1.0 to −2.5 s |
| ONNX INT8 cross-encoder (rerank + validation) | `optimum.onnxruntime` | 3–4× on both passes |
| Validate during streaming | move `_validate_sentences` into a worker started at first token | −25 s p95 → ~0 perceived |
| Skip rerank when RRF top-1 has dense+lexical consensus and a large margin | early-exit gate | −0.3 to −2 s on easy queries |
| Parent store keyed by id + LRU | B6 | −50 to −300 ms/query |
| Parent embeddings from Chroma instead of re-encoding | B6 | −100 to −400 ms/query |
| Semantic answer cache (embed the question, cosine ≥ 0.97 → hit) | extends `TTLCache` | near-zero on repeats |
| Warm reranker + provider in a background thread at boot | `app/resources.py` | −5 to −15 s on question #1 |
| Prefetch: embed the draft question on debounce while the user is still typing | composer → embedder | −80 to −200 ms |
| Incremental BM25 | B6 | upload latency from O(corpus) to O(doc) |

### C4. Ingestion

1. **Format coverage**: DOCX, PPTX, XLSX/CSV, MD, HTML, EML/MSG. Docling already covers most;
   the work is element mapping and per-format section inference. An org corpus is not PDFs.
2. **Per-page OCR.** Whole-document OCR is in (RapidOCR via docling, `OCR_ENABLED`/`OCR_MAX_PAGES`,
   parity measured in `evaluation/results/ocr_parity.md`), but the decision is made once for the
   document from its average char density. The remaining work is per-page: a mixed PDF -- text pages
   plus scanned forms -- is above the floor overall, so its scanned pages are still lost, and a page
   read by OCR is not flagged `ocr=true` for confidence to discount.
3. **Parallel + resumable ingest**: process files concurrently, checkpoint per document, and
   make the persist→embed→index sequence transactional (B12e).
4. **Document versioning**: keep supersession rather than replace-by-filename, so the corpus can
   answer "what changed between v2 and v3" and flag a citation that comes from a superseded
   document.
5. **Enrichment at ingest** (paid for once, reused every query): per-section one-line summaries
   added to `embed_text`; entity/acronym glossary; document-level metadata (title, date,
   author, doc type) extracted once and made filterable.
6. **PII/secret detection** at ingest, with a redaction flag surfaced on the document card.

### C5. Capabilities beyond single-turn Q&A

1. **Document digest** — map-reduce summary with citations per claim, cached per
   `(doc_id, ingest_version)`. The most requested thing users do with a document assistant.
2. **Comparison mode** — pick 2–4 documents, get a cited table of differences along
   auto-detected axes. This is the feature that most clearly beats a general chat assistant,
   because it needs per-document scoped retrieval, not one big context dump.
3. **Batch questions** — upload a CSV/list of questions, get a cited answer sheet with
   confidence per row, exportable. Directly maps to how organisations actually use this
   (RFP response, policy audit, due-diligence checklist).
4. **Saved snippets / answer library** — pin an answer with its provenance, re-verify it against
   the current corpus later ("this answer's source was superseded").
5. **Audit trail** — the JSONL trace already exists; give it a retention policy, a principal,
   and an export. Required for any regulated buyer.
6. **Eval page in-app** — run the golden set from the UI and show retrieval/abstention/
   generation metrics with the current config. Turns the eval harness from a dev script into a
   demonstrable quality claim.

### C6. Platform / non-functional

- **Multi-tenancy**: `tenant_id` on every chunk, enforced as a mandatory Chroma filter (not
  optional — see B2), separate parent stores per tenant.
- **AuthN/Z**: API keys for the HTTP layer, per-collection RBAC, document-level ACLs honoured
  at retrieval time (filter before ranking, never after).
- **Observability**: structured trace already in place; add a counter/histogram export
  (stage latency, abstention rate, confidence distribution, cache hit rate) and a
  `/metrics` endpoint.
- **Testing**: current suite is 6 files of unit tests. Missing: a retrieval regression gate that
  fails CI when golden-set MRR or refusal precision drops, an injection corpus test (documents
  containing "ignore previous instructions"), and a property test that citation ids in the
  answer always exist in the source list.

---

## Part D — Evaluation debt

1. **Regenerate all four result files** against current code; the `Self-correction` stage in
   `hybrid_rerank.md` proves they are stale (D1).
2. **Add faithfulness and answer-relevance metrics** per answer, not just retrieval metrics.
3. **Add a latency budget assertion** so a p95 regression is a test failure.
4. **Grow the golden set** past 26 items, with explicit strata: factoid, comparison,
   enumeration, table lookup, multi-hop, unanswerable, conflicting-sources, and
   injection-bearing. Report metrics *per stratum* — the aggregate hides the failure modes.
5. **Report calibration quality** (per-confidence-bucket accuracy) as a first-class metric.

---

## Part E — UI: current state, problems, target

### E1. What is rendered today

Single centred column, dark translucent panels over a background image. Hero title → chat
message list → document pills → bordered composer with an attach popover and a send button.
Each assistant message renders: answer markdown with `[n]` chips, a verification line,
conflict notes, a confidence badge + "How this was scored" expander, a "Sources" expander, and a
"How this answer was produced" expander.

### E2. What is wrong with it

| # | Problem | Why it matters |
|---|---|---|
| 1 | **Document pills delete on click.** The entire pill is a delete button with a close icon, no confirmation. There is no way to *select* documents. | The single most destructive action in the app is also the easiest to trigger by accident, and the most useful action (scope my question to these two documents) does not exist. |
| 2 | **Citation chips are inert `<span>`s.** | The core promise is "click the claim, see the evidence". Right now the user must open an expander and read four source cards to find `[2]`. |
| 3 | **Flagged sentences are printed a second time** below the answer (`_render_verification`). | The same sentence appears twice with different styling. Verification belongs *inline* on the claim, not as a duplicate list. |
| 4 | **Sources are hidden in a collapsed, unlabelled expander** with no count, no relevance score, no page link. | The evidence is the product; it is currently the least visible element on screen. |
| 5 | **Trace viewer dumps `key: value` lines** including 60-item fused lists. | Unreadable. The retrieval funnel is genuinely interesting and is currently presented as debug spew. |
| 6 | **Confidence shows a score with no action.** Two of its four components are constants (B1). | A number the user cannot interpret or act on is decoration. |
| 7 | **`state.retrieval_mode` is dead state** — settable in code, no control in the UI. | Either expose it or delete it. |
| 8 | **No empty state.** With zero documents the composer is fully enabled and submits happily (into B2's leak). | First-run experience is a blank hero and a text box that does the wrong thing. |
| 9 | **Streaming re-renders the entire answer on every token** (`chipify(text)` → `placeholder.markdown`). | Quadratic DOM work; visible jank on long answers. |
| 10 | **No stop, no regenerate, no copy, no export.** | Table stakes against any free assistant. |
| 11 | ~~**No Enter-to-send** (text area + separate button).~~ **Resolved** — composer uses `st.chat_input`, which sends on Enter and newlines on Shift+Enter natively. | Every message costs a mouse trip. |
| 12 | ~~**Hero block persists mid-conversation**, eating vertical space above the scroll.~~ **Resolved** — header only renders when there are no messages yet. | Long conversations waste a third of the viewport. |
| 13 | ~~**Errors are appended as plain assistant messages** with no retry.~~ **Resolved** — error bubbles carry the failed question and a Retry button that re-submits it. | A transient provider 503 ends the turn permanently. |
| 14 | ~~**Per-answer latency is only visible inside the trace expander.**~~ **Resolved** — elapsed time is shown in the always-visible verdict strip (`answer_card.render_verdict`), not just the trace expander. | We are fast (or slow); either way the user should see it without digging. |
| 15 | ~~**Remote background image** (B12k).~~ **Resolved** — replaced with a solid local color (`#0b0b0d`), no network fetch. | A network stall blocks first paint, and it breaks the offline story. |

### E3. Target information architecture

The rule for every element, stated so it can be enforced in review:

> **Show it only if the user can act on it or verify something with it.** Anything that is
> neither actionable nor verifiable gets deleted, not moved to an expander.

```
┌─ header (collapses to a 40px bar once the conversation starts) ────────────┐
│  Smart Document Assistant                    ● Gemini · 3 docs · 412 chunks │
├─ corpus bar ──────────────────────────────────────────────────────────────┤
│  [✓ policy.pdf 12p]  [✓ handbook.pdf 40p]  [  q3.xlsx 8p]      ⋯ manage   │
│   ↑ checkbox = scope this question    ⋯ = rename / re-index / remove       │
├─ conversation ────────────────────────────────────────────────────────────┤
│  you: What is the data retention period?                                   │
│                                                                            │
│  ┌ answer ──────────────────────────────────────────────────────────────┐ │
│  │ Records are retained for seven years ⟦2⟧ unless a litigation hold    │ │
│  │ applies ⟦4⟧.                                                          │ │
│  │                                                                        │ │
│  │ ── evidence ──────────────────────────────────────────────────────── │ │
│  │  ⟦2⟧ policy.pdf · p.14 · Retention          rerank 0.91  ▸           │ │
│  │  ⟦4⟧ handbook.pdf · p.7 · Legal Holds       rerank 0.68  ▸           │ │
│  │      (expanding shows the passage with the cited sentence underlined) │ │
│  │                                                                        │ │
│  │ ── verdict ───────────────────────────────────────────────────────── │ │
│  │  High confidence · 2/2 claims verified · 1 conflict · 2.4 s          │ │
│  │  ▸ why                                                                │ │
│  │                                                                        │ │
│  │ ── next ──────────────────────────────────────────────────────────── │ │
│  │  · What triggers a litigation hold?              (handbook p.7)      │ │
│  │  · Does retention differ for contractor records? (policy p.15)       │ │
│  │  · Compare retention in policy.pdf vs handbook.pdf                   │ │
│  │                                                                        │ │
│  │  ⧉ copy   ↻ regenerate   ⤓ export   ⚑ save                          │ │
│  └────────────────────────────────────────────────────────────────────────┘ │
├─ composer ────────────────────────────────────────────────────────────────┤
│  ⊕  Ask about your documents…                              scoped: 2 docs ⏎ │
└────────────────────────────────────────────────────────────────────────────┘
```

Four fixed sections per answer, always in this order, each with a single job:
**answer → evidence → verdict → next.** Nothing else is allowed in the card.

### E4. Component specifications

**Corpus bar** (replaces `_render_doc_pills`)
- Checkbox per document = query scope. Scope is persisted in session state and echoed in the
  composer ("scoped: 2 docs"). Zero checked = scope everything *in this session*, never the
  whole store (B2).
- Destructive actions move behind a `⋯` menu with an explicit confirm.
- Each pill shows `name · pages`; parse failures show an inline reason and a retry.
- Ingest progress is a real determinate bar driven by actual stage counts, not `min(+20, 90)`.

**Answer body**
- Citation chips are **buttons**. Clicking `⟦2⟧` expands evidence row 2, scrolls to it, and
  highlights the supporting sentence inside the passage. This one interaction is the product.
- Verification renders **inline** as a chip modifier: verified claims get a quiet chip, `weak`
  gets an amber underline on the sentence, `unsupported` gets a strikethrough-adjacent marker
  and a one-line "not found in ⟦2⟧" beneath. Delete the duplicate flagged-sentence list (E2.3).
- Streaming appends only the new delta into a dedicated stream slot; chipify runs once on
  completion. No full re-render per token.

**Evidence section** (replaces the Sources expander)
- Always visible when an answer has sources — not an expander. Collapsed to one row per source:
  `⟦n⟧ filename · p.x · section · score`. Expanding reveals the passage with the retrieved span
  marked and the cited sentence underlined.
- Row order matches citation order in the answer, not rank order.
- "Open at page" link when a viewer is available.

**Verdict strip**
- One line: `{label} confidence · {k}/{n} claims verified · {conflicts} · {elapsed}`.
- `▸ why` reveals the component breakdown — with **honest, varying** numbers once B1 is fixed —
  plus a plain-language sentence: "Lowered because 1 of 3 claims could not be matched to its
  cited passage."
- Conflicts are a dedicated block inside the verdict, showing both values side by side with
  their citations, not a floating note.

**Follow-up suggestions** (the "next" section — explicitly requested)
- **Generated from evidence we retrieved but did not use.** After context assembly there are
  typically 6–20 ranked passages of which 4 were sent to the model. Passages 5–10 are
  *known-relevant, unconsumed* material. Suggesting questions from them means **every suggestion
  is answerable from the corpus** — a general assistant cannot make that guarantee because it
  has no retrieval pool to draw from.
- Generation: one cheap LLM call, issued **in parallel with the main generation** (zero added
  latency), prompted with the unused passage headers + the current question, asked for three
  short questions. Each suggestion carries the `doc/page` it came from and is displayed with it,
  so the user can see *why* it is being offered.
- Always include one **structural** suggestion derived from the answer's shape, not from the
  LLM: multiple documents cited → "Compare X vs Y"; a conflict was detected → "Show both
  values and their sources"; a table was cited → "Break down the table by row".
- On **abstention**, the same mechanism becomes recovery: the near-miss passage drives
  "Did you mean: …?" with the page shown. This turns our 0.200 false-refusal rate from a dead
  end into a redirect.
- Rendered as plain text rows with the provenance in dim text. No cards, no icons. Click =
  submit. Cap at 3.

**Trace viewer** (rebuilt)
- **Stage timeline**: horizontal bars proportional to duration, labelled with ms. Instantly
  shows where a slow answer went.
- **Retrieval funnel**: `30 dense → 28 lexical → 47 fused → 24 reranked → 4 in context`, with
  the drop reason for anything eliminated by a threshold.
- **Candidate table**: chunk, doc, page, dense rank, lexical rank, rerank score, and a
  ✓/✗ for "made it into context". Sortable. This is what an evaluator actually wants to read.
- **Prompt**: collapsed, with the exact bytes sent, token count from provider usage (B9).
- Delete the raw `key: value` dump entirely.

**Composer**
- Enter sends, Shift+Enter newlines. Stop button during generation. Character counter only
  when within 10% of the limit.
- Empty-corpus state: composer replaced by a drop target with two sample documents and two
  example questions, so a first-run user reaches a cited answer in one click.

### E5. Visual system (unchanged constraints, tightened)

Keep: single centred column, `--panel`/`--line` tokens, 12px radius, hairline borders, no
gradients/glow/animation, monochrome material icons only, everything through `html.escape`.

Add: the background image ships **locally** (B12k); the four answer sections are separated by
hairline rules, not headings, so the card reads as one object; the only accent colour in the
whole app is on confidence and conflict, so those two signals are never competing for
attention; the header collapses to a status bar showing provider, document count and chunk
count — the one piece of persistent chrome that is genuinely informative.

---

## Part F — Where this beats a free general assistant

Stated plainly, because it should drive prioritisation:

| Dimension | Free ChatGPT / Gemini | This tool, once Parts C–E land |
|---|---|---|
| Provenance | Sometimes quotes; cannot guarantee the quote is from your file | Every claim carries a document, page and section; the exact supporting sentence is highlighted |
| Refusal | Answers anyway, fluently | Explicit gate with a stated reason, a near-miss, and a recovery suggestion |
| Calibration | No confidence, or an unearned one | Calibrated score fitted on a golden set, with per-bucket accuracy published |
| Conflicts | Silently picks one value | Detects source-side disagreement and reports both values with citations |
| Auditability | None | Per-query JSONL trace: sources, scores, prompt, timings, principal |
| Scope control | Whole context window, all files at once | Per-document scoping enforced at the retrieval filter |
| Determinism | Non-reproducible | Content-hashed documents, versioned ingest, cached answers, regeneratable eval |
| Latency | 3–10 s | Target: first token < 1.2 s, verified answer < 4 s |
| Data boundary | Leaves the org | Local embeddings, local rerank, local store; provider swappable to fully-offline Ollama |

We do not try to out-write a frontier model. We make the answer *checkable in one click*, and we
refuse when the corpus does not support it.

---

## Part G — Execution order to the deadline

Deadline: 2026-09-24 04:00. Time is the binding constraint, so this is ordered by
(demonstrable value ÷ risk), with a hard cut line. Items below the cut do not get started.

**Tier 1 — must ship (correctness and the leak)**
1. **B1** — real calibration: raw rerank score + fitted logistic/isotonic, honest margin.
   Without this the confidence feature is broken and the "why" panel is misleading. ~45 min.
2. **B2** — empty `doc_ids` must return nothing in both `vector_store` and `keyword_index`;
   composer blocks submission with no scoped documents. ~20 min.
3. **B9** — real token counts from provider usage. ~15 min.
4. Regenerate `evaluation/results/*` and update the README's results table. ~30 min (mostly
   waiting).

**Tier 2 — the UI the user asked for**
5. **Answer card restructure** into answer → evidence → verdict → next, with evidence always
   visible and the duplicate flagged-sentence list deleted (E2.3, E4). ~60 min.
6. **Clickable citation chips** → expand + highlight the matching evidence row. The single
   highest-impact interaction in the product. ~45 min.
7. **Follow-up suggestions** from unused retrieved passages, generated in parallel with the main
   answer, with provenance shown, plus the structural suggestion and the abstain-recovery
   variant. ~60 min.
8. **Corpus bar** with scope checkboxes and delete moved behind a confirm (E4). ~45 min.
9. **Trace viewer rebuild**: stage timeline + retrieval funnel + candidate table. ~45 min.
10. **Composer**: Enter-to-send, stop button, empty state. ~30 min.

**Tier 3 — speed, if the clock allows**
11. ~~**B3** — merge condense+expand into one call, speculate on the raw query.~~ **Resolved** — `query.condense_and_expand` gets both in one LLM call; raw-query dense/lexical search now runs concurrently with that call instead of waiting on it. ~40 min.
12. ~~**B4** — validate concurrently with streaming (defer the ONNX export).~~ **Resolved** — `answerer.answer_question` parses completed sentences out of the growing stream and submits each new batch to `citations.validate_batch` on a background thread as soon as it's stable, instead of waiting for the full answer; only the unvalidated tail is left to the post-stream "Validating citations" stage. ONNX export still deferred. ~40 min.
13. ~~**B6** — parent LRU + MMR embeddings from Chroma.~~ **Resolved (mostly)** — `pipeline.py` caches each doc's parsed parents/children JSON in a `TTLCache` (unbounded TTL, `parent_cache_size` LRU entries), invalidated on ingest/replace/delete, so `_parent_index` no longer re-reads and re-parses disk JSON on every query; `_mmr_order` now pulls each candidate's child embedding back from Chroma (`VectorStore.get_embeddings`) instead of calling `embedder.encode()` on the span text at query time. `keyword_index.KeywordIndex` now tokenizes only newly added chunks and keeps the token lists around, instead of re-tokenizing the whole corpus on every `add_doc` — but `BM25Okapi`'s own IDF pass still rescans the full token corpus on rebuild, and `__init__` still loads the full corpus into memory via `store.all_chunks()`; true incremental BM25 or a move to SQLite FTS5 is deferred. ~30 min.
14. ~~**B12i** — warm reranker and provider in a background thread.~~ **Resolved** — `resources.warm_up()` still loads the embedder and vector store inline (needed for the very first search), then spawns a daemon thread that populates the `reranker()`/`llm_client()` `st.cache_resource` singletons; `st.cache_resource`'s own lock makes this safe if a question arrives before the thread finishes. ~15 min.
15. ~~**B12k** — vendor the background image locally.~~ **Resolved differently** — remote image dropped in favor of a solid local color (see E2 row 15); no image to vendor. ~10 min.

**Cut line.** Everything else — B5, B7, B8, B10, B11, B12 a/c/d/e/f/g/h/j, all of C1–C6, Part D
beyond item 1 — is documented here, named in the README's "Known limitations" with the reason it
was deferred, and left unstarted. Shipping Tier 1 + Tier 2 complete and honest is worth more
than shipping Tier 3 half-done.

**Tier 1 item 1 (B1) — done.** `reranker.calibrate()` no longer rank-normalises (was always
`1.0` for the top hit); it now does Platt scaling in logit space —
`sigmoid(a * logit(raw) + b)` — over the raw cross-encoder probability, with `a`/`b` in
`RetrievalConfig` (`calibration_logistic_a/b`, default `1.0`/`0.0`, i.e. identity until fitted
on the golden set). `calibrated_top` in `answerer.py` now varies with the actual top score
instead of being constant. `calibrated_margin` is the raw top1−top2 gap clipped to `[0, 1]`,
no longer passed through calibration, per the fix note above. Golden-set logistic fit (the
`a`/`b` values) is still open — tracked under Part D item 1 (regenerate eval) — and C2.1.

**Tier 1 item 2 (B2) — done.** `VectorStore.query()` and `KeywordIndex.query()` now return `[]`
immediately when `doc_ids` is empty, instead of dropping the Chroma `where` filter and searching
the whole persisted collection. The composer's send button is now `disabled` when
`state.has_ready_docs()` is false, and `_submit()` re-checks the same condition server-side
before a question is recorded, so an empty-scope query can no longer reach retrieval at all.

**Tier 1 item 3 (B9) — done.** `Provider` now carries `last_usage: tuple[int, int]`
(prompt_tokens, completion_tokens), updated during `stream()` from each SDK's real usage data —
Ollama's final `done` chunk (`prompt_eval_count`/`eval_count`), Gemini's per-chunk
`usage_metadata`, and Groq's `stream_options={"include_usage": True}` final usage event (with a
`not event.choices` guard, since that event carries no delta). `_generate_text`'s streaming
branch in `answerer.py` reads `client.last_usage` instead of `_estimate_tokens`, which is
deleted. The trace panel's token counts are now real for both the streamed and non-streamed
path.

### Sequencing note

Tier 1 must land before Tier 2 item 5, because the verdict strip renders the calibration output
— building the UI against the constant-valued score would mean building it twice.

---

## Part H — Open decisions

1. **Default provider.** `config.py` defaults to Gemini, but the README and CLAUDE.md both claim
   "fully offline at runtime". One of the two must change. Recommendation: keep Gemini as the
   default for demo latency, and state the offline Ollama path explicitly as a supported
   configuration rather than the headline claim.
2. **Where lexical search lives.** In-memory BM25 is fast and simple but O(corpus) to update and
   duplicates the corpus in RAM. SQLite FTS5 is incremental and disk-backed but adds a
   dependency and slightly different scoring. Recommendation: keep `rank_bm25` for submission,
   note FTS5 as the scaling path.
3. **NLI.** Enable it with a quantised export, or delete the dead path. Recommendation: delete
   for submission (B12a), reintroduce alongside source-side conflict detection (B8).
4. **Retrieval mode control.** Expose `state.retrieval_mode` as a developer control inside the
   trace panel, or delete the state key. Recommendation: expose it in the trace panel — it makes
   the dense/hybrid/hybrid_rerank ablation in the eval results demonstrable live.
