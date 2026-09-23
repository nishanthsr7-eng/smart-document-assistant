# Improvement plan — assignment review, 2026-09-24

Self-contained work queue from a full audit of the repo against
`GenAI_Take_Home_Assignment_Candidate.pdf`. Each step is independent enough to pick up in a
fresh chat: state, evidence, fix, acceptance.

Score at time of audit: **82/100**.

| Area | Weight | Score | Loss |
|---|---|---|---|
| GenAI capability | 30 | 25 | `hybrid` mode broken; README describes grounding the code doesn't have |
| Coding skill | 20 | 17 | Subsystems built but unwired; `requirements.txt` can't serve an upload |
| Problem solving | 20 | 17 | Committed eval results are from an older pipeline |
| Creativity | 15 | 14 | — |
| Communication | 15 | 9 | README false in load-bearing places; no demo video in repo |

~7 of the 18 lost points are documentation accuracy, not engineering. Steps 1–4 are roughly
two hours and worth about ten points. Steps 5–11 are polish on something already past the bar.

**Status (2026-09-24): steps 1–10 done. Only step 11's video remains** — the page-cap
error-message half of step 11 is fixed; the video itself needs to be recorded outside this
session. The reference-findings section below has also been triaged (one line added to Known
Limitations; the rest are intentionally-not-fixed by this codebase's own conventions).

---

## Already done (2026-09-24)

Dead code removed; 49 tests pass, `npx oxlint src` clean.

- `chipify()` + `CITE_RE` in `frontend/src/components/AnswerCard.jsx` — never called
- `thinkingLabel`, `clearMessages` in `frontend/src/hooks/useChat.js` — returned, never consumed
- `message.isThinking` branch in `ChatWindow.jsx` — no code ever set the flag
- `onSubmit` prop to ChatWindow, `effectiveIds` prop to Composer — passed, never destructured
- NLI contradiction detection in `src/trust/citations.py` — gated on frozen `nli_enabled = False`;
  took `ModelConfig.nli_name` with it
- `CitationCheck.suggested_source` / `Sentence.suggested_cite` — always `None`, never surfaced
- `POST /ingest/sample` — unused by the frontend, joined an unvalidated filename onto a server path
- `hasattr(near_miss, "section_path")` guard in `router.py` — `Hit` always has the field
- 5 unused CSS rules: `chat-scroll`, `section-label`, `doc-row-error`, `empty-state`, `visually-hidden`
- Streamlit entry in `.claude/launch.json` — pointed at deleted `app/main.py`
- Unused `useCallback` import in `Composer.jsx`

**Left in place deliberately at audit time** — unreachable then, one wire from being a feature.
These were steps 5, 7, 8 and 10 below (`Provider.stream()`, `ConfidenceResult.components`,
`query.condense*`, `parsers.FigureCaptioner`) — all four are now wired; see their step notes.

---

## Step 1 — Fix the `hybrid` score-scale bug (done)

**Severity: highest.** One of three user-selectable modes silently refuses every question, and
it's the one the README names as default. A reviewer who clicks "hybrid" in the demo sees a
broken product.

**Evidence.** `src/retrieval/hybrid.py:42` — fused hits carry an RRF score whose ceiling is
`2/(rrf_k + 1) = 2/61 ≈ 0.033`. That value is then compared against:

- `src/trust/abstention.py:23` — `abstain_threshold: 0.3`
- `src/generation/answerer.py:442` — `min_source_score: 0.2`

Both thresholds are calibrated for a cosine / sigmoid-probability scale. So `hybrid` abstains
unless `retrieval_consensus` happens to be true (top fused hit ranked ≤2 by *both* dense and
lexical). `dense` (cosine) and `hybrid_rerank` (sigmoid probability) are unaffected.

**Fix.** Either normalize fused scores to `[0,1]` before the gate, or give each retrieval mode
its own threshold pair in `TrustConfig`. Normalizing is simpler; per-mode thresholds are more
honest about the fact that the three scales mean different things.

**Acceptance.** `hybrid` mode answers the answerable golden-set items. Re-run the threshold
sweep (`evaluation/run_eval.py:172`) for `hybrid` and pick the threshold from the data, not by
analogy to the cosine one.

---

## Step 2 — Rewrite the README against the code as it actually is (done)

**Idea.** In a take-home the README is the primary evidence that you understand your own
system. Every false claim reads as a comprehension failure, not a typo.

Each of these is currently wrong in `README.md`:

| Claim | Reality |
|---|---|
| "No API keys required" / "Fully offline at runtime" | `ModelConfig.llm_provider` defaults to `gemini`; a key is required |
| Embeddings `all-MiniLM-L6-v2` | `src/core/config.py` → `BAAI/bge-base-en-v1.5` |
| "Structured JSON output … schema enforced via Ollama's `format` parameter" | Plain text with inline `[n]` markers, parsed by `prompts.parse_citations`. The whole numbered section describes a system that no longer exists |
| "Default mode: `hybrid`" | `RetrievalConfig.mode = "hybrid_rerank"` |
| "Cross-encoder reranking … disabled by default" | It is the default |
| Edge-case table cites `app/state.py`, `app/components/composer.py` | Streamlit files, deleted |
| Env-var table lists only `OLLAMA_*` | Missing `LLM_PROVIDER`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `GROQ_API_KEY`, `GROQ_MODEL`, `HF_TOKEN` |
| "No streaming … the LLM outputs structured JSON, which can't be streamed" | Wrong reason; all three providers implement `stream()` (see step 5) |

Also add a short **provider choice** section that owns the Ollama-vs-free-tier-API decision
explicitly. That is a defensible trade-off, and stating it plainly reads far better than the
current claim of being offline while defaulting to a cloud key.

`CLAUDE.md` has the same drift in its Stack line (says Ollama `qwen3:8b` + all-MiniLM) — fix
it in the same pass.

**Acceptance.** Every factual claim in README.md traceable to a line in the code.

---

## Step 3 — Make `requirements.txt` actually install a working app (done)

**Idea.** The first thing a grader does is a clean install; the second is upload a file.

- `python-multipart` is missing — FastAPI's `UploadFile` requires it, so `POST /ingest`
  returns 500 on a clean environment.
- `httpx` is imported directly in `src/generation/client.py` but only arrives transitively via
  `ollama` / `chromadb`.

**Acceptance.** Fresh venv, `pip install -r requirements.txt`, upload a PDF through the UI,
get an answer. No other manual installs.

---

## Step 4 — Re-run the evaluation and regenerate the results (done)

**Idea.** Stale metrics are worse than no metrics: they assert something checkable and false.

`evaluation/results/*.md` report stage names `Scoring` and `Self-correction`. Neither exists in
the current `answerer.py`, whose stages are `Searching`, `Reranking`, `Assembling context`,
`Generating`, `Validating citations`, `Abstaining`. The committed numbers describe a previous
architecture, and README's "Evaluation Results" section quotes them.

**Do this after step 1**, so `hybrid` numbers are real.

```bash
python -m evaluation.run_eval --mode dense --generate
python -m evaluation.run_eval --mode hybrid --generate
python -m evaluation.run_eval --mode hybrid_rerank --generate
```

**Acceptance.** Results regenerated for all three modes; README quotes the numbers for the mode
that is actually the default.

---

## Step 5 — Wire streaming through `/query` (done)

**Idea.** Perceived latency is most of perceived quality, and the hard part is already written.

Currently unreachable: `Provider.stream()` on all three providers, the `on_token` path in
`answerer._generate_text`, and `_submit_completed_sentences` — an optimization that overlaps
cross-encoder citation validation with token generation. `/query` never passes `on_token`, so
the user waits in silence for the whole answer.

**Fix.** Convert `/query` to SSE (or a streamed response), pass `on_token`, consume it in
`useChat`. Keep the non-streaming path for `evaluation/run_eval.py`.

Watch the answer cache: `answer_question` already replays `cached.answer_text` through
`on_token` in one shot (`answerer.py:127`) — that is correct, leave it.

**Acceptance.** Tokens appear progressively in the UI; `Validating citations` stage duration
drops because validation overlapped generation.

---

## Step 6 — Fix the delete / replace index leaks (done)

**Idea.** An index that only grows is a correctness bug waiting for a longer demo.

Two leaks, neither producing wrong answers today (the `doc_id` filter in
`VectorStore.query` saves you), but both unbounded:

1. `DELETE /documents/{doc_id}` drops the manifest entry and the Chroma vectors, but never
   touches `KeywordIndex`, which holds its corpus in memory. BM25's IDF statistics drift.
2. Re-ingesting a same-named file with new bytes: `pipeline.ingest` → `_delete(replaced, ...)`
   removes the old parent JSON and manifest entry, but the old doc's vectors stay in Chroma
   forever and its chunks stay in `KeywordIndex`.

**Fix.** Add `KeywordIndex.remove_doc(doc_id)` and call it from the delete route. Have the
ingest route read `IngestReport.replaced_doc_id` — the field already exists and is populated,
it is simply never read — and purge the superseded doc from both stores.

**Acceptance.** Upload → delete → re-upload → `VectorStore.all_chunks()` count returns to the
expected number rather than accumulating.

---

## Step 7 — Surface the confidence breakdown (done)

**Idea.** The stated product thesis is "provenance, refusal, calibration, auditability".
Showing the score without its inputs undercuts exactly that.

`ConfidenceResult.components` (`src/trust/confidence.py:36`) carries four labelled signals —
Retrieval score, Top-1/top-2 margin, Supported claims, Source agreement — and is dropped at the
API boundary. The UI shows "High confidence" with no explanation.

**Fix.** Add `components` to `ConfidenceOut` in `src/api/schemas.py`, map it in
`router._to_response`, render the four signals under the confidence dot in `SourcesPanel.jsx`.

~20 lines, and the most demo-able thing in the trust layer.

---

## Step 8 — Decide on conversation memory: wire it or delete it (done, 2026-09-24)

Wired: `QueryRequest.history`, threaded through `router.query` to `answer_question`;
`useChat` keeps the last 3 answered turns and sends them each request. README limitation
#1 removed, single-session caveat added.



**Idea.** Half-built features are a liability in review. Either answer is fine; drifting is not.

`answer_question` accepts `history`. `query.condense_and_expand` resolves a follow-up into a
standalone question and generates search variants in one LLM call. `GenerationConfig.history_turns`
exists. The router never sends history; the frontend never stores it. Meanwhile README lists
"No conversation memory" as limitation #1.

**Wiring it is ~30 lines**: store `(question, answer_text)` pairs in `useChat`, send the last N
in the `/query` body, thread through to `answer_question`. The cache-bypass guard for history
already exists (`answerer.py:123`).

It also removes a README limitation and directly satisfies the first creative feature the
assignment PDF suggests.

If instead deleting: remove `query.condense`, `query.condense_and_expand`, `CONDENSE_SYSTEM`,
`CONDENSE_EXPAND_SYSTEM`, `build_condense_prompt`, `history_turns`, and the `history` parameter.

---

## Step 9 — Test the API layer (done, 2026-09-24)

Added `tests/test_api.py`: 10 `TestClient` tests covering the acceptance list below.
`/ingest` happy-path and replace tests monkeypatch `router.ingest`/`load_children` and
`deps.vector_store`/`keyword_index`/`embedder` so they don't touch the real Chroma/parents
store or load the real embedding model; the reject cases (oversize, wrong extension,
empty) exercise the real `validate_upload` since it fails before any disk I/O.
`/query` validation cases hit `_validate_query` directly, no mocking needed. 59/59 tests
pass.



**Idea.** The 49 existing tests cover every module *except* the one a grader actually exercises.
There is no `tests/test_api.py`.

FastAPI `TestClient` tests for:

- `POST /ingest` — happy path, oversize file, wrong extension, 0-byte
- `POST /query` — empty question, over-length, unknown mode, special-character ratio reject
- `GET /documents` and `DELETE /documents/{id}` round-trip

~60 lines for visibly broader coverage.

---

## Step 10 — Decide on BLIP figure captioning (done, 2026-09-24)

Wired: `deps.figure_captioner()` (lazy `lru_cache` singleton, same pattern as `deps.embedder()`)
is constructed and passed to `ingest()` from `/ingest`. Moved `validate_upload` to run in the
route *before* that construction, so a rejected upload (bad extension, oversize, empty) never
triggers the BLIP model load. Updated the README tech-choices row and OCR limitation, and the
`CLAUDE.md` stack line. No figure-bearing sample PDF in the repo to eyeball a caption against;
worth a manual check with a real figure PDF before the demo.



**Idea.** An advertised feature that never runs is a false claim in the tech-choices table.

`parsers.FigureCaptioner` is a real class with a real config entry (`ModelConfig.blip_name`).
`pipeline.ingest` is only ever called with `captioner=None`, so every figure becomes the literal
string `[Figure, p.3]`. README lists BLIP in the tech-choices table as shipped.

**Wiring it is barely more work than removing it**: the captioner is already threaded through
`parse()` — construct one lazily (behind `lru_cache`, like `deps.embedder()`) and pass it from
the ingest route. Otherwise cut the class, `blip_name`, the README row, and the "mixed PDFs …
caption the scanned parts via BLIP" claim in limitation #5.

---

## Step 11 — Page-cap error message, then record the video

**Bug fix done (2026-09-24).** Added `DocumentTooManyPages` to `src/core/errors.py`; the
page-count check in `parsers._preflight_pdf` now raises it instead of the mismatched
`DocumentTooLarge(20)`, so a 300-page PDF gets "Document exceeds the 200-page limit."
instead of a false MB-limit message. Updated `tests/test_parsers.py` to match.

**Video still outstanding** — not something I can produce; the beat structure below is
ready whenever it's recorded.

**Video.** 1 of 6 required submission items, currently absent. No amount of code quality
substitutes for it. Shoot to the PDF's exact beat structure:

- 1 min — problem and approach
- 2–3 min — demo: upload, ask, show answer + source, ask an unanswerable question, show the
  creative feature
- 1–2 min — architecture and data flow
- 1 min — technical decisions and hallucination handling
- 30–60 s — what you'd improve with another two days

Make sure the unanswerable-question beat runs in a mode not affected by step 1.

---

## Reference: audit findings not covered above

Minor, noted for completeness.

- `abstention.check` compares thresholds against three different score scales depending on mode
  (cosine / RRF / sigmoid). Step 1 fixes the broken one; the general design smell remains.
- `answer_question`'s `reranker` parameter is typed `Optional[Reranker]` but is dereferenced
  unconditionally in `hybrid_rerank` mode and in `citations.validate_batch`. Latent `None` deref;
  the router always passes one.
- `client._retry_transient` falls off the end of its loop implicitly returning `None` on a path
  that is unreachable but not provably so to a type checker.
- `doc_ids` in `/query` is client-supplied and unvalidated against the manifest. Harmless for a
  single-user local app; worth one line in "Known limitations".
- `OVERHAUL.md` sits at the repo root, which `CLAUDE.md` forbids ("No loose files at root except
  config files"). It and the 665-line `docs/architecture.md` are internal planning documents —
  consider whether they belong in a submission package.
