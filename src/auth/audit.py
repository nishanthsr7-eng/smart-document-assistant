from typing import Optional

from sqlalchemy import select

from src.auth.principal import Principal
from src.core.config import SETTINGS
from src.core.errors import PermissionDenied
from src.storage.db import session
from src.storage.models import AuditEvent


def record(
    actor: Principal, action: str, doc_id: Optional[str] = None, **detail: object
) -> None:
    with session() as sess:
        sess.add(
            AuditEvent(
                tenant_id=actor.tenant_id,
                user_id=actor.user_id,
                email=actor.email,
                action=action,
                doc_id=doc_id,
                detail=detail,
            )
        )


def read(actor: Principal, limit: int = 0) -> list[dict]:
    if not actor.can("admin"):
        raise PermissionDenied("Only an admin can read the audit log.")
    with session() as sess:
        stmt = (
            select(AuditEvent)
            .where(AuditEvent.tenant_id == actor.tenant_id)
            .order_by(AuditEvent.created_at.desc(), AuditEvent.event_id.desc())
            .limit(limit or SETTINGS.auth.audit_page_size)
        )
        return [
            {
                "event_id": e.event_id,
                "email": e.email,
                "action": e.action,
                "doc_id": e.doc_id,
                "detail": e.detail,
                "created_at": e.created_at.isoformat(),
            }
            for e in sess.scalars(stmt)
        ]
