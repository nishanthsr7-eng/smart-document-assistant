import dataclasses
from dataclasses import dataclass
from typing import Optional

from src.core.config import SETTINGS
from src.retrieval.vector_store import Hit


@dataclass
class FusedHit:
    hit: Hit
    dense_rank: Optional[int]
    lexical_rank: Optional[int]


def merge_dense(hit_lists: list[list[Hit]], k: int) -> list[Hit]:
    # Union of per-variant dense results, each chunk kept at its best cosine score across
    # variants. Keeps `.score` a cosine value so abstention/confidence thresholds stay valid.
    best: dict[str, Hit] = {}
    for hits in hit_lists:
        for hit in hits:
            if hit.chunk_id not in best or hit.score > best[hit.chunk_id].score:
                best[hit.chunk_id] = hit
    ranked = sorted(best.values(), key=lambda h: h.score, reverse=True)
    return ranked[:k]


def fuse(dense_hits: list[Hit], lexical_hits: list[Hit]) -> list[FusedHit]:
    dense_ranks = {hit.chunk_id: rank for rank, hit in enumerate(dense_hits, start=1)}
    lexical_ranks = {hit.chunk_id: rank for rank, hit in enumerate(lexical_hits, start=1)}

    by_id = {hit.chunk_id: hit for hit in dense_hits}
    for hit in lexical_hits:
        by_id.setdefault(hit.chunk_id, hit)

    # RRF's max possible score (both ranks == 1) is 2/(rrf_k+1); divide by it so fused
    # scores land in [0,1] like the cosine/sigmoid scales abstention/confidence expect.
    rrf_ceiling = 2.0 / (SETTINGS.retrieval.rrf_k + 1)

    fused = []
    for chunk_id, hit in by_id.items():
        dense_rank = dense_ranks.get(chunk_id)
        lexical_rank = lexical_ranks.get(chunk_id)
        rrf_score = 0.0
        if dense_rank is not None:
            rrf_score += 1.0 / (SETTINGS.retrieval.rrf_k + dense_rank)
        if lexical_rank is not None:
            rrf_score += 1.0 / (SETTINGS.retrieval.rrf_k + lexical_rank)
        fused.append(
            FusedHit(
                hit=dataclasses.replace(hit, score=rrf_score / rrf_ceiling),
                dense_rank=dense_rank,
                lexical_rank=lexical_rank,
            )
        )

    fused.sort(key=lambda f: f.hit.score, reverse=True)
    return fused
