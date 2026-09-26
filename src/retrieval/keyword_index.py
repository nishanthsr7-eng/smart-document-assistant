import re

from sqlalchemy import func, select

from src.storage.db import session
from src.storage.models import Chunk
from src.retrieval.vector_store import Hit, to_hit

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from",
    "in", "into", "is", "it", "of", "on", "or", "that", "the", "to", "with",
}
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[.,][a-z0-9]+)*")


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


class KeywordIndex:
    """Lexical retrieval over the shared Postgres tsvector index.

    Replaces the in-process BM25 corpus: the index is a GIN-backed generated column on `chunks`,
    so it is maintained by the same write that stores the chunk and is identical in every worker.
    Ranking is ts_rank_cd rather than BM25 — fusion is rank-based, so the scale does not matter.
    The tenant predicate is applied here, alongside the dense one, for the same reason.
    """

    def query(self, question: str, k: int, doc_ids: list[str], tenant_id: str) -> list[Hit]:
        terms = tokenize(question)
        if not doc_ids or not terms:
            return []
        tsquery = func.to_tsquery("english", " | ".join(terms))
        rank = func.ts_rank_cd(Chunk.tsv, tsquery)
        with session() as sess:
            stmt = (
                select(Chunk, rank.label("rank"))
                .where(
                    Chunk.tenant_id == tenant_id,
                    Chunk.doc_id.in_(doc_ids),
                    Chunk.tsv.op("@@")(tsquery),
                    rank > 0,
                )
                .order_by(rank.desc())
                .limit(k)
            )
            return [to_hit(chunk, float(score)) for chunk, score in sess.execute(stmt)]
