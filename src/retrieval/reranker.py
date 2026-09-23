import dataclasses
import math

from sentence_transformers import CrossEncoder

from src.core.config import SETTINGS
from src.retrieval.vector_store import Hit


class Reranker:
    def __init__(self) -> None:
        self._model = CrossEncoder(SETTINGS.models.reranker_name, cache_folder=str(SETTINGS.paths.models))

    def rerank(self, question: str, hits: list[Hit]) -> list[Hit]:
        candidates = hits[: SETTINGS.retrieval.rerank_candidates]
        if not candidates:
            return []
        probabilities = self.score([(question, hit.text) for hit in candidates])
        reranked = [
            dataclasses.replace(hit, score=probability)
            for hit, probability in zip(candidates, probabilities)
        ]
        reranked.sort(key=lambda h: h.score, reverse=True)
        return reranked

    def score(self, pairs: list[tuple[str, str]]) -> list[float]:
        if not pairs:
            return []
        # num_labels=1 model: CrossEncoder applies sigmoid internally, so scores are already probabilities.
        return [float(p) for p in self._model.predict(pairs)]

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
