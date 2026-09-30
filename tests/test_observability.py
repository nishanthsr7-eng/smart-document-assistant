import json
import pickle

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from src.api import router
from src.core import llm_traces, logs, metrics, observability, otel
from src.core.config import SETTINGS, replace
from src.core.tracing import Trace


@pytest.fixture(scope="module")
def spans():
    observability.setup("test")
    exporter = InMemorySpanExporter()
    otel.provider().add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


@pytest.fixture
def recorded(spans):
    spans.clear()
    yield spans


def _counter(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _span(recorded, name: str):
    return next(s for s in recorded.get_finished_spans() if s.name == name)


def test_stage_label_is_bounded_by_the_first_word():
    assert metrics.stage_label("Searching 3 document(s)") == "searching"
    assert metrics.stage_label("Validating citations") == "validating"
    assert metrics.stage_label("  ") == "unknown"


def test_stage_spans_nest_under_the_answer_span_and_time_themselves(recorded):
    before = _counter("sda_stage_duration_seconds_count", stage="searching")
    trace = Trace(tenant_id="t-1", user_id="u-1")
    with trace.root("How much leave?", "hybrid", ["doc-a"], True), trace.stage(
        "Searching 1 document(s)"
    ) as payload:
        payload["top_score"] = 0.8
        payload["mode"] = "hybrid"

    stage, root = _span(recorded, "searching"), _span(recorded, "answer")
    assert stage.parent.span_id == root.context.span_id
    assert stage.attributes["sda.stage"] == "Searching 1 document(s)"
    assert stage.attributes["sda.top_score"] == 0.8
    assert root.attributes["sda.tenant_id"] == "t-1"
    assert root.attributes["sda.query_id"] == trace.query_id
    assert _counter("sda_stage_duration_seconds_count", stage="searching") == before + 1
    assert trace.stages[0].duration_s >= 0


def test_stage_with_no_hits_reports_no_top_score(recorded):
    trace = Trace()
    with trace.root("q", "dense", [], True), trace.stage("Reranking") as payload:
        payload["top_score"] = None
        payload["sources"] = 0

    attributes = _span(recorded, "reranking").attributes
    assert "sda.top_score" not in attributes
    assert attributes["sda.sources"] == 0


def test_generation_stage_carries_prompt_usage_and_cost_for_langfuse(recorded):
    trace = Trace()
    with trace.root("q", "dense", [], True), trace.stage("Generating") as payload:
        payload["prompt"] = "context ..."
        payload["completion"] = "answer [1]"
        payload["model"] = "gemini-3.6-flash"
        payload["prompt_tokens"] = 1000
        payload["completion_tokens"] = 100
        payload["cost_usd"] = 0.001

    attributes = _span(recorded, "generating").attributes
    assert attributes["langfuse.observation.type"] == "generation"
    assert attributes["langfuse.observation.input"] == "context ..."
    assert attributes["langfuse.observation.model.name"] == "gemini-3.6-flash"
    assert '"input": 1000' in attributes["langfuse.observation.usage_details"]


def test_stage_records_the_error_and_still_appends_the_record(recorded):
    trace = Trace()
    with pytest.raises(RuntimeError), trace.root("q", "dense", [], True), trace.stage("Generating"):
        raise RuntimeError("provider exploded")

    assert "provider exploded" in _span(recorded, "generating").status.description
    assert [s.name for s in trace.stages] == ["Generating"]


def test_pooled_work_stays_in_the_caller_trace(recorded):
    def child() -> None:
        with otel.tracer().start_as_current_span("pooled"):
            pass

    with otel.tracer().start_as_current_span("outer"), otel.TracedPool(max_workers=2) as pool:
        pool.submit(child).result()

    assert _span(recorded, "pooled").parent.span_id == _span(recorded, "outer").context.span_id


def test_finish_records_answer_metrics(recorded):
    before = _counter("sda_answers_total", status="answered", confidence="High")
    hits = _counter("sda_answer_cache_total", result="hit")
    supported = _counter("sda_citation_checks_total", status="supported")

    trace = Trace()
    with trace.root("q", "dense", [], True):
        trace.finish(
            status="answered",
            answer_text="20 days.",
            confidence_label="High",
            confidence_score=0.82,
            citation_statuses=["supported", "unsupported"],
            abstain_reason=None,
            cached=True,
        )

    assert _counter("sda_answers_total", status="answered", confidence="High") == before + 1
    assert _counter("sda_answer_cache_total", result="hit") == hits + 1
    assert _counter("sda_citation_checks_total", status="supported") == supported + 1
    root = _span(recorded, "answer")
    assert root.attributes["sda.status"] == "answered"
    assert root.attributes["langfuse.trace.output"] == "20 days."


def test_trace_survives_the_pickle_into_the_answer_cache():
    trace = Trace(tenant_id="t-1")
    with trace.root("q", "dense", [], True):
        with trace.stage("Reranking"):
            pass
        # The live span must not travel with the cached answer.
        restored = pickle.loads(pickle.dumps(trace))
    assert restored.query_id == trace.query_id
    assert [s.name for s in restored.stages] == ["Reranking"]
    assert not hasattr(restored, "_span")


def test_token_cost_prices_known_models_only():
    assert metrics.token_cost("gemini-3.6-flash", 1_000_000, 0) == pytest.approx(0.30)
    assert metrics.token_cost("gemini-3.6-flash", 0, 1_000_000) == pytest.approx(2.50)
    assert metrics.token_cost("model-nobody-priced", 1_000_000, 1_000_000) == 0.0


def test_record_tokens_counts_both_directions_and_spend():
    before = _counter("sda_llm_cost_usd_total", provider="gemini", model="gemini-3.6-flash")
    metrics.record_tokens("gemini", "gemini-3.6-flash", 2000, 500)
    assert (
        _counter("sda_llm_tokens_total", provider="gemini", model="gemini-3.6-flash", kind="prompt")
        >= 2000
    )
    assert _counter(
        "sda_llm_cost_usd_total", provider="gemini", model="gemini-3.6-flash"
    ) == pytest.approx(before + metrics.token_cost("gemini-3.6-flash", 2000, 500))


def test_sql_statements_become_client_spans(recorded):
    engine = create_engine("sqlite://")
    otel.instrument_engine(engine)
    with engine.connect() as conn:
        conn.execute(text("select 1 from sqlite_master"))

    span = _span(recorded, "select sqlite_master")
    assert span.attributes["db.system"] == "sqlite"
    assert span.attributes["db.statement"].startswith("select 1")


def test_failed_sql_ends_its_span_instead_of_leaking_it(recorded):
    engine = create_engine("sqlite://")
    otel.instrument_engine(engine)
    with engine.connect() as conn:
        with pytest.raises(OperationalError):
            conn.execute(text("select * from table_that_is_not_there"))
        assert not conn.info.get("sda_span")
    assert _span(recorded, "select table_that_is_not_there").status.description


def test_langfuse_stays_detached_without_keys():
    assert not llm_traces.enabled()


def test_log_lines_carry_the_correlation_ids(recorded, monkeypatch, capsys):
    # Re-run setup so the handler writes to the captured stdout; alembic's own fileConfig
    # replaces the root handlers earlier in the session.
    monkeypatch.setattr(logs, "_configured", False)
    logs.setup("test")
    trace = Trace(tenant_id="t-1")
    with trace.root("q", "dense", [], True):
        logs.logger("test").info("something happened")
    entry = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert entry["event"] == "something happened"
    assert entry["query_id"] == trace.query_id
    assert entry["tenant_id"] == "t-1"
    assert entry["trace_id"] == format(_span(recorded, "answer").context.trace_id, "032x")


def test_metrics_endpoint_serves_the_registry():
    response = TestClient(router.app).get("/metrics")
    assert response.status_code == 200
    assert "sda_answers_total" in response.text
    assert "sda_ingest_queue_depth" in response.text


def test_metrics_endpoint_requires_the_token_when_one_is_set(monkeypatch):
    guarded = replace(SETTINGS, observability=replace(SETTINGS.observability, metrics_token="s3cret"))
    monkeypatch.setattr(router, "SETTINGS", guarded)
    client = TestClient(router.app)
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_probe_paths_are_not_traced(recorded):
    TestClient(router.app).get("/livez")
    assert not recorded.get_finished_spans()
