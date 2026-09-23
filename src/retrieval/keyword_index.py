import dataclasses
import re
import threading
from typing import Optional

from rank_bm25 import BM25Okapi

from src.ingestion.chunker import ChildChunk, build_header
from src.retrieval.vector_store import Hit, VectorStore

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "into", "is", "it", "of", "on", "or", "that", "the", "to", "with",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.,][a-z0-9]+)*")


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


class KeywordIndex:
    def __init__(self, store: VectorStore) -> None:
        self._lock = threading.Lock()
        self._hits = store.all_chunks()
        self._tokens = [_tokenize_hit(h) for h in self._hits]
        self._rebuild()

    def _rebuild(self) -> None:
        self._bm25: Optional[BM25Okapi] = BM25Okapi(self._tokens) if self._tokens else None

    def add_doc(self, children: list[ChildChunk]) -> None:
        if not children:
            return
        with self._lock:
            present = {hit.chunk_id for hit in self._hits}
            # Only tokenize the new chunks (B6) — re-tokenizing the whole corpus here scaled with
            # total corpus size on every upload. BM25Okapi's own IDF pass still rescans all tokens;
            # true incremental BM25 (or SQLite FTS5) is deferred.
            new_hits = [_child_to_hit(c) for c in children if c.chunk_id not in present]
            if not new_hits:
                return
            self._hits.extend(new_hits)
            self._tokens.extend(_tokenize_hit(h) for h in new_hits)
            self._rebuild()

    def remove_doc(self, doc_id: str) -> None:
        with self._lock:
            kept = [(h, t) for h, t in zip(self._hits, self._tokens) if h.doc_id != doc_id]
            if len(kept) == len(self._hits):
                return
            self._hits = [h for h, _ in kept]
            self._tokens = [t for _, t in kept]
            self._rebuild()

    def query(self, question: str, k: int, doc_ids: list[str]) -> list[Hit]:
        if not doc_ids:
            return []
        with self._lock:
            bm25 = self._bm25
            hits = list(self._hits)
        if bm25 is None:
            return []
        scores = bm25.get_scores(tokenize(question))
        ranked = sorted(
            (
                (score, hit)
                for score, hit in zip(scores, hits)
                if score > 0 and hit.doc_id in doc_ids
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        return [dataclasses.replace(hit, score=float(score)) for score, hit in ranked[:k]]


def _tokenize_hit(hit: Hit) -> list[str]:
    return tokenize(f"{build_header(hit.filename, hit.section_path)}\n{hit.text}")


def _child_to_hit(c: ChildChunk) -> Hit:
    return Hit(
        chunk_id=c.chunk_id,
        parent_id=c.parent_id,
        doc_id=c.doc_id,
        filename=c.filename,
        text=c.text,
        page_start=c.page_start,
        page_end=c.page_end,
        section_path=c.section_path,
        kind=c.kind,
        char_span_in_parent=c.char_span_in_parent,
        score=0.0,
    )
