"""Opaque keyset cursors.

Keyset rather than offset: a document inserted while a client is paging shifts every subsequent
offset and makes it skip a row, whereas `(created_at, doc_id) > last` is stable under insertion.
The cursor is base64 so nobody builds one by hand and depends on its shape.
"""

import base64
import binascii
from datetime import datetime
from typing import Optional

from fastapi import HTTPException

_SEP = "\x1f"


def encode(created_at: datetime, doc_id: str) -> str:
    raw = f"{created_at.isoformat()}{_SEP}{doc_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(cursor: str) -> tuple[datetime, str]:
    """A malformed cursor is the caller's error, not a 500."""
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        stamp, _, doc_id = base64.urlsafe_b64decode(padded).decode().partition(_SEP)
        return datetime.fromisoformat(stamp), doc_id
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Malformed cursor.") from exc


def decode_optional(cursor: Optional[str]) -> Optional[tuple[datetime, str]]:
    return decode(cursor) if cursor else None
