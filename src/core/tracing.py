import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Optional
from uuid import uuid4

OnStage = Callable[[str], None]

logger = logging.getLogger("sda.trace")

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
        logger.info(self.to_json())

    @contextmanager
    def stage(self, name: str, on_stage: Optional[OnStage] = None) -> Iterator[dict[str, Any]]:
        if on_stage is not None:
            on_stage(name)
        payload: dict[str, Any] = {}
        started = time.perf_counter()
        try:
            yield payload
        finally:
            self.stages.append(StageRecord(name=name, duration_s=time.perf_counter() - started, payload=payload))
