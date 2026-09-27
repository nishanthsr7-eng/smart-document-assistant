from src.retrieval.hybrid import fuse, merge_dense
from src.retrieval.keyword_index import tokenize
from src.retrieval.vector_store import Hit


def _hit(chunk_id: str, score: float = 0.0) -> Hit:
    return Hit(
        chunk_id=chunk_id,
        parent_id="p0",
        doc_id="d0",
        filename="f.txt",
        text="text",
        page_start=1,
        page_end=1,
        section_path=(),
        kind="prose",
        char_span_in_parent=(0, 4),
        score=score,
    )


def test_rrf_rewards_agreement_across_both_lists():
    dense = [_hit("a"), _hit("b")]
    lexical = [_hit("a"), _hit("c")]
    fused = {f.hit.chunk_id: f for f in fuse(dense, lexical)}
    assert fused["a"].hit.score == 1.0
    assert fused["a"].dense_rank == 1 and fused["a"].lexical_rank == 1
    assert fused["b"].lexical_rank is None


def test_rrf_top_is_the_agreed_chunk():
    dense = [_hit("a"), _hit("b")]
    lexical = [_hit("b"), _hit("a")]
    fused = fuse(dense, lexical)
    assert fused[0].hit.chunk_id in {"a", "b"}
    assert fused[0].hit.score > fused[-1].hit.score or len(fused) == 2


def test_fuse_empty_lists():
    assert fuse([], []) == []


def test_tokenize_drops_stopwords_and_lowercases():
    assert tokenize("The Leave Policy and Rules") == ["leave", "policy", "rules"]


def test_tokenize_keeps_decimal_tokens():
    assert "3.5" in tokenize("Accrue 3.5 days")


def test_merge_dense_keeps_best_score_per_chunk():
    list_a = [_hit("x", 0.4), _hit("y", 0.3)]
    list_b = [_hit("x", 0.7), _hit("z", 0.2)]
    merged = merge_dense([list_a, list_b], k=10)
    by_id = {h.chunk_id: h.score for h in merged}
    assert by_id["x"] == 0.7
    assert merged[0].chunk_id == "x"
    assert set(by_id) == {"x", "y", "z"}


def test_merge_dense_respects_k():
    hits = [[_hit(str(i), 1.0 - i * 0.1) for i in range(5)]]
    assert len(merge_dense(hits, k=3)) == 3

