from functools import lru_cache

from src.generation.client import Provider, build_client
from src.ingestion.parsers import FigureCaptioner
from src.retrieval.embedder import Embedder
from src.retrieval.keyword_index import KeywordIndex
from src.retrieval.reranker import Reranker
from src.retrieval.vector_store import VectorStore


@lru_cache(maxsize=1)
def embedder() -> Embedder:
    return Embedder()


@lru_cache(maxsize=1)
def figure_captioner() -> FigureCaptioner:
    return FigureCaptioner()


@lru_cache(maxsize=1)
def vector_store() -> VectorStore:
    return VectorStore()


@lru_cache(maxsize=1)
def keyword_index() -> KeywordIndex:
    return KeywordIndex(vector_store())


@lru_cache(maxsize=1)
def reranker() -> Reranker:
    return Reranker()


@lru_cache(maxsize=1)
def llm_client() -> Provider:
    return build_client()
