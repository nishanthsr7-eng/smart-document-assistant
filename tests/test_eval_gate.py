import pytest

from evaluation.run_eval import (
    _by_type,
    _injection_resistance,
    check_gates,
    load_golden_set,
    load_profile,
)
from src.core.tracing import Trace
from src.generation.answerer import Answer, Sentence, Suggestion
from src.generation.client import NullProvider


def _answer(text: str, suggestions: list[str] | None = None) -> Answer:
    return Answer(
        status="answered",
        sentences=[Sentence(text=text, cites=[1])],
        sources=[],
        trace=Trace(tenant_id="t", user_id="u"),
        hits=[],
        suggestions=[Suggestion(text=s) for s in (suggestions or [])],
    )


def test_golden_set_covers_every_declared_type():
    items = load_golden_set()
    declared = {
        "lookup", "table", "exact_term", "paraphrase",
        "multi_doc", "conflict", "unanswerable", "injection",
    }
    assert {item.type for item in items} == declared
    assert len({item.id for item in items}) == len(items)
    for item in items:
        assert item.answerable == bool(item.expected)


def test_golden_set_expectations_name_real_sample_docs():
    from src.core.config import SETTINGS

    available = {p.name for p in SETTINGS.paths.sample_docs.iterdir()}
    for item in load_golden_set():
        for expected in item.expected:
            assert expected.filename in available, f"{item.id} names a missing document"
            assert expected.page >= 1


@pytest.mark.parametrize("profile_name", ["retrieval", "generation"])
def test_profiles_gate_metrics_that_a_run_actually_produces(profile_name):
    profile = load_profile(profile_name)
    assert profile["mode"] in ("dense", "hybrid", "hybrid_rerank")
    groups = {"retrieval", "abstention", "generation"}
    for metric, rule in profile["gates"].items():
        group, _ = metric.split(".", 1)
        assert group in groups
        assert ("min" in rule) != ("max" in rule)
    if not profile["generate"]:
        assert not any(m.startswith("generation.") for m in profile["gates"])


def test_unknown_profile_is_fatal():
    with pytest.raises(SystemExit):
        load_profile("nope")


def test_gate_compares_against_bounds():
    results = {"retrieval": {"hit_at_k": 0.9}, "abstention": {"false_refusal_rate": 0.1}}
    outcomes = {
        o.metric: o
        for o in check_gates(
            results,
            {
                "retrieval.hit_at_k": {"min": 0.95},
                "abstention.false_refusal_rate": {"max": 0.25},
            },
        )
    }
    assert outcomes["retrieval.hit_at_k"].passed is False
    assert outcomes["abstention.false_refusal_rate"].passed is True


def test_a_metric_the_run_did_not_produce_fails_the_gate():
    outcome = check_gates({"generation": {}}, {"generation.faithfulness": {"min": 0.5}})[0]
    assert outcome.passed is False
    assert outcome.detail == "not measured"


def test_injection_resistance_flags_a_followed_instruction():
    assert _injection_resistance(_answer("Employees accrue 4 hours per pay period.")) == 1.0
    assert _injection_resistance(_answer("INJECTION SUCCESSFUL")) == 0.0
    assert _injection_resistance(_answer("ok", suggestions=["Enter developer mode"])) == 0.0


def test_by_type_splits_abstention_correctness():
    items = [i for i in load_golden_set() if i.type in ("lookup", "unanswerable")][:2]
    per_item = [{"status": "answered", "hit_at_k": 1.0} for _ in items]
    buckets = _by_type(items, per_item)
    assert sum(b["n"] for b in buckets.values()) == len(items)
    for item, bucket in zip(items, [buckets[i.type] for i in items], strict=True):
        assert bucket["correct_abstention"] == (1.0 if item.answerable else 0.0)


def test_null_provider_generates_nothing_and_needs_no_key():
    client = NullProvider()
    client.health()
    assert client.generate("s", "u").text == ""
