"""Property-based tests for the text functions every uploaded document flows through.

These take arbitrary text rather than the tidy fixtures the example-based tests use, which is the
point: a chunker is fed whatever a stranger's PDF parses to, and the invariants below are the ones
that must hold for all of it. Each property is a claim about the function, not about a case.
"""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.core import redaction
from src.core.config import SETTINGS
from src.generation import prompts
from src.ingestion.chunker import _atomic_windows, _sentence_windows, chunk_document
from src.ingestion.parsers import Element
from src.retrieval import query

# Whatever a parser can produce: control characters, scripts we do not test by hand, no text.
TEXT = st.text(max_size=2000)
PROSE = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=500
)

SLOW = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])


# --- chunking ---


@given(TEXT)
@SLOW
def test_atomic_windows_cover_the_text_exactly_once(text):
    """Every character lands in exactly one window, in order. A gap silently drops content from
    the index; an overlap double-counts it in retrieval."""
    windows = _atomic_windows(text)
    assert "".join(w for w, _ in windows) == text
    spans = [span for _, span in windows]
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:], strict=False))


@given(TEXT)
@SLOW
def test_window_spans_address_their_own_text(text):
    """The span is what the UI highlights in the source document. If it does not slice back to
    the window's text, the citation points at the wrong place."""
    for chunk, (start, end) in _atomic_windows(text):
        assert text[start:end] == chunk


@given(TEXT)
@SLOW
def test_sentence_windows_stay_inside_the_text(text):
    for chunk, (start, end) in _sentence_windows(text):
        assert 0 <= start <= end <= len(text)
        assert text[start:end] == chunk


@given(st.lists(PROSE, min_size=1, max_size=12))
@SLOW
def test_every_child_belongs_to_a_parent_it_is_contained_in(paragraphs):
    elements = [
        Element(kind="paragraph", text=p, page=1, section_path=()) for p in paragraphs
    ]
    parents, children = chunk_document(elements, "doc", "notes.pdf")
    by_id = {p.parent_id: p for p in parents}
    for child in children:
        parent = by_id[child.parent_id]
        start, end = child.char_span_in_parent
        assert parent.text[start:end] == child.text


# --- question sanitization ---


@given(PROSE)
@SLOW
def test_sanitize_never_invents_text(question):
    """It only ever drops clauses. Anything else would change the question the user asked."""
    sanitized = query.sanitize(question)
    assert sanitized == question.strip() or len(sanitized) < len(question.strip())


@given(PROSE)
@SLOW
def test_sanitize_is_idempotent(question):
    once = query.sanitize(question)
    assert query.sanitize(once) == once


@given(PROSE)
@SLOW
def test_subqueries_always_lead_with_the_whole_question(question):
    parts = query.subqueries(question)
    assert parts[0] == question.strip() or not question.strip()
    assert len(parts) <= SETTINGS.retrieval.max_subqueries


# --- prompt assembly ---


@given(PROSE, PROSE)
@SLOW
def test_a_source_can_never_close_its_own_block(question, source):
    """The nonce-tagged wrapper is what keeps document text from being read as prompt structure.
    No source text may produce a closing tag, whatever it contains."""
    prompt = prompts.build_user_prompt(question, [(1, "d.pdf", source)])
    nonce = prompt.split("<source-", 1)[1].split(" ", 1)[0]
    assert prompt.count(f"</source-{nonce}>") == 1


@given(PROSE)
@SLOW
def test_spotlighting_does_not_change_the_length_of_a_passage(source):
    assert len(prompts.spotlight(source)) == len(source)


# --- redaction ---


@given(TEXT)
@SLOW
def test_redaction_never_leaves_an_email_it_found_behind(text):
    email = dict(redaction._PATTERNS)["EMAIL"]
    assert not email.search(redaction.scrub(text))


@given(TEXT)
@SLOW
def test_redaction_is_idempotent(text):
    once = redaction.scrub(text)
    assert redaction.scrub(once) == once
