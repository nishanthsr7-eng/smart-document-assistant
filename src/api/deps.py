from functools import lru_cache
from typing import Callable, Optional

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.auth.principal import Principal
from src.auth.tokens import decode_access_token
from src.core.errors import AuthError, PermissionDenied
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
    return KeywordIndex()


@lru_cache(maxsize=1)
def reranker() -> Reranker:
    return Reranker()


@lru_cache(maxsize=1)
def llm_client() -> Provider:
    return build_client()


_bearer = HTTPBearer(auto_error=False)


def current_principal(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Principal:
    if credentials is None:
        raise AuthError("Authorization header missing.")
    return decode_access_token(credentials.credentials)


def _require(role: str) -> Callable[[Principal], Principal]:
    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.can(role):
            raise PermissionDenied(f"This action requires the '{role}' role.")
        return principal

    return dependency


# Viewer: read documents and ask questions. Editor: also upload and delete. Admin: also
# manage users and read the audit log.
require_viewer = _require("viewer")
require_editor = _require("editor")
require_admin = _require("admin")
