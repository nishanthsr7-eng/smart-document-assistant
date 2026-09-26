import argparse
import json
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import SETTINGS  # noqa: E402
from src.generation.answerer import Answer, answer_question  # noqa: E402
from src.generation.client import build_client  # noqa: E402
from src.ingestion.pipeline import ingest  # noqa: E402
from src.retrieval.embedder import Embedder  # noqa: E402
from src.retrieval.keyword_index import KeywordIndex  # noqa: E402
from src.retrieval.reranker import Reranker  # noqa: E402
from src.retrieval.vector_store import Hit, VectorStore  # noqa: E402

GOLDEN_SET = Path(__file__).parent / "golden_set.yaml"
RESULTS_DIR = Path(__file__).parent / "results"
_NUMBER_RE = re.compile(r"\d[\d,.]*")


@dataclass
class ExpectedHit:
    filename: str
    page: int
    must_contain: str


@dataclass
class GoldenItem:
    id: str
    question: str
    answerable: bool
    type: str
    expected: list[ExpectedHit]


def load_golden_set() -> list[GoldenItem]:
    raw = yaml.safe_load(GOLDEN_SET.read_text(encoding="utf-8"))
    return [
        GoldenItem(
            id=entry["id"],
            question=entry["question"],
            answerable=entry["answerable"],
            type=entry["type"],
            expected=[ExpectedHit(**e) for e in entry["expected"]],
        )
        for entry in raw
    ]


def ingest_sample_docs(embedder: Embedder) -> list[str]:
    doc_ids = []
    for path in sorted(SETTINGS.paths.sample_docs.iterdir()):
        report = ingest(path.name, path.read_bytes(), embedder)
        doc_ids.append(report.doc_id)
    return doc_ids


def _parse_pages(pages: str) -> tuple[int, int]:
    if "-" in pages:
        start, end = pages.split("-", 1)
        return int(start), int(end)
    return int(pages), int(pages)


def _hit_matches(filename: str, page_start: int, page_end: int, expected: ExpectedHit) -> bool:
    return filename == expected.filename and page_start <= expected.page <= page_end


def _retrieval_metrics(item: GoldenItem, hits: list[Hit]) -> dict:
    if not item.expected:
        return {}
    first_rank = None
    covered = set()
    for rank, hit in enumerate(hits, start=1):
        for expected in item.expected:
            if _hit_matches(hit.filename, hit.page_start, hit.page_end, expected):
                covered.add(id(expected))
                if first_rank is None:
                    first_rank = rank
    return {
        "hit_at_k": 1.0 if covered else 0.0,
        "mrr": 1.0 / first_rank if first_rank else 0.0,
    }


def _context_metrics(item: GoldenItem, answer: Answer) -> dict:
    if not item.expected:
        return {}
    source_ranges = [(s.filename, *_parse_pages(s.pages)) for s in answer.sources]
    matched_expected = sum(
        1
        for expected in item.expected
        if any(_hit_matches(filename, start, end, expected) for filename, start, end in source_ranges)
    )
    metrics = {"context_recall": matched_expected / len(item.expected)}
    if source_ranges:
        matched_sources = sum(
            1
            for filename, start, end in source_ranges
            if any(_hit_matches(filename, start, end, expected) for expected in item.expected)
        )
        metrics["context_precision"] = matched_sources / len(source_ranges)
    return metrics


def _must_contain_pass(item: GoldenItem, answer: Answer) -> float:
    if not item.expected:
        return None
    answer_text = " ".join(s.text for s in answer.sentences).lower()
    return 1.0 if all(e.must_contain.lower() in answer_text for e in item.expected) else 0.0


def _citation_validity(answer: Answer) -> list[float]:
    valid_ids = set(range(1, len(answer.sources) + 1))
    return [1.0 if s.cites and set(s.cites) <= valid_ids else 0.0 for s in answer.sentences]


def _numeric_grounding(answer: Answer) -> list[float]:
    source_text = {s.id: s.text for s in answer.sources}
    results = []
    for sentence in answer.sentences:
        numbers = _NUMBER_RE.findall(sentence.text)
        if not numbers:
            continue
        cited_text = " ".join(source_text.get(c, "") for c in sentence.cites)
        results.append(1.0 if all(n in cited_text for n in numbers) else 0.0)
    return results


