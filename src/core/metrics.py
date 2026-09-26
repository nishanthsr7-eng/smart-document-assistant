"""Prometheus metrics. One definition site, so the dashboards and alerts have a fixed contract.

Under `uvicorn --workers N` each worker holds its own counters, so the API runs prometheus_client
in multiprocess mode (`PROMETHEUS_MULTIPROC_DIR`, set by the container entrypoint) and `render`
aggregates across the workers of one pod. Without that variable the process-local registry is
used, which is what a single-process worker or a test wants.
"""

import os
from typing import Optional

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)

from src.core.config import SETTINGS

_LATENCY_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0)
_INGEST_BUCKETS = (1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 600.0, 900.0)
_SCORE_BUCKETS = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0)

HTTP_REQUESTS = Counter(
    "sda_http_requests_total", "HTTP requests served.", ["method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "sda_http_request_duration_seconds",
    "HTTP request latency.",
    ["method", "route"],
    buckets=_LATENCY_BUCKETS,
)

STAGE_DURATION = Histogram(
    "sda_stage_duration_seconds",
    "Answer pipeline stage latency.",
    ["stage"],
    buckets=_LATENCY_BUCKETS,
)
ANSWER_DURATION = Histogram(
    "sda_answer_duration_seconds",
    "End-to-end answer latency, cache misses only.",
    buckets=_LATENCY_BUCKETS,
)
ANSWERS = Counter(
    "sda_answers_total",
    "Answers served, by outcome. The abstain rate is the 'abstained' share.",
    ["status", "confidence"],
)
CONFIDENCE = Histogram(
    "sda_confidence_score", "Confidence score distribution.", buckets=_SCORE_BUCKETS
)
CITATION_CHECKS = Counter(
    "sda_citation_checks_total",
    "Cited sentences by verifier verdict. Citation validity is supported / (supported+unsupported).",
    ["status"],
)
RETRIEVAL_TOP_SCORE = Histogram(
    "sda_retrieval_top_score",
    "Top fused retrieval score per query, the input to the abstain gate.",
    buckets=_SCORE_BUCKETS,
)
LLM_TOKENS = Counter(
    "sda_llm_tokens_total", "Generation tokens.", ["provider", "model", "kind"]
)
LLM_COST = Counter(
    "sda_llm_cost_usd_total", "Generation spend, priced from config.", ["provider", "model"]
)
ANSWER_CACHE = Counter("sda_answer_cache_total", "Answer cache lookups.", ["result"])

INGEST_JOBS = Counter("sda_ingest_jobs_total", "Ingest jobs by outcome.", ["outcome"])
INGEST_DURATION = Histogram(
    "sda_ingest_duration_seconds", "Ingest job latency.", buckets=_INGEST_BUCKETS
)
INGEST_CHUNKS = Counter("sda_ingest_chunks_total", "Child chunks indexed.")
QUEUE_DEPTH = Gauge(
    "sda_ingest_queue_depth",
    "Jobs waiting in the ingest queue.",
    # Every replica reports the same shared queue, so aggregating by max is the true depth.
    multiprocess_mode="max",
)


def stage_label(name: str) -> str:
    """Stage names carry counts ("Searching 3 document(s)"); the first word is the bounded part."""
    return name.split()[0].lower() if name.strip() else "unknown"


def registry() -> CollectorRegistry:
    directory = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not directory:
        return REGISTRY
    collected = CollectorRegistry()
    multiprocess.MultiProcessCollector(collected, path=directory)
    return collected


def render() -> tuple[bytes, str]:
    return generate_latest(registry()), CONTENT_TYPE_LATEST


def record_tokens(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> None:
    LLM_TOKENS.labels(provider, model, "prompt").inc(prompt_tokens)
    LLM_TOKENS.labels(provider, model, "completion").inc(completion_tokens)
    cost = token_cost(model, prompt_tokens, completion_tokens)
    if cost:
        LLM_COST.labels(provider, model).inc(cost)


def token_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    price: Optional[tuple[float, float]] = SETTINGS.observability.pricing.get(model)
    if price is None:
        return 0.0
    return (prompt_tokens * price[0] + completion_tokens * price[1]) / 1_000_000
