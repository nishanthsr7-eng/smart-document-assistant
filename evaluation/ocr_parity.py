"""Does a scanned copy of a document retrieve as well as its text layer?

OCR turns an upload the service used to reject into one it can ingest, but whether that is worth
having is a retrieval question rather than a parsing one. This harness rasterises a sample PDF --
the copy has no text layer at all, so every character in it came out of OCR -- ingests both
copies into tenants of their own, and re-asks the golden-set questions whose answers live in that
document against each. The numbers are the same ones the gate uses, so an OCR'd corpus is
comparable to a native one instead of merely "working".
"""

import argparse
import io
import json
import time

from sqlalchemy.dialects.postgresql import insert

from evaluation.run_eval import (
    RESULTS_DIR,
    GoldenItem,
    _context_metrics,
    _mean,
    _retrieval_metrics,
    load_golden_set,
)
from src.auth.principal import Principal
from src.core.config import SETTINGS
from src.generation.answerer import answer_question
from src.generation.client import build_client
from src.ingestion.pipeline import ingest
from src.retrieval.embedder import Embedder
from src.retrieval.keyword_index import KeywordIndex
from src.retrieval.reranker import Reranker
from src.retrieval.vector_store import VectorStore
from src.storage.db import session
from src.storage.models import Tenant

# Two tenants, one document each, so both copies can carry the same filename: the golden set
# identifies an expected hit by filename and page, and a parity run has to compare like with like.
TEXT_TENANT = "00000000-0000-0000-0000-0000000000c1"
OCR_TENANT = "00000000-0000-0000-0000-0000000000c2"


def _principal(tenant_id: str) -> Principal:
    return Principal(
        user_id="00000000-0000-0000-0000-0000000000e1",
        tenant_id=tenant_id,
        email="ocr-parity@localhost",
        role="admin",
    )


def _ensure_tenant(tenant_id: str, name: str) -> None:
    with session() as sess:
        sess.execute(
            insert(Tenant)
            .values(tenant_id=tenant_id, name=name)
            .on_conflict_do_nothing(index_elements=[Tenant.tenant_id])
        )


def rasterize(data: bytes, scale: float) -> bytes:
    """Render every page to an image and wrap the images back into a PDF: a synthetic scan."""
    import pypdfium2

    document = pypdfium2.PdfDocument(io.BytesIO(data))
    images = [page.render(scale=scale).to_pil().convert("RGB") for page in document]
    document.close()
    buffer = io.BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:], resolution=72 * scale)
    return buffer.getvalue()


def _items_for(filename: str) -> list[GoldenItem]:
    return [
        item
        for item in load_golden_set()
        if item.expected and all(e.filename == filename for e in item.expected)
    ]


def _measure(
    items: list[GoldenItem], doc_id: str, principal: Principal, mode: str
) -> dict:
    embedder, store, keyword_index, reranker = Embedder(), VectorStore(), KeywordIndex(), Reranker()
    client = build_client()
    hit_at_k, mrr, recall, precision, answered = [], [], [], [], []
    per_item = []
    for item in items:
        answer = answer_question(
            item.question, [doc_id], embedder, store, client,
            keyword_index, reranker, principal, mode, generate=False,
        )
        retrieval = _retrieval_metrics(item, answer.hits)
        context = _context_metrics(item, answer)
        hit_at_k.append(retrieval["hit_at_k"])
        mrr.append(retrieval["mrr"])
        recall.append(context["context_recall"])
        if "context_precision" in context:
            precision.append(context["context_precision"])
        answered.append(0.0 if answer.status == "abstained" else 1.0)
        per_item.append(
            {
                "id": item.id,
                "type": item.type,
                "status": answer.status,
                "hit_at_k": retrieval["hit_at_k"],
                "context_recall": context["context_recall"],
            }
        )
    return {
        "hit_at_k": _mean(hit_at_k),
        "mrr": _mean(mrr),
        "context_recall": _mean(recall),
        "context_precision": _mean(precision),
        "answered_rate": _mean(answered),
        "items": per_item,
    }


def run(filename: str, scale: float, mode: str) -> dict:
    path = SETTINGS.paths.sample_docs / filename
    original = path.read_bytes()
    items = _items_for(filename)
    embedder = Embedder()

    _ensure_tenant(TEXT_TENANT, "ocr-parity-text")
    _ensure_tenant(OCR_TENANT, "ocr-parity-ocr")

    text_report = ingest(filename, original, embedder, _principal(TEXT_TENANT))

    scanned = rasterize(original, scale)
    started = time.perf_counter()
    ocr_report = ingest(filename, scanned, embedder, _principal(OCR_TENANT))
    ocr_seconds = time.perf_counter() - started

    return {
        "filename": filename,
        "mode": mode,
        "num_items": len(items),
        "raster_scale": scale,
        "pages": ocr_report.pages,
        "ocr_ingest_seconds": round(ocr_seconds, 1),
        "chunks": {"text_layer": text_report.num_children, "ocr": ocr_report.num_children},
        "text_layer": _measure(items, text_report.doc_id, _principal(TEXT_TENANT), mode),
        "ocr": _measure(items, ocr_report.doc_id, _principal(OCR_TENANT), mode),
    }


_METRICS = ("hit_at_k", "mrr", "context_recall", "context_precision", "answered_rate")


def _markdown(results: dict) -> str:
    lines = [
        f"# OCR parity — {results['filename']}",
        "",
        f"{results['num_items']} golden-set items answered only by this document, mode "
        f"`{results['mode']}`. The OCR copy is the same document rasterised at "
        f"{results['raster_scale']}x with no text layer, {results['pages']} pages, ingested in "
        f"{results['ocr_ingest_seconds']}s ({results['chunks']['ocr']} chunks against "
        f"{results['chunks']['text_layer']} from the text layer).",
        "",
        "| Metric | Text layer | OCR |",
        "|---|---|---|",
    ]
    for metric in _METRICS:
        text_value, ocr_value = results["text_layer"][metric], results["ocr"][metric]
        lines.append(
            f"| {metric} | {_format(text_value)} | {_format(ocr_value)} |"
        )
    lines += ["", "| Item | Type | Text layer hit@k | OCR hit@k | OCR status |", "|---|---|---|---|---|"]
    by_id = {item["id"]: item for item in results["ocr"]["items"]}
    for item in results["text_layer"]["items"]:
        ocr_item = by_id[item["id"]]
        lines.append(
            f"| {item['id']} | {item['type']} | {_format(item['hit_at_k'])} | "
            f"{_format(ocr_item['hit_at_k'])} | {ocr_item['status']} |"
        )
    return "\n".join(lines) + "\n"


def _format(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare retrieval against a scanned copy of a document.")
    parser.add_argument("--filename", default="leave_policy.pdf")
    parser.add_argument("--scale", type=float, default=2.0, help="Rasterisation scale; 2.0 is ~144 DPI.")
    parser.add_argument("--mode", choices=SETTINGS.retrieval.modes, default=SETTINGS.retrieval.mode)
    args = parser.parse_args()

    results = run(args.filename, args.scale, args.mode)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (RESULTS_DIR / "ocr_parity.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    report = _markdown(results)
    (RESULTS_DIR / "ocr_parity.md").write_text(report, encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
