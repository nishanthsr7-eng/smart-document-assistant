"""Right-to-erasure for one data subject, with a receipt that can be checked afterwards.

Erasure here is not "issue some DELETEs and hope". Personal data for a user is spread over
Postgres (the user row, the audit log, the documents they uploaded and every chunk of them),
object storage (the raw upload and the parent blobs) and Redis (the answer and document caches,
which hold document text). This walks all three, then re-reads each one and counts what is left.
The receipt records both halves and is hashed, so the number that gets handed to a regulator or a
requester is one that was produced by a verification pass, not by the code that did the deleting.

The audit log is redacted in place rather than deleted: the security record of who did what stays,
the identifiers that make it personal data do not. That is the retention position this service
takes, and it is stated in the receipt so it is not a silent one.
"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional, cast

from sqlalchemy import CursorResult, Result, func, select, update
from sqlalchemy import delete as sa_delete

from src.auth.principal import Principal
from src.core.cache import ANSWER_CACHE, DOC_CACHE
from src.core.errors import PermissionDenied
from src.storage import objects
from src.storage.db import session
from src.storage.models import AuditEvent, Chunk, Document, User


def _rowcount(result: Result[Any]) -> int:
    return cast("CursorResult[Any]", result).rowcount


ERASED_EMAIL = "erased@invalid"
ERASED_USER = "erased"


@dataclass(frozen=True)
class Receipt:
    subject_user_id: str
    tenant_id: str
    erased_at: str
    doc_ids: list[str]
    deleted: dict[str, int]
    redacted: dict[str, int]
    # Post-delete re-read. Every count here must be zero for the erasure to be complete.
    remaining: dict[str, int]
    retained: dict[str, str] = field(default_factory=dict)

    @property
    def complete(self) -> bool:
        return not any(self.remaining.values())

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def erase_user(actor: Principal, user_id: str) -> Receipt:
    """Erase one user's personal data across Postgres, object storage and the caches."""
    if not actor.can("admin"):
        raise PermissionDenied("Only an admin can erase a user's data.")

    tenant_id = actor.tenant_id
    with session() as sess:
        doc_ids = list(
            sess.scalars(
                select(Document.doc_id).where(
                    Document.tenant_id == tenant_id, Document.owner_id == user_id
                )
            )
        )
        chunks = (
            sess.scalar(select(func.count()).select_from(Chunk).where(Chunk.doc_id.in_(doc_ids)))
            or 0
            if doc_ids
            else 0
        )
        documents = _rowcount(sess.execute(
            sa_delete(Document).where(
                Document.tenant_id == tenant_id, Document.owner_id == user_id
            )
        ))
        audit = _rowcount(sess.execute(
            update(AuditEvent)
            .where(AuditEvent.tenant_id == tenant_id, AuditEvent.user_id == user_id)
            .values(user_id=ERASED_USER, email=ERASED_EMAIL, detail={})
        ))
        users = _rowcount(
            sess.execute(
                sa_delete(User).where(User.tenant_id == tenant_id, User.user_id == user_id)
            )
        )

    for doc_id in doc_ids:
        objects.delete_doc(doc_id)
        DOC_CACHE.delete(doc_id)
    # The answer cache is keyed by question and configuration, not by document, so there is no
    # way to evict exactly the entries that quoted an erased document. It goes entirely.
    ANSWER_CACHE.clear()

    receipt = Receipt(
        subject_user_id=user_id,
        tenant_id=tenant_id,
        erased_at=datetime.now(timezone.utc).isoformat(),
        doc_ids=doc_ids,
        deleted={"users": users, "documents": documents, "chunks": chunks},
        redacted={"audit_events": audit},
        remaining=_verify(tenant_id, user_id, doc_ids),
        retained={
            "audit_log": "Events kept with the subject's user id and email replaced; the "
            "detail payload is dropped."
        },
    )
    return receipt


def _verify(tenant_id: str, user_id: str, doc_ids: list[str]) -> dict[str, int]:
    """Re-read every store the erasure touched and count what answers to the subject."""
    with session() as sess:
        remaining: dict[str, Optional[int]] = {
            "users": sess.scalar(
                select(func.count()).select_from(User).where(User.user_id == user_id)
            ),
            "documents": sess.scalar(
                select(func.count())
                .select_from(Document)
                .where(Document.tenant_id == tenant_id, Document.owner_id == user_id)
            ),
            "chunks": sess.scalar(
                select(func.count()).select_from(Chunk).where(Chunk.doc_id.in_(doc_ids))
            )
            if doc_ids
            else 0,
            "audit_events": sess.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.tenant_id == tenant_id, AuditEvent.user_id == user_id)
            ),
        }
    remaining["objects"] = sum(len(objects.list_doc_keys(doc_id)) for doc_id in doc_ids)
    remaining["cached_documents"] = sum(DOC_CACHE.get(doc_id) is not None for doc_id in doc_ids)
    return {key: value or 0 for key, value in remaining.items()}
