import hashlib
import re
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Optional

from src.auth.principal import Principal
from src.core.cache import ANSWER_CACHE
from src.core.config import SETTINGS
from src.core.errors import GenerationError, ModelUnavailable
from src.core.tokens import count_tokens
from src.core.tracing import OnStage, Trace
from src.generation.client import Provider
from src.generation.prompts import (
    DIDYOUMEAN_SYSTEM,
    FOLLOWUP_SYSTEM,
    NO_ANSWER,
    SYSTEM,
    build_didyoumean_prompt,
    build_followup_prompt,
    build_user_prompt,
    parse_citations,
)
from src.ingestion.chunker import ParentChunk
from src.ingestion.pipeline import load_parents
from src.retrieval import query
from src.retrieval.embedder import Embedder
from src.retrieval.hybrid import FusedHit, fuse, merge_dense
from src.retrieval.keyword_index import KeywordIndex
from src.retrieval.reranker import Reranker
from src.retrieval.vector_store import Hit, VectorStore
from src.trust import abstention, citations, confidence
from src.trust.confidence import ConfidenceResult

OnToken = Callable[[str], None]

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\[])")
_CITE_STRIP_RE = re.compile(r"\s*\[\d+\]")

def _cache_key(
    tenant_id: str, question: str, doc_ids: list[str], mode: str, generate: bool
) -> str:
    # tenant_id is part of the key: without it two tenants asking the same question of
    # same-named documents would share one cached answer.
    raw = "\x1f".join(
        [tenant_id, question.strip(), "|".join(sorted(doc_ids)), mode, str(generate)]
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _finish(key: Optional[str], answer: "Answer") -> "Answer":
    answer.trace.log()
    if key is not None:
        ANSWER_CACHE.set(key, answer)
    return answer


@dataclass
class Sentence:
    text: str
    cites: list[int]
    citation_status: str = "uncited"


@dataclass
class Source:
    id: int
    filename: str
    pages: str
    section_path: str
    text: str
    char_span_in_parent: tuple[int, int]
    score: float


@dataclass
class Conflict:
    claim: str
    source_ids: list[int]


@dataclass
class UnusedPassage:
    filename: str
    pages: str
    section_path: str


@dataclass
class Suggestion:
    text: str
    filename: Optional[str] = None
    pages: Optional[str] = None
    label: Optional[str] = None


@dataclass
class Answer:
    status: str
    sentences: list[Sentence]
    sources: list[Source]
    trace: Trace
    hits: list[Hit]
    answer_text: str = ""
    abstain_reason: Optional[str] = None
    near_miss: Optional[Hit] = None
    conflicts: list[Conflict] = field(default_factory=list)
    confidence: Optional[ConfidenceResult] = None
    suggestions: list[Suggestion] = field(default_factory=list)


def answer_question(
    question: str,
    doc_ids: list[str],
    embedder: Embedder,
    store: VectorStore,
    client: Provider,
    keyword_index: KeywordIndex,
    reranker: Reranker,
    principal: Principal,
    mode: str = SETTINGS.retrieval.mode,
    on_stage: Optional[OnStage] = None,
    on_token: Optional[OnToken] = None,
    history: Optional[list[tuple[str, str]]] = None,
    generate: bool = True,
) -> Answer:
    trace = Trace()
    tenant_id = principal.tenant_id

    cache_key: Optional[str] = None
    if not history:
        cache_key = _cache_key(tenant_id, question, doc_ids, mode, generate)
        cached: Optional[Answer] = ANSWER_CACHE.get(cache_key)
        if cached is not None:
            if on_token is not None and cached.answer_text:
                on_token(cached.answer_text)
            return cached

    search_query = question
    retrieval_consensus = False
    with trace.stage(f"Searching {len(doc_ids)} document(s)", on_stage) as payload:
        payload["mode"] = mode

        with ThreadPoolExecutor(max_workers=3) as pool:
            # Speculate on the raw question: dense/lexical search on it starts immediately and
            # runs concurrently with the condense/expansion LLM call(s) below, instead of
            # waiting on them serially.
            raw_vector = embedder.encode_query([question])[0]
            raw_dense_future = pool.submit(
                store.query, raw_vector, SETTINGS.retrieval.dense_k, doc_ids, tenant_id
            )
            lexical_future = (
                pool.submit(
                    keyword_index.query,
                    question,
                    SETTINGS.retrieval.lexical_k,
                    doc_ids,
                    tenant_id,
                )
                if mode != "dense"
                else None
            )

            variants: list[str] = []
            if history:
                if SETTINGS.retrieval.query_expansion:
                    search_query, variants = query.condense_and_expand(client, history, question)
                else:
                    search_query = query.condense(client, history, question)
            elif SETTINGS.retrieval.query_expansion:
                expanded = query.expand_query(client, question)
                search_query, variants = expanded[0], expanded[1:]
            payload["standalone_question"] = search_query

            expansion_texts = ([search_query] if search_query != question else []) + variants
            hyde_text = query.hyde_passage(client, search_query) if SETTINGS.retrieval.hyde else None
            extra_texts = expansion_texts + ([hyde_text] if hyde_text else [])

            extra_dense_future = None
            if extra_texts:
                extra_vectors = embedder.encode_query(extra_texts)
                extra_dense_future = pool.submit(
                    lambda: [
                        store.query(v, SETTINGS.retrieval.dense_k, doc_ids, tenant_id)
                        for v in extra_vectors
                    ]
                )

            raw_hits = raw_dense_future.result()
            extra_hits_lists = extra_dense_future.result() if extra_dense_future is not None else []
            hits = merge_dense([raw_hits] + extra_hits_lists, SETTINGS.retrieval.dense_k)
            lexical_hits = lexical_future.result() if lexical_future is not None else []

        payload["expanded_queries"] = [question] + expansion_texts
        payload["dense_hits"] = len(hits)
        payload["dense"] = _hit_summaries(hits)

        if mode != "dense":
            fused = fuse(hits, lexical_hits)
            payload["lexical_hits"] = len(lexical_hits)
            payload["lexical"] = _hit_summaries(lexical_hits)
            payload["fused"] = [
                {
                    "chunk_id": f.hit.chunk_id,
                    "doc_id": f.hit.doc_id,
                    "filename": f.hit.filename,
                    "page": f.hit.page_start if f.hit.page_start == f.hit.page_end else f"{f.hit.page_start}-{f.hit.page_end}",
                    "dense_rank": f.dense_rank,
                    "lexical_rank": f.lexical_rank,
                }
                for f in fused
            ]
            hits = [f.hit for f in fused]
            retrieval_consensus = _agree_in_top_ranks(fused[0]) if fused else False
            payload["retrieval_consensus"] = retrieval_consensus

    calibrated_top: float = 0.0
    calibrated_margin: float = 1.0
    if mode == "hybrid_rerank":
        with trace.stage("Reranking", on_stage) as payload:
            scored_hits = reranker.rerank(search_query, hits)
            top_score = scored_hits[0].score if scored_hits else None
            margin = 1.0 if len(scored_hits) < 2 else scored_hits[0].score - scored_hits[1].score
            payload["top_score"] = top_score
            payload["margin"] = margin
            payload["reranked"] = _hit_summaries(scored_hits)
            # Calibrated top score for confidence; raw top_score (uncalibrated) drives abstention.
            cal = reranker.calibrate([h.score for h in scored_hits])
            calibrated_top = cal[0] if cal else 0.0
            calibrated_margin = max(0.0, min(1.0, margin))
        hits = scored_hits
    else:
        top_score = hits[0].score if hits else None
        margin = 1.0 if len(hits) < 2 else hits[0].score - hits[1].score
        calibrated_top = top_score or 0.0
        calibrated_margin = margin

    gate = abstention.check(hits, top_score, retrieval_consensus)
    if gate.should_abstain:
        with trace.stage("Abstaining", on_stage) as payload:
            payload["reason"] = gate.reason
        suggestions = _didyoumean(client, question, gate.near_miss) if generate else []
        return _finish(
            cache_key,
            Answer(
                "abstained", [], [], trace, hits,
                abstain_reason=gate.reason, near_miss=gate.near_miss, suggestions=suggestions,
            ),
        )

    with trace.stage("Assembling context", on_stage) as payload:
        sources, unused, context_chunk_ids = _assemble_sources(hits, store, tenant_id)
        payload["sources"] = len(sources)
        payload["context_chunk_ids"] = context_chunk_ids

    if not sources:
        reason = "No matching passages were found for this question."
        return _finish(cache_key, Answer("abstained", [], [], trace, hits, abstain_reason=reason))

    if not generate:
        return _finish(cache_key, Answer("retrieved", [], sources, trace, hits))

    prompt_sources = [(s.id, f"{s.filename} {s.section_path}".strip(), s.text) for s in sources]
    source_texts = {s.id: s.text for s in sources}

    validated_prefix: list[Sentence] = []
    validation_futures: list[tuple[list[Sentence], "Future[list[citations.CitationCheck]]"]] = []
    validation_pool = ThreadPoolExecutor(max_workers=1)

    def _submit_completed_sentences(accumulated_text: str) -> None:
        # Overlaps cross-encoder validation with token generation (B4): only sentences before the
        # trailing one are submitted, since the last sentence in a partial stream may still grow.
        nonlocal validated_prefix
        parsed = _dedup_sentences(_parse_sentences(accumulated_text))
        complete = parsed[:-1] if len(parsed) > 1 else []
        new = [s for s in complete[len(validated_prefix):] if s.cites]
        if new:
            validation_futures.append((new, validation_pool.submit(citations.validate_batch, new, source_texts, reranker)))
        validated_prefix = complete

    with ThreadPoolExecutor(max_workers=1) as pool:
        followup_future = pool.submit(_suggested_followups, client, search_query, unused or sources)
        text = _generate_text(
            client,
            build_user_prompt(question, prompt_sources),
            trace,
            on_stage,
            on_token,
            on_partial=_submit_completed_sentences if on_token is not None else None,
        )
        llm_suggestions = followup_future.result()

    if not text or text.strip().upper().startswith(NO_ANSWER):
        reason = "The answer isn't supported by the selected documents."
        validation_pool.shutdown(wait=False, cancel_futures=True)
        return _finish(cache_key, Answer("abstained", [], sources, trace, hits, abstain_reason=reason))

    sentences = _dedup_sentences(_parse_sentences(text))

    with trace.stage("Validating citations", on_stage) as payload:
        tail = [s for s in sentences[len(validated_prefix):] if s.cites]
        if tail:
            validation_futures.append((tail, validation_pool.submit(citations.validate_batch, tail, source_texts, reranker)))
        validation_pool.shutdown(wait=True)

        validated: dict[tuple[str, tuple[int, ...]], citations.CitationCheck] = {}
        for batch, future in validation_futures:
            for sent, batch_check in zip(batch, future.result(), strict=True):
                validated[(sent.text, tuple(sent.cites))] = batch_check
        for sent in sentences:
            check = validated.get((sent.text, tuple(sent.cites))) if sent.cites else None
            if check is not None:
                sent.citation_status = check.status

        unsupported = [s.text for s in sentences if s.citation_status == "unsupported"]
        payload["unsupported"] = len(unsupported)
        payload["validations"] = [
            {"text": s.text, "cites": s.cites, "status": s.citation_status}
            for s in sentences
        ]

    source_docs = {s.id: s.filename for s in sources}
    conflicts = citations.detect_conflicts([(s.text, s.cites) for s in sentences], source_docs)
    cited = [s for s in sentences if s.cites]
    supported = [1.0 if s.citation_status == "supported" else 0.0 for s in cited]
    supported_fraction = sum(supported) / len(supported) if supported else 0.0
    agreement = 0.0 if conflicts else 1.0
    conf = confidence.compute(
        calibrated_top, calibrated_margin, supported_fraction, agreement, bool(conflicts), gate.confidence_modifier
    )

    structural = _structural_suggestion(sources, conflicts)
    suggestions = ([structural] if structural else []) + llm_suggestions
    suggestions = suggestions[: SETTINGS.retrieval.max_suggestions]

    return _finish(
        cache_key,
        Answer(
            "answered",
            sentences,
            sources,
            trace,
            hits,
            answer_text=text,
            conflicts=[Conflict(c.claim, c.source_ids) for c in conflicts],
            confidence=conf,
            suggestions=suggestions,
        ),
    )


def _generate_text(
    client: Provider,
    prompt: str,
    trace: Trace,
    on_stage: Optional[OnStage],
    on_token: Optional[OnToken],
    on_partial: Optional[Callable[[str], None]] = None,
) -> str:
    with trace.stage("Generating", on_stage) as payload:
        payload["prompt"] = prompt
        if on_token is None:
            completion = client.generate(SYSTEM, prompt)
            text = completion.text
            payload["prompt_tokens"] = completion.prompt_tokens
            payload["completion_tokens"] = completion.completion_tokens
        else:
            parts: list[str] = []
            for chunk in client.stream(SYSTEM, prompt):
                parts.append(chunk)
                accumulated = "".join(parts)
                on_token(accumulated)
                if on_partial is not None:
                    on_partial(accumulated)
            text = "".join(parts)
            payload["prompt_tokens"], payload["completion_tokens"] = client.last_usage
    return text.strip()


def _parse_sentences(text: str) -> list[Sentence]:
    sentences: list[Sentence] = []
    for line in text.splitlines():
        stripped = re.sub(r"^\s*([#>*\-]+|\d+\.)\s*", "", line).strip()
        if not stripped:
            continue
        for piece in _SENTENCE_RE.split(stripped):
            piece = piece.strip()
            if not piece:
                continue
            cites = parse_citations(piece)
            clean = _CITE_STRIP_RE.sub("", piece).strip()
            sentences.append(Sentence(text=clean, cites=cites))
    return sentences


def _dedup_sentences(sentences: list[Sentence]) -> list[Sentence]:
    # Drop near-duplicate restatements so confidence is not inflated by repetition.
    kept: list[Sentence] = []
    kept_tokens: list[set[str]] = []
    for sent in sentences:
        tokens = set(re.findall(r"[a-z0-9]+", sent.text.lower()))
        if not tokens:
            kept.append(sent)
            continue
        if any(
            len(tokens & prev) / len(tokens | prev) >= SETTINGS.retrieval.dedup_jaccard
            for prev in kept_tokens
        ):
            continue
        kept.append(sent)
        kept_tokens.append(tokens)
    return kept


def _hit_summaries(hits: list[Hit]) -> list[dict]:
    return [
        {
            "chunk_id": h.chunk_id,
            "doc_id": h.doc_id,
            "filename": h.filename,
            "page": h.page_start if h.page_start == h.page_end else f"{h.page_start}-{h.page_end}",
            "score": h.score,
        }
        for h in hits
    ]


def _agree_in_top_ranks(top: FusedHit) -> bool:
    return top.dense_rank is not None and top.lexical_rank is not None and top.dense_rank <= 2 and top.lexical_rank <= 2


def _assemble_sources(
    hits: list[Hit], store: VectorStore, tenant_id: str
) -> tuple[list[Source], list[UnusedPassage], list[str]]:
    # Parents are loaded for the documents that actually matched, not for everything the
    # caller selected: the hits have already been through the tenant filter.
    parents = _parent_index([hit.doc_id for hit in hits])
    best_hit: dict[str, Hit] = {}
    order: list[str] = []
    for hit in hits:
        if hit.parent_id not in best_hit:
            order.append(hit.parent_id)
        if hit.parent_id not in best_hit or hit.score > best_hit[hit.parent_id].score:
            best_hit[hit.parent_id] = hit

    order.sort(key=lambda pid: best_hit[pid].score, reverse=True)
    order = _mmr_order(order, best_hit, store, tenant_id)
    order = _diversify(order, best_hit, parents)

    sources: list[Source] = []
    unused: list[UnusedPassage] = []
    context_chunk_ids: list[str] = []
    used_tokens = 0
    budget = SETTINGS.retrieval.context_token_budget
    pad = SETTINGS.retrieval.span_padding_tokens
    context_k = SETTINGS.retrieval.context_k
    unused_k = SETTINGS.retrieval.unused_passages_k
    for parent_id in order:
        parent = parents.get(parent_id)
        if parent is None:
            continue
        if best_hit[parent_id].score < SETTINGS.trust.min_source_score:
            continue
        remaining = budget - used_tokens
        if len(sources) < context_k and remaining > 0:
            text, span = _center_on_span(parent.text, best_hit[parent_id].char_span_in_parent, pad)
            tokens = count_tokens(text)
            if tokens > remaining:
                text, span = _truncate_around_span(text, span, remaining)
                tokens = count_tokens(text)
            used_tokens += tokens
            sources.append(_source(len(sources) + 1, parent, span, text, best_hit[parent_id].score))
            context_chunk_ids.append(best_hit[parent_id].chunk_id)
        elif len(unused) < unused_k:
            unused.append(_unused_passage(parent))
        else:
            break
    return sources, unused, context_chunk_ids


def _mmr_order(
    order: list[str], best_hit: dict[str, Hit], store: VectorStore, tenant_id: str
) -> list[str]:
    if len(order) <= 2:
        return order
    # Child chunks were already embedded at ingest time; reuse those vectors instead of
    # re-encoding the matched spans on every query (B6).
    chunk_ids = [best_hit[pid].chunk_id for pid in order]
    vectors = store.get_embeddings(chunk_ids, tenant_id)
    emb = {pid: vectors[best_hit[pid].chunk_id] for pid in order}

    scores = [best_hit[pid].score for pid in order]
    lo, hi = min(scores), max(scores)
    rel = {pid: (best_hit[pid].score - lo) / (hi - lo) if hi > lo else 1.0 for pid in order}

    lam = SETTINGS.retrieval.mmr_lambda
    selected: list[str] = []
    remaining = list(order)
    while remaining:
        best_pid = max(
            remaining,
            key=lambda pid: rel[pid]
            if not selected
            else lam * rel[pid] - (1 - lam) * max(_cosine(emb[pid], emb[s]) for s in selected),
        )
        selected.append(best_pid)
        remaining.remove(best_pid)
    return selected


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _center_on_span(text: str, span: tuple[int, int], pad_tokens: int) -> tuple[str, tuple[int, int]]:
    if count_tokens(text) <= 2 * pad_tokens + count_tokens(text[span[0]:span[1]]):
        return text, span
    ratio = len(text) / max(1, count_tokens(text))
    budget_chars = (span[1] - span[0]) + int(2 * pad_tokens * ratio)
    return _window(text, span, budget_chars)


def _diversify(order: list[str], best_hit: dict[str, Hit], parents: dict[str, ParentChunk]) -> list[str]:
    cap = SETTINGS.retrieval.max_per_doc
    primary: list[str] = []
    overflow: list[str] = []
    per_doc: dict[str, int] = {}
    for parent_id in order:
        parent = parents.get(parent_id)
        doc = parent.filename if parent else parent_id
        if per_doc.get(doc, 0) < cap:
            primary.append(parent_id)
            per_doc[doc] = per_doc.get(doc, 0) + 1
        else:
            overflow.append(parent_id)
    return primary + overflow


def _parent_index(doc_ids: list[str]) -> dict[str, ParentChunk]:
    index: dict[str, ParentChunk] = {}
    for doc_id in dict.fromkeys(doc_ids):
        for parent in load_parents(doc_id):
            index[parent.parent_id] = parent
    return index


def _source(number: int, parent: ParentChunk, span: tuple[int, int], text: str, score: float) -> Source:
    pages = str(parent.page_start) if parent.page_start == parent.page_end else f"{parent.page_start}-{parent.page_end}"
    return Source(
        id=number,
        filename=parent.filename,
        pages=pages,
        section_path=" > ".join(parent.section_path),
        text=text,
        char_span_in_parent=span,
        score=score,
    )


def _unused_passage(parent: ParentChunk) -> UnusedPassage:
    pages = str(parent.page_start) if parent.page_start == parent.page_end else f"{parent.page_start}-{parent.page_end}"
    return UnusedPassage(filename=parent.filename, pages=pages, section_path=" > ".join(parent.section_path))


_FOLLOWUP_LINE_RE = re.compile(r"^\s*(\d+)\s*[:.]\s*(.+)$")


def _suggested_followups(
    client: Provider, question: str, unused: list[UnusedPassage] | list[Source]
) -> list[Suggestion]:
    if not unused:
        return []
    headers = [f"{u.filename} p.{u.pages}" + (f" - {u.section_path}" if u.section_path else "") for u in unused]
    try:
        completion = client.generate(FOLLOWUP_SYSTEM, build_followup_prompt(question, headers))
    except (ModelUnavailable, GenerationError):
        return []
    asked = re.sub(r"[^\w\s]", "", question).strip().lower()
    suggestions: list[Suggestion] = []
    for line in completion.text.splitlines():
        match = _FOLLOWUP_LINE_RE.match(line)
        if not match:
            continue
        idx = int(match.group(1)) - 1
        text = match.group(2).strip()
        if not text or not (0 <= idx < len(unused)):
            continue
        if text.startswith(headers[idx]):
            text = text[len(headers[idx]):].strip(" -:–—")
        if not text:
            continue
        if re.sub(r"[^\w\s]", "", text).strip().lower() == asked:
            continue
        source = unused[idx]
        suggestions.append(Suggestion(text, source.filename, source.pages))
    return suggestions


def _didyoumean(client: Provider, question: str, near_miss: Optional[Hit]) -> list[Suggestion]:
    if near_miss is None:
        return []
    try:
        completion = client.generate(DIDYOUMEAN_SYSTEM, build_didyoumean_prompt(question, near_miss.text))
    except (ModelUnavailable, GenerationError):
        return []
    text = completion.text.strip()
    if not text:
        return []
    pages = (
        str(near_miss.page_start)
        if near_miss.page_start == near_miss.page_end
        else f"{near_miss.page_start}-{near_miss.page_end}"
    )
    return [Suggestion(text, near_miss.filename, pages, label=f"Did you mean: {text}")]


_TABLE_RE = re.compile(r"^\s*\|.+\|\s*$", re.MULTILINE)


def _structural_suggestion(sources: list[Source], conflicts: list[citations.ValidatedConflict]) -> Optional[Suggestion]:
    if conflicts:
        return Suggestion("Show both values and their sources")
    if any(_TABLE_RE.search(s.text) for s in sources):
        return Suggestion("Break down the table by row")
    filenames = list(dict.fromkeys(s.filename for s in sources))
    if len(filenames) > 1:
        return Suggestion(f"Compare {filenames[0]} vs {filenames[1]}")
    return None


def _truncate_around_span(text: str, span: tuple[int, int], budget_tokens: int) -> tuple[str, tuple[int, int]]:
    # Derive the char budget from this text's own density so token accounting stays honest,
    # then shrink until the window actually fits the token budget.
    ratio = len(text) / max(1, count_tokens(text))
    budget_chars = int(budget_tokens * ratio)
    while True:
        clipped, new_span = _window(text, span, budget_chars)
        if budget_chars <= 1 or count_tokens(clipped) <= budget_tokens:
            return clipped, new_span
        budget_chars = int(budget_chars * 0.85)


def _window(text: str, span: tuple[int, int], budget_chars: int) -> tuple[str, tuple[int, int]]:
    start, end = span
    if end - start >= budget_chars:
        window_start, window_end = start, start + budget_chars
        return text[window_start:window_end], (0, min(end, window_end) - window_start)
    slack = budget_chars - (end - start)
    window_start = max(0, start - slack // 2)
    window_end = min(len(text), window_start + budget_chars)
    window_start = max(0, window_end - budget_chars)
    return text[window_start:window_end], (start - window_start, end - window_start)
