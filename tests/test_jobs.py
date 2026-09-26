import hashlib

import pytest

from src.core.config import SETTINGS
from src.core.errors import DocumentError
from src.ingestion import jobs, pipeline, worker
from src.storage import objects
from src.storage.redis_client import client

DOC = b"Employees accrue 20 days of paid leave per year.\n\nUnused days expire after 12 months.\n"


class _FakeEmbedder:
    def encode(self, texts):
        return [[0.01] * 768 for _ in texts]


@pytest.fixture
def worker_deps(monkeypatch, clean_state):
    monkeypatch.setattr(worker.deps, "embedder", lambda: _FakeEmbedder())
    monkeypatch.setattr(worker.deps, "figure_captioner", lambda: None)
    client().delete(SETTINGS.jobs.dlq_key)
    yield


def test_job_state_tracks_stages_and_result(worker_deps):
    doc_id = hashlib.sha256(DOC).hexdigest()
    job_id = jobs.job_id_for(doc_id)
    objects.put_raw(doc_id, "policy.txt", DOC)
    jobs.record_queued(job_id, doc_id, "policy.txt")
    assert jobs.get(job_id)["status"] == jobs.QUEUED

    report = worker._run(job_id, doc_id, "policy.txt")

    state = jobs.get(job_id)
    assert state["status"] == jobs.DONE
    assert state["report"]["num_children"] == report["num_children"]
    assert [d["doc_id"] for d in pipeline.list_indexed()] == [doc_id]


def test_failed_job_lands_in_the_dlq_and_leaves_no_pending_document(worker_deps):
    doc_id = hashlib.sha256(b"   ").hexdigest()
    job_id = jobs.job_id_for(doc_id)
    objects.put_raw(doc_id, "broken.txt", b"   ")
    jobs.record_queued(job_id, doc_id, "broken.txt")

    with pytest.raises(DocumentError):
        worker._run(job_id, doc_id, "broken.txt")

    state = jobs.get(job_id)
    assert state["status"] == jobs.FAILED
    assert state["error"]
    assert jobs.dead_letters()[0]["job_id"] == job_id
    assert pipeline.list_indexed() == []
