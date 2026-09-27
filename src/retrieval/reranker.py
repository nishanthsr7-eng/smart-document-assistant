import dataclasses
import math

from src.core.config import SETTINGS
from src.retrieval.tei import TeiReranker
from src.retrieval.vector_store import Hit


class _LocalCrossEncoder:
    def __init__(self) -> None:
        from sentence_transformers import CrossEncoder

        self._model = CrossEncoder(SETTINGS.models.reranker_name, cache_folder=str(SETTINGS.paths.models))

    def score(self, pairs: list[tuple[str, str]]) -> list[float]:
        # num_labels=1 model: CrossEncoder applies sigmoid internally, so scores are already
        # probabilities. TEI's /rerank does the same, which is what keeps the two backends
        # interchangeable under one set of thresholds.
        return [float(p) for p in self._model.predict(pairs)]


class Reranker:
    """In-process weights by default; a TEI service when `TEI_RERANK_URL` is set."""

    def __init__(self) -> None:
        url = SETTINGS.models.tei_rerank_url
        self.backend = "tei" if url else "local"
        self._impl: TeiReranker | _LocalCrossEncoder = TeiReranker(url) if url else _LocalCrossEncoder()

    def rerank(self, queries: list[str], hits: list[Hit]) -> list[Hit]:
        """Score each candidate against every sub-query and keep its best.

        A passage that fully answers one half of a compound question should not be penalised for
        the half it does not answer. All pairs go out in one batch, so the extra sub-queries cost
        one larger forward pass rather than several round trips.
        """
        candidates = hits[: SETTINGS.retrieval.rerank_candidates]
        if not candidates:
            return []
        pairs = [(query, hit.text) for query in queries for hit in candidates]
        probabilities = self.score(pairs)
        best = [
            max(probabilities[i] for i in range(index, len(pairs), len(candidates)))
            for index in range(len(candidates))
        ]
        reranked = [
            dataclasses.replace(hit, score=probability)
            for hit, probability in zip(candidates, best, strict=True)
        ]
        reranked.sort(key=lambda h: h.score, reverse=True)
        return reranked

    def score(self, pairs: list[tuple[str, str]]) -> list[float]:
        if not pairs:
            return []
        return self._impl.score(pairs)

    @staticmethod
    def calibrate(scores: list[float]) -> list[float]:
        # Platt scaling in logit space: sigmoid(a * logit(raw) + b). a=1,b=0 (config default)
        # is the identity, since raw scores are already sigmoid outputs. Fit a/b on the golden
        # set (raw score -> observed correctness) to correct systematic bias.
        a = SETTINGS.retrieval.calibration_logistic_a
        b = SETTINGS.retrieval.calibration_logistic_b
        eps = 1e-6
        calibrated = []
        for s in scores:
            clipped = min(max(s, eps), 1.0 - eps)
            logit = math.log(clipped / (1.0 - clipped))
            calibrated.append(1.0 / (1.0 + math.exp(-(a * logit + b))))
        return calibrated
