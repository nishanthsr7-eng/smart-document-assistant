import pytest

from src.auth.principal import Principal
from src.core.config import SETTINGS
from src.core.errors import DocumentError
from src.ingestion import jobs, pipeline, worker
from src.storage import objects
from src.storage.db import session
from src.storage.models import Tenant
from src.storage.redis_client import client

OWNER = Principal(
    user_id="00000000-0000-0000-0000-000000000001",
    tenant_id="00000000-0000-0000-0000-0000000000aa",
    email="owner@acme.test",
    role="admin",
)

DOC = b"Employees accrue 20 days of paid leave per year.\n\nUnused days expire after 12 months.\n"


class _FakeEmbedder:
    def encode(self, texts):
        return [[0.01] * 768 for _ in texts]


@pytest.fixture
def worker_deps(monkeypatch, clean_state):
    monkeypatch.setattr(worker.deps, "embedder", lambda: _FakeEmbedder())
    monkeypatch.setattr(worker.deps, "figure_captioner", lambda: None)
    client().delete(SETTINGS.jobs.dlq_key)
    with session() as sess:
        sess.add(Tenant(tenant_id=OWNER.tenant_id, name="job-tests"))
    yield


def test_job_state_tracks_stages_and_result(worker_deps):
    doc_id = pipeline.doc_id_for(OWNER.tenant_id, DOC)
    job_id = jobs.job_id_for(doc_id)
    objects.put_raw(doc_id, "policy.txt", DOC)
    jobs.record_queued(job_id, doc_id, "policy.txt", OWNER)
    assert jobs.get(job_id, OWNER.tenant_id)["status"] == jobs.QUEUED

    report = worker._run(job_id, doc_id, "policy.txt", OWNER)

    state = jobs.get(job_id, OWNER.tenant_id)
    assert state["status"] == jobs.DONE
    assert state["report"]["num_children"] == report["num_children"]
    assert [d["doc_id"] for d in pipeline.list_indexed(OWNER.tenant_id)] == [doc_id]


def test_failed_job_lands_in_the_dlq_and_leaves_no_pending_document(worker_deps):
    doc_id = pipeline.doc_id_for(OWNER.tenant_id, b"   ")
    job_id = jobs.job_id_for(doc_id)
    objects.put_raw(doc_id, "broken.txt", b"   ")
    jobs.record_queued(job_id, doc_id, "broken.txt", OWNER)

    with pytest.raises(DocumentError):
        worker._run(job_id, doc_id, "broken.txt", OWNER)

    state = jobs.get(job_id, OWNER.tenant_id)
    assert state["status"] == jobs.FAILED
    assert state["error"]
    assert jobs.dead_letters(OWNER.tenant_id)[0]["job_id"] == job_id
    assert pipeline.list_indexed(OWNER.tenant_id) == []