def _faithfulness(answer: Answer, reranker: Reranker) -> float | None:
    """Fraction of answer sentences supported by at least one source.
    Uses the reranker to score each sentence against all source texts.
    Sentences without factual claims (no nouns/verbs) are skipped.
    """
    if not answer.sentences or not answer.sources:
        return None
    all_source_text = [s.text for s in answer.sources]
    supported = 0
    total = 0
    for sent in answer.sentences:
        words = sent.text.split()
        if len(words) < 4:
            continue
        total += 1
        pairs = [(sent.text, src) for src in all_source_text]
        scores = reranker.score(pairs)
        if scores and max(scores) >= SETTINGS.trust.support_threshold:
            supported += 1
    return supported / total if total > 0 else None


def _top_score(answer: Answer) -> float | None:
    for stage in answer.trace.stages:
        if stage.name == "Reranking":
            return stage.payload.get("top_score")
    if answer.hits:
        return answer.hits[0].score
    return None


def sweep_abstain_threshold(mode: str) -> list[dict]:
    embedder = Embedder()
    store = VectorStore()
    client = build_client()
    doc_ids = ingest_sample_docs(embedder)
    keyword_index = KeywordIndex() if mode != "dense" else None
    reranker = Reranker()

    scored = []
    for item in load_golden_set():
        answer = answer_question(
            item.question, doc_ids, embedder, store, client, keyword_index, reranker, mode, generate=False
        )
        scored.append((item, _top_score(answer)))

    curve = []
    for step in range(21):
        threshold = step / 20
        tp = fp = tn = fn = 0
        for item, score in scored:
            should_abstain = not item.answerable
            predicted_abstain = score is None or score < threshold
            if should_abstain and predicted_abstain:
                tp += 1
            elif should_abstain and not predicted_abstain:
                fn += 1
            elif not should_abstain and predicted_abstain:
                fp += 1
            else:
                tn += 1
        sensitivity = tp / (tp + fn) if (tp + fn) else 1.0
        specificity = tn / (tn + fp) if (tn + fp) else 1.0
        curve.append(
            {
                "threshold": threshold,
                "balanced_accuracy": (sensitivity + specificity) / 2,
                "tp": tp,
                "fp": fp,
                "tn": tn,
                "fn": fn,
            }
        )
    return curve


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(len(ordered) * pct))
    return ordered[index]


def run(mode: str, generate: bool) -> dict:
    embedder = Embedder()
    store = VectorStore()
    client = build_client()
    doc_ids = ingest_sample_docs(embedder)
    keyword_index = KeywordIndex() if mode != "dense" else None
    reranker = Reranker()

    items = load_golden_set()
    per_item = []
    stage_durations: dict[str, list[float]] = {}
    hit_at_k, mrr = [], []
    context_recall, context_precision = [], []
    must_contain = []
    citation_validity = []
    numeric_grounding = []
    faithfulness = []
    tp = fp = fn = 0
    answerable_total = 0

    for item in items:
        answer = answer_question(
            item.question,
            doc_ids,
            embedder,
            store,
            client,
            keyword_index,
            reranker,
            mode,
            generate=generate,
        )
        for stage in answer.trace.stages:
            stage_durations.setdefault(stage.name.split(" ")[0], []).append(stage.duration_s)

        should_abstain = not item.answerable
        abstained = answer.status == "abstained"
        if not should_abstain:
            answerable_total += 1
        if should_abstain and abstained:
            tp += 1
        elif should_abstain and not abstained:
            fn += 1
        elif not should_abstain and abstained:
            fp += 1

        retrieval = _retrieval_metrics(item, answer.hits)
        context = _context_metrics(item, answer)
        if "hit_at_k" in retrieval:
            hit_at_k.append(retrieval["hit_at_k"])
            mrr.append(retrieval["mrr"])
        if "context_recall" in context:
            context_recall.append(context["context_recall"])
        if "context_precision" in context:
            context_precision.append(context["context_precision"])

        record = {
            "id": item.id,
            "type": item.type,
            "status": answer.status,
            "abstain_reason": answer.abstain_reason,
            "top_score": _top_score(answer),
            **retrieval,
            **context,
        }

        if generate and answer.status == "answered":
            pass_rate = _must_contain_pass(item, answer)
            if pass_rate is not None:
                must_contain.append(pass_rate)
                record["must_contain"] = pass_rate
            citation_validity.extend(_citation_validity(answer))
            numeric_grounding.extend(_numeric_grounding(answer))
            faith = _faithfulness(answer, reranker)
            if faith is not None:
                faithfulness.append(faith)
                record["faithfulness"] = faith

        per_item.append(record)

    refusal_precision = tp / (tp + fp) if (tp + fp) else None
    refusal_recall = tp / (tp + fn) if (tp + fn) else None
    false_refusal_rate = fp / answerable_total if answerable_total else None

    results = {
        "mode": mode,
        "generate": generate,
        "num_items": len(items),
        "retrieval": {
            "hit_at_k": _mean(hit_at_k),
            "mrr": _mean(mrr),
            "context_recall": _mean(context_recall),
            "context_precision": _mean(context_precision),
        },
        "abstention": {
            "refusal_precision": refusal_precision,
            "refusal_recall": refusal_recall,
            "false_refusal_rate": false_refusal_rate,
        },
        "generation": {
            "must_contain_accuracy": _mean(must_contain),
            "citation_validity": _mean(citation_validity),
            "numeric_grounding_pass_rate": _mean(numeric_grounding),
            "faithfulness": _mean(faithfulness),
        },
        "latency_s": {
            stage: {"p50": _percentile(durations, 0.5), "p95": _percentile(durations, 0.95)}
            for stage, durations in stage_durations.items()
        },
        "items": per_item,
    }
    return results


