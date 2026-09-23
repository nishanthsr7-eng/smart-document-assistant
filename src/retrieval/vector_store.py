import json
from dataclasses import dataclass

import chromadb

from src.core.config import SETTINGS
from src.ingestion.chunker import ChildChunk

_COLLECTION = "chunks"


@dataclass
class Hit:
    chunk_id: str
    parent_id: str
    doc_id: str
    filename: str
    text: str
    page_start: int
    page_end: int
    section_path: tuple[str, ...]
    kind: str
    char_span_in_parent: tuple[int, int]
    score: float


class VectorStore:
    def __init__(self) -> None:
        client = chromadb.PersistentClient(path=str(SETTINGS.paths.chroma))
        self._collection = client.get_or_create_collection(
            _COLLECTION, metadata={"hnsw:space": "cosine"}
        )

    def has_doc(self, doc_id: str) -> bool:
        return bool(self._collection.get(where={"doc_id": doc_id}, limit=1)["ids"])

    def add(self, children: list[ChildChunk], embeddings: list[list[float]]) -> None:
        self._collection.add(
            ids=[c.chunk_id for c in children],
            embeddings=embeddings,
            documents=[c.text for c in children],
            metadatas=[_metadata(c) for c in children],
        )

    def query(self, vector: list[float], k: int, doc_ids: list[str]) -> list[Hit]:
        if not doc_ids:
            return []
        where = {"doc_id": {"$in": doc_ids}}
        res = self._collection.query(query_embeddings=[vector], n_results=k, where=where)
        hits = []
        for chunk_id, doc, meta, dist in zip(
            res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0]
        ):
            hits.append(_hit(chunk_id, doc, meta, 1.0 - dist))
        return hits

    def get_embeddings(self, chunk_ids: list[str]) -> dict[str, list[float]]:
        if not chunk_ids:
            return {}
        res = self._collection.get(ids=chunk_ids, include=["embeddings"])
        return dict(zip(res["ids"], res["embeddings"]))

    def delete_doc(self, doc_id: str) -> None:
        self._collection.delete(where={"doc_id": doc_id})

    def all_chunks(self) -> list[Hit]:
        res = self._collection.get()
        return [
            _hit(chunk_id, doc, meta, 0.0)
            for chunk_id, doc, meta in zip(res["ids"], res["documents"], res["metadatas"])
        ]


def _metadata(c: ChildChunk) -> dict:
    return {
        "parent_id": c.parent_id,
        "doc_id": c.doc_id,
        "filename": c.filename,
        "page_start": c.page_start,
        "page_end": c.page_end,
        "section_path": json.dumps(c.section_path),
        "kind": c.kind,
        "char_span_in_parent": json.dumps(c.char_span_in_parent),
    }


def _hit(chunk_id: str, text: str, meta: dict, score: float) -> Hit:
    return Hit(
        chunk_id=chunk_id,
        parent_id=meta["parent_id"],
        doc_id=meta["doc_id"],
        filename=meta["filename"],
        text=text,
        page_start=meta["page_start"],
        page_end=meta["page_end"],
        section_path=tuple(json.loads(meta["section_path"])),
        kind=meta["kind"],
        char_span_in_parent=tuple(json.loads(meta["char_span_in_parent"])),
        score=score,
    )
