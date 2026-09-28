from functools import lru_cache
from hmac import compare_digest
from typing import Callable, Optional

from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.auth.principal import Principal
from src.auth.tokens import decode_access_token
from src.core.config import SETTINGS
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


def require_operator(
    principal: Principal = Depends(require_admin),
    token: Optional[str] = Header(default=None, alias="X-Operator-Token"),
) -> Principal:
    """A deployment-wide operation needs a deployment-level credential.

    Reindexing and the retention sweep act on every tenant, so a tenant admin's own token is
    the wrong authority for them. Unset `OPERATOR_TOKEN` disables these endpoints rather than
    opening them: the failure mode of the other choice is one tenant rebuilding everyone's index.
    """
    expected = SETTINGS.security.operator_token
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Operator endpoints are disabled: OPERATOR_TOKEN is not configured.",
        )
    if token is None or not compare_digest(token, expected):
        raise PermissionDenied("A valid X-Operator-Token is required.")
    return principal
