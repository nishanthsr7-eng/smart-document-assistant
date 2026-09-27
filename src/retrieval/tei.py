"""HTTP transport for HuggingFace Text Embeddings Inference.

Embedding and reranking are the two CPU-bound forward passes on the request path. Running them
in-process means every API replica carries the weights and serialises its own batches; TEI moves
them behind a service that batches across concurrent requests and can be scaled, quantised or
moved to a GPU node independently. The API keeps the same `Embedder` / `Reranker` surface, so the
choice is a URL in the environment, not a code path.
"""

import httpx

from src.core.config import SETTINGS
from src.core.errors import ModelUnavailable


class TeiClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._http = httpx.Client(
            base_url=self._base_url,
            timeout=httpx.Timeout(SETTINGS.models.tei_timeout_s),
            limits=httpx.Limits(max_connections=SETTINGS.models.tei_max_connections),
        )

    def post(self, path: str, payload: dict) -> list:
        try:
            response = self._http.post(path, json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ModelUnavailable(
                f"TEI at {self._base_url}{path} returned {exc.response.status_code}: {exc.response.text[:200]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelUnavailable(f"TEI at {self._base_url}{path} is unreachable: {exc}") from exc
        body: list = response.json()
        return body


class TeiEmbeddings:
    """`POST /embed`. Normalisation and truncation happen server-side, matching the local
    SentenceTransformer call, so vectors from either backend are interchangeable."""

    def __init__(self, base_url: str) -> None:
        self._client = TeiClient(base_url)

    def encode(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._client.post("/embed", {"inputs": texts, "normalize": True, "truncate": True})
        return [[float(v) for v in vector] for vector in vectors]


class TeiReranker:
    """`POST /rerank`, one call per distinct query. The endpoint takes a query and a list of
    passages, which is the shape every caller here already has: the cross-encoder pairs are
    grouped by query rather than sent one by one."""

    def __init__(self, base_url: str) -> None:
        self._client = TeiClient(base_url)

    def score(self, pairs: list[tuple[str, str]]) -> list[float]:
        if not pairs:
            return []
        grouped: dict[str, list[int]] = {}
        for index, (query, _) in enumerate(pairs):
            grouped.setdefault(query, []).append(index)

        scores = [0.0] * len(pairs)
        for query, indices in grouped.items():
            ranked = self._client.post(
                "/rerank",
                {"query": query, "texts": [pairs[i][1] for i in indices], "truncate": True},
            )
            for entry in ranked:
                scores[indices[entry["index"]]] = float(entry["score"])
        return scores


def health(base_url: str) -> None:
    """Readiness probe for a configured TEI service. Raises if it is not serving."""
    httpx.get(f"{base_url.rstrip('/')}/health", timeout=SETTINGS.models.tei_timeout_s).raise_for_status()
