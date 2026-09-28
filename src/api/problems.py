"""RFC 9457 `application/problem+json` for every error this API returns.

One shape for all of them, so a client writes one parser: `type` is a stable URI the caller can
branch on, `title` is the human-readable class, and `detail` is the instance-specific sentence.
`detail` is kept because it is both an RFC 9457 member and what every existing client reads.
"""

from typing import Any, Mapping, Optional

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src.core.errors import (
    AuthError,
    DocumentError,
    DocumentNotFound,
    DocumentTooLarge,
    PermissionDenied,
    RateLimited,
)

CONTENT_TYPE = "application/problem+json"
# Problem types are URIs, not URLs: nothing has to be served from here for them to identify.
BASE_TYPE = "https://smartdocs.dev/problems"

# Fallback titles for statuses raised as a bare HTTPException.
_TITLES = {
    400: ("invalid-request", "Invalid request"),
    401: ("unauthenticated", "Not authenticated"),
    403: ("forbidden", "Forbidden"),
    404: ("not-found", "Not found"),
    409: ("conflict", "Conflict"),
    412: ("precondition-failed", "Precondition failed"),
    413: ("payload-too-large", "Payload too large"),
    415: ("unsupported-media-type", "Unsupported media type"),
    422: ("unprocessable-content", "Unprocessable content"),
    429: ("rate-limited", "Too many requests"),
    503: ("unavailable", "Service unavailable"),
}


def problem(
    request: Optional[Request],
    status: int,
    slug: str,
    title: str,
    detail: str,
    headers: Optional[Mapping[str, str]] = None,
    **extensions: Any,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"{BASE_TYPE}/{slug}",
        "title": title,
        "status": status,
        "detail": detail,
        **extensions,
    }
    if request is not None:
        body["instance"] = request.url.path
    return JSONResponse(
        status_code=status,
        content=body,
        media_type=CONTENT_TYPE,
        headers=dict(headers) if headers else None,
    )


def _http_exception(request: Request, exc: HTTPException) -> JSONResponse:
    slug, title = _TITLES.get(exc.status_code, ("error", "Error"))
    detail = exc.detail if isinstance(exc.detail, str) else title
    return problem(request, exc.status_code, slug, title, detail, headers=exc.headers)


def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Pydantic's per-field report kept as an extension member, not flattened into prose."""
    fields = [
        {"field": ".".join(str(part) for part in err["loc"][1:]), "message": err["msg"]}
        for err in exc.errors()
    ]
    detail = "; ".join(f"{f['field']}: {f['message']}" for f in fields) or "Invalid request body."
    return problem(
        request, 422, "unprocessable-content", "Unprocessable content", detail, errors=fields
    )


def _auth_error(request: Request, exc: AuthError) -> JSONResponse:
    return problem(
        request,
        401,
        "unauthenticated",
        "Not authenticated",
        exc.message,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _permission_denied(request: Request, exc: PermissionDenied) -> JSONResponse:
    return problem(request, 403, "forbidden", "Forbidden", exc.message)


def _rate_limited(request: Request, exc: RateLimited) -> JSONResponse:
    return problem(
        request,
        429,
        "rate-limited",
        "Too many requests",
        exc.message,
        headers={"Retry-After": str(exc.retry_after_s)},
        scope=exc.scope,
        retry_after_s=exc.retry_after_s,
    )


def _document_not_found(request: Request, exc: DocumentNotFound) -> JSONResponse:
    return problem(request, 404, "document-not-found", "Not found", exc.message)


def _document_too_large(request: Request, exc: DocumentTooLarge) -> JSONResponse:
    return problem(request, 413, "payload-too-large", "Payload too large", exc.message)


def _document_error(request: Request, exc: DocumentError) -> JSONResponse:
    return problem(request, 400, "invalid-document", "Invalid document", exc.message)


# Ordered most specific first: FastAPI dispatches on the exact class, so DocumentTooLarge needs
# its own entry even though DocumentError would otherwise cover it.
HANDLERS: list[tuple[type[Exception], Any]] = [
    (RequestValidationError, _validation_error),
    (HTTPException, _http_exception),
    (AuthError, _auth_error),
    (PermissionDenied, _permission_denied),
    (RateLimited, _rate_limited),
    (DocumentNotFound, _document_not_found),
    (DocumentTooLarge, _document_too_large),
    (DocumentError, _document_error),
]
