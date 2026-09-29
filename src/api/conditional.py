"""`ETag` / `If-None-Match` for the collections a client polls.

The SPA re-reads `/documents` after every ingest and on every reconnect; almost all of those
reads are unchanged. The tag is a strong hash of the serialized body, so it changes exactly when
the body does, and a match costs a 304 with no body instead of the list.
"""

import hashlib
import json
from typing import Any, Optional

from fastapi import Request, Response


def etag_for(payload: Any) -> str:
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:32]
    return f'"{digest}"'


def matches(request: Request, etag: str) -> bool:
    """RFC 9110 If-None-Match: a comma-separated list, or `*` for "any current representation"."""
    header = request.headers.get("if-none-match")
    if not header:
        return False
    candidates = {value.strip() for value in header.split(",")}
    return "*" in candidates or etag in candidates or f"W/{etag}" in candidates


def not_modified(etag: str, cache_control: str = "private, no-cache") -> Response:
    return Response(status_code=304, headers={"ETag": etag, "Cache-Control": cache_control})


def tag(response: Response, etag: str, cache_control: str = "private, no-cache") -> None:
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = cache_control


def conditional(
    request: Request, response: Response, payload: Any
) -> Optional[Response]:
    """Tag the response, and return a 304 to send instead when the client already has it."""
    etag = etag_for(payload)
    if matches(request, etag):
        return not_modified(etag)
    tag(response, etag)
    return None
