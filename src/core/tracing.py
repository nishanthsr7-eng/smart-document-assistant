import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional
from uuid import uuid4

from opentelemetry.trace import Span

from src.core import llm_traces, logs, metrics, otel

OnStage = Callable[[str], None]

logger = logs.logger("sda.trace")

# Scalar payload keys worth persisting; large blobs (prompts, hit dumps) stay UI-only.
_LOG_KEYS = {
    "mode",
    "standalone_question",
    "dense_hits",
    "lexical_hits",
    "retrieval_consensus",
    "top_score",
    "margin",
    "sources",
    "unsupported",
    "prompt_tokens",
    "completion_tokens",
    "reason",
    "cache_hit",
}


@dataclass
class StageRecord:
    name: str
    duration_s: float
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Trace:
    stages: list[StageRecord] = field(default_factory=list)
    query_id: str = field(default_factory=lambda: uuid4().hex[:12])
    tenant_id: str = ""
    user_id: str = ""
    started: float = field(default_factory=time.perf_counter)

    def to_json(self) -> str:
        return json.dumps(
            {
                "query_id": self.query_id,
                "total_duration_s": round(sum(s.duration_s for s in self.stages), 4),
                "stages": [
                    {
                        "name": s.name,
                        "duration_s": round(s.duration_s, 4),
                        **{k: v for k, v in s.payload.items() if k in _LOG_KEYS},
                    }
                    for s in self.stages
                ],
            }
        )

    def log(self) -> None:
        logger.info("answer.trace", **json.loads(self.to_json()))

    def __getstate__(self) -> dict[str, Any]:
        # A Trace travels into the answer cache by pickle; a live span cannot go with it.
        return {k: v for k, v in self.__dict__.items() if k != "_span"}

    @contextmanager
    def root(self, question: str, mode: str, doc_ids: list[str], generate: bool) -> Iterator[None]:
        """The span every stage hangs off, and the Langfuse trace for this query."""
        with otel.tracer().start_as_current_span("answer") as span:
            self._span: Optional[Span] = span
            span.set_attribute("sda.query_id", self.query_id)
            span.set_attribute("sda.tenant_id", self.tenant_id)
            span.set_attribute("sda.mode", mode)
            span.set_attribute("sda.num_docs", len(doc_ids))
            span.set_attribute("sda.generate", generate)
            llm_traces.annotate_trace(
                span,
                name="answer",
                user_id=self.user_id,
                session_id=self.tenant_id,
                question=question,
                metadata={"mode": mode, "doc_ids": doc_ids, "generate": generate},
                tags=[mode],
            )
            logs.bind(query_id=self.query_id, tenant_id=self.tenant_id)
            try:
                yield
            finally:
                self._span = None
                logs.unbind("query_id", "tenant_id")

    @contextmanager
    def stage(self, name: str, on_stage: Optional[OnStage] = None) -> Iterator[dict[str, Any]]:
        if on_stage is not None:
            on_stage(name)
        payload: dict[str, Any] = {}
        label = metrics.stage_label(name)
        started = time.perf_counter()
        with otel.tracer().start_as_current_span(label) as span:
            span.set_attribute("sda.stage", name)
            # An exception marks the span ERROR through the span context manager itself; the
            # record is appended either way so a failed answer still reports its timings.
            try:
                yield payload
            finally:
                duration = time.perf_counter() - started
                metrics.STAGE_DURATION.labels(label).observe(duration)
                self.stages.append(StageRecord(name=name, duration_s=duration, payload=payload))
                _annotate_stage(span, label, payload)

    def finish(
        self,
        *,
        status: str,
        answer_text: str,
        confidence_label: str,
        confidence_score: Optional[float],
        citation_statuses: list[str],
        abstain_reason: Optional[str],
        cached: bool = False,
    ) -> None:
        """Answer-level metrics and the Langfuse trace output. Called once per served answer."""
        metrics.ANSWERS.labels(status, confidence_label).inc()
        metrics.ANSWER_CACHE.labels("hit" if cached else "miss").inc()
        for citation_status in citation_statuses:
            metrics.CITATION_CHECKS.labels(citation_status).inc()
        if confidence_score is not None:
            metrics.CONFIDENCE.observe(confidence_score)
        if not cached:
            metrics.ANSWER_DURATION.observe(time.perf_counter() - self.started)

        span = getattr(self, "_span", None)
        if span is not None:
            span.set_attribute("sda.status", status)
            span.set_attribute("sda.confidence", confidence_label)
            if abstain_reason:
                span.set_attribute("sda.abstain_reason", abstain_reason)
            llm_traces.annotate_trace(
                span,
                name="answer",
                answer=answer_text or abstain_reason or "",
                metadata={"status": status, "confidence": confidence_label, "cached": cached},
            )
        llm_traces.flush()


def _annotate_stage(span: Span, label: str, payload: dict[str, Any]) -> None:
    for key, value in payload.items():
        # A stage with no hits reports top_score as None, which is neither an attribute value
        # nor an observation.
        if key in _LOG_KEYS and value is not None:
            span.set_attribute(f"sda.{key}", value)
    if payload.get("top_score") is not None:
        metrics.RETRIEVAL_TOP_SCORE.observe(payload["top_score"])
    if label != "generating":
        llm_traces.annotate_observation(
            span,
            kind="retriever" if label == "searching" else "span",
            metadata={k: v for k, v in payload.items() if k in _LOG_KEYS and v is not None},
        )
        return
    llm_traces.annotate_observation(
        span,
        kind="generation",
        input_payload=payload.get("prompt"),
        output_payload=payload.get("completion"),
        model=payload.get("model", ""),
        usage={
            "input": payload.get("prompt_tokens", 0),
            "output": payload.get("completion_tokens", 0),
        },
        cost={"total": payload["cost_usd"]} if "cost_usd" in payload else None,
    )
