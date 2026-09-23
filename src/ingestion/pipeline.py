import hashlib
import json
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Literal, Optional

from src.core.cache import ANSWER_CACHE, TTLCache
from src.core.config import SETTINGS
from src.core.tracing import OnStage
from src.ingestion.chunker import ChildChunk, ParentChunk, chunk_document
from src.ingestion.parsers import Element, FigureCaptioner, parse, validate_upload

Outcome = Literal["indexed", "replaced", "duplicate"]

_MANIFEST = SETTINGS.paths.parents / "manifest.json"

# Parents/children are read on every query (B6); cache the parsed JSON per doc_id instead of
# re-reading and re-parsing the file each time. TTL is unbounded — entries are invalidated
# explicitly on ingest/replace/delete rather than by time.
_DOC_CACHE: TTLCache[dict] = TTLCache(SETTINGS.ingestion.parent_cache_size, ttl_s=float("inf"))


@dataclass
class IngestReport:
    doc_id: str
    filename: str
    pages: int
    elements_by_kind: dict[str, int]
    num_parents: int
    num_children: int
    outcome: Outcome
    replaced_doc_id: Optional[str] = None


def ingest(
    filename: str,
    data: bytes,
    captioner: Optional[FigureCaptioner] = None,
    on_stage: Optional[OnStage] = None,
) -> IngestReport:
    extension = validate_upload(filename, data)
    doc_id = hashlib.sha256(data).hexdigest()

    manifest = _load_manifest()
    existing = manifest.get(doc_id)
    if existing and existing["ingest_version"] == SETTINGS.ingest_version:
        return IngestReport(
            doc_id=doc_id,
            filename=existing["filename"],
            pages=existing["pages"],
            elements_by_kind=existing["elements_by_kind"],
            num_parents=existing["num_parents"],
            num_children=existing["num_children"],
            outcome="duplicate",
        )

    replaced = _find_by_filename(manifest, filename, doc_id)

    _emit(on_stage, "Parsing document")
    elements = parse(extension, data, captioner)

    figures = sum(1 for e in elements if e.kind == "figure")
    if figures and captioner is not None:
        _emit(on_stage, f"Captioning {figures} figure(s)")

    _emit(on_stage, "Chunking")
    parents, children = chunk_document(elements, doc_id, filename)

    _emit(on_stage, f"Indexing {len(children)} chunks")
    if replaced:
        _delete(replaced, manifest)
    _persist(doc_id, parents, children)

    report = IngestReport(
        doc_id=doc_id,
        filename=filename,
        pages=_page_count(elements),
        elements_by_kind=dict(Counter(e.kind for e in elements)),
        num_parents=len(parents),
        num_children=len(children),
        outcome="replaced" if replaced else "indexed",
        replaced_doc_id=replaced,
    )
    manifest[doc_id] = {
        "filename": filename,
        "ingest_version": SETTINGS.ingest_version,
        "pages": report.pages,
        "elements_by_kind": report.elements_by_kind,
        "num_parents": report.num_parents,
        "num_children": report.num_children,
    }
    _save_manifest(manifest)
    ANSWER_CACHE.clear()
    return report


def list_indexed() -> list[dict]:
    manifest = _load_manifest()
    return [
        {
            "doc_id": doc_id,
            "filename": entry["filename"],
            "pages": entry["pages"],
            "num_children": entry["num_children"],
        }
        for doc_id, entry in manifest.items()
        if entry["ingest_version"] == SETTINGS.ingest_version
    ]


def delete(doc_id: str) -> None:
    manifest = _load_manifest()
    if doc_id in manifest:
        _delete(doc_id, manifest)
        _save_manifest(manifest)
        ANSWER_CACHE.clear()


def load_parents(doc_id: str) -> list[ParentChunk]:
    return [_as_parent(p) for p in _load_payload(doc_id)["parents"]]


def load_children(doc_id: str) -> list[ChildChunk]:
    return [_as_child(c) for c in _load_payload(doc_id)["children"]]


def _load_payload(doc_id: str) -> dict:
    payload = _DOC_CACHE.get(doc_id)
    if payload is None:
        path = SETTINGS.paths.parents / f"{doc_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        _DOC_CACHE.set(doc_id, payload)
    return payload


def _as_parent(data: dict) -> ParentChunk:
    data = {**data, "section_path": tuple(data["section_path"])}
    return ParentChunk(**data)


def _as_child(data: dict) -> ChildChunk:
    data = {
        **data,
        "section_path": tuple(data["section_path"]),
        "char_span_in_parent": tuple(data["char_span_in_parent"]),
    }
    return ChildChunk(**data)


def _persist(doc_id: str, parents: list[ParentChunk], children: list[ChildChunk]) -> None:
    SETTINGS.paths.parents.mkdir(parents=True, exist_ok=True)
    path = SETTINGS.paths.parents / f"{doc_id}.json"
    payload = {
        "parents": [asdict(p) for p in parents],
        "children": [asdict(c) for c in children],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    _DOC_CACHE.delete(doc_id)


def _delete(doc_id: str, manifest: dict) -> None:
    path = SETTINGS.paths.parents / f"{doc_id}.json"
    path.unlink(missing_ok=True)
    manifest.pop(doc_id, None)
    _DOC_CACHE.delete(doc_id)


def _find_by_filename(manifest: dict, filename: str, new_doc_id: str) -> Optional[str]:
    for doc_id, entry in manifest.items():
        if doc_id != new_doc_id and entry["filename"] == filename:
            return doc_id
    return None


def _load_manifest() -> dict:
    if _MANIFEST.exists():
        return json.loads(_MANIFEST.read_text(encoding="utf-8"))
    return {}


def _save_manifest(manifest: dict) -> None:
    SETTINGS.paths.parents.mkdir(parents=True, exist_ok=True)
    _MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _page_count(elements: list[Element]) -> int:
    return max((e.page for e in elements), default=0)


def _emit(on_stage: Optional[OnStage], label: str) -> None:
    if on_stage is not None:
        on_stage(label)
