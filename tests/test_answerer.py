import json
from types import SimpleNamespace

import pytest

from src.core.config import SETTINGS
from src.core.tokens import count_tokens
from src.generation import answerer
from src.ingestion import pipeline
from src.retrieval.vector_store import Hit

BUDGET = SETTINGS.retrieval.context_token_budget


@pytest.fixture
def isolated_parents(tmp_path, monkeypatch):
    store = tmp_path / "parents"
    store.mkdir()
    monkeypatch.setattr(pipeline, "SETTINGS", SimpleNamespace(paths=SimpleNamespace(parents=store)))
    return store


def _write_parents(store, doc_id: str, parents: list[tuple[str, str]]) -> None:
    payload = {
        "parents": [
            {
                "parent_id": parent_id,
                "doc_id": doc_id,
                "filename": "big.pdf",
                "text": text,
                "page_start": 1,
                "page_end": 1,
                "section_path": [],
                "kind": "table",
            }
            for parent_id, text in parents
        ],
        "children": [],
    }
    (store / f"{doc_id}.json").write_text(json.dumps(payload), encoding="utf-8")


class _FakeEmbedder:
    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(t) % 5), 1.0] for t in texts]

    def get_embeddings(self, chunk_ids: list[str]) -> dict[str, list[float]]:
        return {cid: [0.1, 0.2] for cid in chunk_ids}


def _hit(parent_id: str, doc_id: str, score: float, span: tuple[int, int] = (0, 10)) -> Hit:
    return Hit(
        chunk_id=f"{parent_id}-c",
        parent_id=parent_id,
        doc_id=doc_id,
        filename="big.pdf",
        text="chunk text",
        page_start=1,
        page_end=1,
        section_path=(),
        kind="table",
        char_span_in_parent=span,
        score=score,
    )


_FILLER = "lorem ipsum dolor sit amet consectetur "


def test_huge_single_source_is_truncated_to_budget(isolated_parents):
    doc_id = "doc1"
    huge_text = _FILLER * (BUDGET * 2)
    _write_parents(isolated_parents, doc_id, [("p1", huge_text)])
    sources, _, _ = answerer._assemble_sources([_hit("p1", doc_id, 0.9)], [doc_id], _FakeEmbedder())
    assert count_tokens(sources[0].text) <= BUDGET


def test_truncated_source_keeps_matched_span_in_bounds(isolated_parents):
    doc_id = "doc3"
    marker = "MATCHED-PASSAGE"
    prefix = _FILLER * BUDGET
    huge_text = prefix + marker + _FILLER * BUDGET
    span = (len(prefix), len(prefix) + len(marker))
    _write_parents(isolated_parents, doc_id, [("p1", huge_text)])
    sources, _, _ = answerer._assemble_sources([_hit("p1", doc_id, 0.9, span=span)], [doc_id], _FakeEmbedder())
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


def test_multiple_sources_never_exceed_budget(isolated_parents):
    doc_id = "doc2"
    chunk = _FILLER * BUDGET
    parents = [(f"p{i}", chunk) for i in range(5)]
    _write_parents(isolated_parents, doc_id, parents)
    hits = [_hit(pid, doc_id, 1.0 - i * 0.01) for i, (pid, _) in enumerate(parents)]
    sources, _, _ = answerer._assemble_sources(hits, [doc_id], _FakeEmbedder())
    total_tokens = sum(count_tokens(s.text) for s in sources)
    assert total_tokens <= BUDGET