def _write_markdown(results: dict, path: Path) -> None:
    lines = [f"# Evaluation results — mode: {results['mode']}", ""]
    lines.append(f"Items: {results['num_items']} (generate={results['generate']})")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    for group in ("retrieval", "abstention", "generation"):
        for key, value in results[group].items():
            display = f"{value:.3f}" if isinstance(value, float) else "n/a"
            lines.append(f"| {group}.{key} | {display} |")
    lines.append("")
    lines.append("| Stage | p50 (s) | p95 (s) |")
    lines.append("|---|---|---|")
    for stage, values in results["latency_s"].items():
        p50 = f"{values['p50']:.3f}" if values["p50"] is not None else "n/a"
        p95 = f"{values['p95']:.3f}" if values["p95"] is not None else "n/a"
        lines.append(f"| {stage} | {p50} | {p95} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_comparison(results: list[dict], path: Path) -> None:
    modes = [r["mode"] for r in results]
    lines = ["# Ablation: retrieval mode comparison", ""]
    lines.append("| Metric | " + " | ".join(modes) + " |")
    lines.append("|---|" + "|".join("---" for _ in modes) + "|")
    for group in ("retrieval", "abstention", "generation"):
        for key in results[0][group]:
            cells = []
            for r in results:
                value = r[group][key]
                cells.append(f"{value:.3f}" if isinstance(value, float) else "n/a")
            lines.append(f"| {group}.{key} | " + " | ".join(cells) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def compare(generate: bool) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for mode in ("dense", "hybrid", "hybrid_rerank"):
        result = run(mode, generate=generate)
        (RESULTS_DIR / f"{mode}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        results.append(result)
    path = RESULTS_DIR / "comparison.md"
    _write_comparison(results, path)
    print(f"Wrote {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["dense", "hybrid", "hybrid_rerank"], default="dense")
    parser.add_argument("--no-generate", action="store_true")
    parser.add_argument("--sweep-abstain", action="store_true")
    parser.add_argument("--compare", action="store_true")
    args = parser.parse_args()

    if args.compare:
        compare(generate=not args.no_generate)
        return

    if args.sweep_abstain:
        curve = sweep_abstain_threshold(args.mode)
        best = max(curve, key=lambda c: c["balanced_accuracy"])
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        path = RESULTS_DIR / "abstain_sweep.json"
        path.write_text(json.dumps({"mode": args.mode, "curve": curve, "best": best}, indent=2), encoding="utf-8")
        print(f"Wrote {path}; best threshold={best['threshold']} balanced_accuracy={best['balanced_accuracy']:.3f}")
        return

    results = run(args.mode, generate=not args.no_generate)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    json_path = RESULTS_DIR / f"{args.mode}.json"
    json_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    _write_markdown(results, RESULTS_DIR / f"{args.mode}.md")
    print(f"Wrote {json_path}")


if __name__ == "__main__":
    main()
