import pytest

from src.core.config import SETTINGS
from src.core.errors import QueryCancelled
from src.core.tokens import count_tokens
from src.generation import answerer
from src.ingestion.chunker import ParentChunk
from src.retrieval.vector_store import Hit

BUDGET = SETTINGS.retrieval.context_token_budget
TENANT = "00000000-0000-0000-0000-0000000000aa"


@pytest.fixture
def stub_parents(monkeypatch):
    """Context assembly is storage-agnostic; stub the parent loader instead of the blob store."""
    store: dict[str, list[ParentChunk]] = {}
    monkeypatch.setattr(answerer, "load_parents", lambda doc_id: store[doc_id])
    return store


def _write_parents(store, doc_id: str, parents: list[tuple[str, str]], kind: str = "prose") -> None:
    store[doc_id] = [
        ParentChunk(
            parent_id=parent_id,
            doc_id=doc_id,
            filename="big.pdf",
            text=text,
            page_start=1,
            page_end=1,
            section_path=(),
            kind=kind,
        )
        for parent_id, text in parents
    ]


class _FakeEmbedder:
    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t) % 5), 1.0] for t in texts]

    def get_embeddings(self, chunk_ids: list[str], tenant_id: str) -> dict[str, list[float]]:
        return {cid: [0.1, 0.2] for cid in chunk_ids}


def _hit(parent_id: str, doc_id: str, score: float, span: tuple[int, int] = (0, 10), kind: str = "prose") -> Hit:
    return Hit(
        chunk_id=f"{parent_id}-c",
        parent_id=parent_id,
        doc_id=doc_id,
        filename="big.pdf",
        text="chunk text",
        page_start=1,
        page_end=1,
        section_path=(),
        kind=kind,
        char_span_in_parent=span,
        score=score,
    )


_FILLER = "lorem ipsum dolor sit amet consectetur "


def test_huge_single_source_is_truncated_to_budget(stub_parents):
    doc_id = "doc1"
    huge_text = _FILLER * (BUDGET * 2)
    _write_parents(stub_parents, doc_id, [("p1", huge_text)])
    sources, _, _ = answerer._assemble_sources([_hit("p1", doc_id, 0.9)], _FakeEmbedder(), TENANT)
    assert count_tokens(sources[0].text) <= BUDGET


def test_truncated_source_keeps_matched_span_in_bounds(stub_parents):
    doc_id = "doc3"
    marker = "MATCHED-PASSAGE"
    prefix = _FILLER * BUDGET
    huge_text = prefix + marker + _FILLER * BUDGET
    span = (len(prefix), len(prefix) + len(marker))
    _write_parents(stub_parents, doc_id, [("p1", huge_text)])
    sources, _, _ = answerer._assemble_sources(
        [_hit("p1", doc_id, 0.9, span=span)], _FakeEmbedder(), TENANT
    )
    source = sources[0]
    start, end = source.char_span_in_parent
    assert 0 <= start <= end <= len(source.text)
    assert source.text[start:end] == marker


def test_dedup_drops_near_duplicate_sentences():
    sents = [
        answerer.Sentence(text="Employees accrue twelve days of leave per year", cites=[1]),
        answerer.Sentence(text="Per year employees accrue twelve days of leave", cites=[1]),
        answerer.Sentence(text="Sick leave is tracked separately", cites=[2]),
    ]
    kept = answerer._dedup_sentences(sents)
    assert len(kept) == 2
    assert kept[0].text.endswith("per year")
    assert kept[1].text == "Sick leave is tracked separately"


def test_multiple_sources_never_exceed_budget(stub_parents):
    doc_id = "doc2"
    chunk = _FILLER * BUDGET
    parents = [(f"p{i}", chunk) for i in range(5)]
    _write_parents(stub_parents, doc_id, parents)
    hits = [_hit(pid, doc_id, 1.0 - i * 0.01) for i, (pid, _) in enumerate(parents)]
    sources, _, _ = answerer._assemble_sources(hits, _FakeEmbedder(), TENANT)
    total_tokens = sum(count_tokens(s.text) for s in sources)
    assert total_tokens <= BUDGET


def _markdown_table(rows: int) -> str:
    lines = ["| Grade | Hours per pay period |", "|---|---|"]
    lines += [f"| GS-{i:02d} | {i} hours of annual leave |" for i in range(1, rows + 1)]
    return "\n".join(lines) + "\n"


def test_table_window_keeps_the_header_and_whole_rows():
    text = _markdown_table(200)
    row = "| GS-90 | 90 hours of annual leave |"
    start = text.index(row)
    windowed, span = answerer._table_window(text, (start, start + len(row)), budget_tokens=60)

    assert count_tokens(windowed) <= 60
    assert windowed.startswith("| Grade | Hours per pay period |")
    assert "|---|---|" in windowed
    assert windowed[span[0]:span[1]].strip() == row
    body = [line for line in windowed.splitlines() if line.strip()]
    assert all(line.startswith("|") and line.endswith("|") for line in body)


def test_table_window_returns_a_small_table_untouched():
    text = _markdown_table(3)
    assert answerer._table_window(text, (0, 5), budget_tokens=500) == (text, (0, 5))


def test_table_source_is_row_aligned_end_to_end(stub_parents):
    doc_id = "doc4"
    text = _markdown_table(200)
    row = "| GS-90 | 90 hours of annual leave |"
    start = text.index(row)
    _write_parents(stub_parents, doc_id, [("p1", text)], kind="table")
    sources, _, _ = answerer._assemble_sources(
        [_hit("p1", doc_id, 0.9, span=(start, start + len(row)), kind="table")], _FakeEmbedder(), TENANT
    )
    assert sources[0].text.startswith("| Grade | Hours per pay period |")
    assert row in sources[0].text


# --- answer cache key ---


def _key(question: str, history=None, mode: str = "hybrid", generate: bool = True) -> str:
    return answerer._cache_key(TENANT, question, ["d1"], mode, generate, history)


def test_cache_key_separates_two_conversations():
    """The same words mean a different question in a different conversation, so they must not
    share a cached answer."""
    first = [("user", "Tell me about the 2024 bonus pool."), ("assistant", "It was 4.2M.")]
    second = [("user", "Tell me about the 2025 bonus pool."), ("assistant", "It was 5.1M.")]
    assert _key("What is the revenue?", first) != _key("What is the revenue?", second)


def test_cache_key_is_stable_for_the_same_conversation():
    history = [("user", "What is the bonus pool?"), ("assistant", "4.2M.")]
    assert _key("And the revenue?", history) == _key("And the revenue?", list(history))


def test_cache_key_ignores_turns_outside_the_condense_window():
    """Only the last history_turns turns reach the condenser, so only those may change the key --
    otherwise every follow-up in a long conversation misses."""
    window = SETTINGS.generation.history_turns
    recent = [(f"user{i}", f"turn {i}") for i in range(window)]
    assert _key("And then?", [("user", "ancient")] + recent) == _key("And then?", recent)


def test_cache_key_without_history_is_not_the_empty_history_key():
    history = [("user", "What is the bonus pool?")]
    assert _key("What is the revenue?", None) == _key("What is the revenue?", [])
    assert _key("What is the revenue?", None) != _key("What is the revenue?", history)


# --- cancellation ---


def test_halt_raises_once_the_flag_is_set():
    cancelled = False
    answerer._halt(lambda: cancelled, "searching")
    cancelled = True
    with pytest.raises(QueryCancelled) as exc:
        answerer._halt(lambda: cancelled, "generating")
    assert exc.value.reason == "generating"


def test_halt_without_a_cancel_callback_is_a_no_op():
    answerer._halt(None, "searching")
