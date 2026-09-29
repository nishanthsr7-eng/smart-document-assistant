"""Right-to-erasure against the real Postgres, Redis and MinIO.

Mocking these would defeat the purpose: the claim under test is that nothing is left in any of
the three stores, and only the stores can say.
"""

import pytest
from sqlalchemy import func, select

from src.auth import audit, service
from src.core.config import SETTINGS
from src.core.errors import PermissionDenied
from src.ingestion import pipeline
from src.storage import erasure, objects
from src.storage.db import session
from src.storage.models import AuditEvent, Chunk, Document, User

DIM = SETTINGS.storage.embedding_dim

pytestmark = pytest.mark.usefixtures("clean_state")

DOCUMENT = (
    b"1 Personal\n\n"
    b"Contact the subject at subject@acme.test about the 2026 leave carryover.\n"
)


class _FakeEmbedder:
    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] * DIM for _ in texts]

    def encode_query(self, texts: list[str]) -> list[list[float]]:
        return self.encode(texts)


@pytest.fixture
def tenant():
    admin = service.register_tenant("Acme", "admin@acme.test", "acme-password-1")
    subject = service.create_user(admin, "subject@acme.test", "subject-password-1", "editor")
    return admin, subject


def _ingest(owner):
    return pipeline.ingest("policy.txt", DOCUMENT, _FakeEmbedder(), owner)


def test_erasure_removes_the_user_their_documents_and_their_chunks(tenant):
    admin, subject = tenant
    report = _ingest(subject)
    audit.record(subject, "query", chars=12)

    receipt = erasure.erase_user(admin, subject.user_id)

    assert receipt.complete
    assert receipt.doc_ids == [report.doc_id]
    assert receipt.deleted["users"] == 1 and receipt.deleted["documents"] == 1
    assert receipt.deleted["chunks"] > 0
    with session() as sess:
        assert sess.scalar(select(func.count()).select_from(User).where(User.user_id == subject.user_id)) == 0
        assert sess.scalar(select(func.count()).select_from(Document).where(Document.doc_id == report.doc_id)) == 0
        assert sess.scalar(select(func.count()).select_from(Chunk).where(Chunk.doc_id == report.doc_id)) == 0


def test_erasure_removes_the_blobs_from_object_storage(tenant):
    admin, subject = tenant
    report = _ingest(subject)
    assert objects.list_doc_keys(report.doc_id)

    erasure.erase_user(admin, subject.user_id)

    assert objects.list_doc_keys(report.doc_id) == []


def test_the_audit_log_is_redacted_rather_than_deleted(tenant):
    admin, subject = tenant
    audit.record(subject, "query", chars=12, question="ask about subject@acme.test")

    receipt = erasure.erase_user(admin, subject.user_id)

    assert receipt.redacted["audit_events"] >= 1
    with session() as sess:
        events = list(sess.scalars(select(AuditEvent).where(AuditEvent.action == "query")))
    assert events, "the security record itself must survive"
    assert all(e.user_id == erasure.ERASED_USER and e.email == erasure.ERASED_EMAIL for e in events)
    assert all(e.detail == {} for e in events)
    assert "audit_log" in receipt.retained


def test_the_receipt_reports_incompleteness_rather_than_claiming_success(tenant, monkeypatch):
    admin, subject = tenant
    report = _ingest(subject)
    # A blob delete that silently does nothing is the realistic partial failure: the rows go,
    # the bytes stay. The receipt has to say so instead of reporting a clean erasure.
    monkeypatch.setattr(objects, "delete_doc", lambda doc_id: None)

    receipt = erasure.erase_user(admin, subject.user_id)

    assert not receipt.complete
    assert receipt.remaining["objects"] > 0
    assert report.doc_id in receipt.doc_ids


def test_the_receipt_digest_covers_the_verification_result(tenant):
    admin, subject = tenant
    _ingest(subject)
    receipt = erasure.erase_user(admin, subject.user_id)

    tampered = type(receipt)(**{**receipt.__dict__, "remaining": {"objects": 3}})
    assert tampered.digest() != receipt.digest()


def test_only_an_admin_can_erase(tenant):
    _admin, subject = tenant
    with pytest.raises(PermissionDenied):
        erasure.erase_user(subject, subject.user_id)


def test_erasure_never_reaches_another_tenant(tenant):
    admin, _subject = tenant
    other_admin = service.register_tenant("Globex", "admin@globex.test", "globex-password-1")
    other_report = _ingest(other_admin)

    receipt = erasure.erase_user(admin, other_admin.user_id)

    # A foreign user id matches nothing: the tenant comes from the actor, never the parameter.
    assert receipt.deleted == {"users": 0, "documents": 0, "chunks": 0}
    with session() as sess:
        assert sess.scalar(
            select(func.count()).select_from(Document).where(Document.doc_id == other_report.doc_id)
        ) == 1
