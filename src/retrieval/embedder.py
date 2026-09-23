from sentence_transformers import SentenceTransformer

from src.core.config import SETTINGS


class Embedder:
    def __init__(self) -> None:
        self._model = SentenceTransformer(SETTINGS.models.embedder_name)
        self._query_prefix = SETTINGS.models.embedder_query_prefix

    def encode(self, texts: list[str]) -> list[list[float]]:
        return self._model.encode(
            texts, normalize_embeddings=True, batch_size=32, show_progress_bar=False
        ).tolist()

    def encode_query(self, texts: list[str]) -> list[list[float]]:
        """Encode queries with instruction prefix for asymmetric retrieval."""
        return self.encode([self._query_prefix + t for t in texts])
