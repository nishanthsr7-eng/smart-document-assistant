from src.core.config import SETTINGS
from src.retrieval.vector_store import Hit
from src.trust import abstention, citations, confidence


def _hit(chunk_id: str, score: float) -> Hit:
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


def test_abstain_below_hard_threshold():
    result = abstention.check([_hit("c0", 0.1)], 0.1)
    assert result.should_abstain
    assert result.near_miss is not None


def test_soft_zone_answers_with_reduced_modifier():
    hard = SETTINGS.trust.abstain_threshold
    result = abstention.check([_hit("c0", hard + 0.01)], hard + 0.01)
    assert not result.should_abstain
    assert 0.0 < result.confidence_modifier < 1.0


def test_consensus_overrides_low_score():
    result = abstention.check([_hit("c0", 0.05)], 0.05, retrieval_consensus=True)
    assert not result.should_abstain
    assert result.confidence_modifier == 1.0


def test_no_hits_abstains():
    assert abstention.check([], None).should_abstain


def test_grounded_normalizes_thousands_and_decimals():
    assert citations._grounded("The cap is 4,000 units.", "cap of 4000 units") == 1.0
    assert citations._grounded("It is 8.0 hours.", "limit is 8 hours") == 1.0


def test_grounded_partial_fraction():
    score = citations._grounded("Values are 10 and 999.", "only 10 appears here")
    assert score == 0.5


def test_grounded_no_numbers_is_full():
    assert citations._grounded("A qualitative claim.", "unrelated") == 1.0


def test_same_claim_shape_detects_overlap():
    assert citations._same_claim_shape("Leave is 12 days per year", "Leave is 15 days per year")
    assert not citations._same_claim_shape("Leave is 12 days", "Parking costs 5 dollars")


def test_detect_conflicts_on_differing_numbers():
    sentences = [("Leave is 12 days per year", [1]), ("Leave is 15 days per year", [2])]
    conflicts = citations.detect_conflicts(sentences, {1: "a.pdf", 2: "b.pdf"})
    assert len(conflicts) == 1
    assert conflicts[0].source_ids == [1, 2]


def test_confidence_label_boundaries():
    high = confidence.compute(1.0, 1.0, 1.0, 1.0, False)
    assert high.label == "High" and high.score == 1.0
    low = confidence.compute(0.0, 0.0, 0.0, 0.0, False)
    assert low.label == "Low"


def test_confidence_modifier_scales_score():
    full = confidence.compute(0.8, 0.5, 1.0, 1.0, False, 1.0).score
    half = confidence.compute(0.8, 0.5, 1.0, 1.0, False, 0.5).score
    assert half < full


def test_confidence_stays_in_unit_range():
    result = confidence.compute(2.0, 2.0, 2.0, 2.0, False, 2.0)
    assert 0.0 <= result.score <= 1.0
