from typing import cast

from src.core.config import SETTINGS
from src.retrieval.tei import TeiEmbeddings


class _LocalEmbeddings:
    def __init__(self) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(SETTINGS.models.embedder_name)

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(
            texts, normalize_embeddings=True, batch_size=32, show_progress_bar=False
        )
        return cast(list[list[float]], vectors.tolist())


class Embedder:
    """In-process weights by default; a TEI service when `TEI_EMBED_URL` is set."""

    def __init__(self) -> None:
        url = SETTINGS.models.tei_embed_url
        self.backend = "tei" if url else "local"
        self._impl: TeiEmbeddings | _LocalEmbeddings = TeiEmbeddings(url) if url else _LocalEmbeddings()
        self._query_prefix = SETTINGS.models.embedder_query_prefix

    def encode(self, texts: list[str]) -> list[list[float]]:
        return self._impl.encode(texts)

    def encode_query(self, texts: list[str]) -> list[list[float]]:
        """Encode queries with instruction prefix for asymmetric retrieval."""
        return self.encode([self._query_prefix + t for t in texts])
