from types import SimpleNamespace

import pytest

from src.core.config import SETTINGS
from src.ingestion import pipeline


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    store = tmp_path / "parents"
    paths = SimpleNamespace(parents=store)
    monkeypatch.setattr(pipeline, "SETTINGS", SimpleNamespace(paths=paths, ingest_version=SETTINGS.ingest_version))
    monkeypatch.setattr(pipeline, "_MANIFEST", store / "manifest.json")
    return store


DOC = b"1 Policy\n\nEmployees accrue leave each pay period. Sick leave is separate.\n\n- rule one\n- rule two"


def test_first_ingest_indexes(isolated_store):
    report = pipeline.ingest("policy.txt", DOC)
    assert report.outcome == "indexed"
    assert report.num_children >= 1
    assert (isolated_store / f"{report.doc_id}.json").exists()


def test_reingest_same_bytes_is_duplicate(isolated_store):
    first = pipeline.ingest("policy.txt", DOC)
    second = pipeline.ingest("policy.txt", DOC)
    assert second.outcome == "duplicate"
    assert second.doc_id == first.doc_id
    assert second.num_children == first.num_children


def test_same_filename_new_content_replaces(isolated_store):
    first = pipeline.ingest("policy.txt", DOC)
    report = pipeline.ingest("policy.txt", DOC + b"\n\nExtra clause added here.")
    assert report.outcome == "replaced"
    assert report.replaced_doc_id == first.doc_id
    manifest = pipeline._load_manifest()
    assert len(manifest) == 1
    assert not (isolated_store / f"{first.doc_id}.json").exists()


def test_list_indexed_reflects_manifest(isolated_store):
    report = pipeline.ingest("policy.txt", DOC)
    entries = {e["doc_id"]: e for e in pipeline.list_indexed()}
    assert entries[report.doc_id]["filename"] == "policy.txt"
    assert entries[report.doc_id]["num_children"] == report.num_children


def test_delete_removes_from_manifest_and_disk(isolated_store):
    report = pipeline.ingest("policy.txt", DOC)
    pipeline.delete(report.doc_id)
    assert report.doc_id not in pipeline._load_manifest()
    assert not (isolated_store / f"{report.doc_id}.json").exists()


def test_roundtrip_parents_and_children(isolated_store):
    report = pipeline.ingest("policy.txt", DOC)
    parents = pipeline.load_parents(report.doc_id)
    children = pipeline.load_children(report.doc_id)
    assert len(parents) == report.num_parents
    assert len(children) == report.num_children
    assert all(isinstance(c.section_path, tuple) for c in children)
    assert all(isinstance(c.char_span_in_parent, tuple) for c in children)
