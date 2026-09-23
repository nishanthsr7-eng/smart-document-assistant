from src.core.config import SETTINGS
from src.ingestion.chunker import _count_tokens, chunk_document
from src.ingestion.parsers import Element


def _prose(n: int) -> str:
    return " ".join(f"Sentence number {i} states a distinct fact." for i in range(n))


def test_children_respect_token_budget():
    elements = [Element("paragraph", _prose(60), 1, ("Sec A",))]
    _, children = chunk_document(elements, "doc1", "file.txt")
    limit = SETTINGS.ingestion.child_tokens
    multi = [c for c in children if len(c.text.split(". ")) > 1]
    assert multi, "expected multi-sentence windows"
    assert all(_count_tokens(c.text) <= limit + 40 for c in children)


def test_child_spans_match_parent_text():
    elements = [Element("paragraph", _prose(40), 2, ("Sec B",))]
    parents, children = chunk_document(elements, "doc2", "file.txt")
    by_id = {p.parent_id: p for p in parents}
    for c in children:
        s, e = c.char_span_in_parent
        assert by_id[c.parent_id].text[s:e] == c.text


def test_windows_overlap_by_one_sentence():
    elements = [Element("paragraph", _prose(30), 1, ("Sec",))]
    _, children = chunk_document(elements, "doc3", "file.txt")
    assert len(children) >= 2
    first_tail = children[0].text.rsplit(".", 2)[-2].strip()
    assert first_tail in children[1].text


def test_table_and_figure_are_atomic_parents():
    elements = [
        Element("paragraph", "Intro text.", 1, ("S",)),
        Element("table", "| a | b |\n| 1 | 2 |", 1, ("S",)),
        Element("figure", "[Figure, p.1: a chart]", 1, ("S",)),
    ]
    parents, _ = chunk_document(elements, "doc4", "file.txt")
    kinds = sorted(p.kind for p in parents)
    assert kinds == ["figure", "prose", "table"]


def test_section_change_starts_new_parent():
    elements = [
        Element("paragraph", "Alpha content.", 1, ("One",)),
        Element("paragraph", "Beta content.", 1, ("Two",)),
    ]
    parents, _ = chunk_document(elements, "doc5", "file.txt")
    assert len(parents) == 2


def test_embed_text_carries_contextual_header():
    elements = [Element("paragraph", "Body.", 1, ("Chapter", "Clause"))]
    _, children = chunk_document(elements, "doc6", "handbook.pdf")
    assert children[0].embed_text.startswith("handbook > Chapter > Clause\n")
