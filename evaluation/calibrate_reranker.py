"""Fit the Platt coefficients behind `Reranker.calibrate` against the golden set.

The cross-encoder emits a sigmoid output, which is a score, not a probability: a passage scoring
0.95 is not right 95% of the time. `calibrated_top` feeds the confidence label the UI shows, so
an uncalibrated score means "High confidence" is a rank, not a claim about being correct.

This fits sigmoid(a * logit(raw) + b) on (top-1 raw score -> was the top-1 passage correct), and
reports Brier and expected calibration error before and after so the change carries a number
rather than a story. It reads the gate results rather than re-running retrieval: `--gate
retrieval` already records `top_score` and, since the golden set grew distractors, `hit_at_1`.

    python evaluation/run_eval.py --gate retrieval
    python evaluation/calibrate_reranker.py
"""

import argparse
import json
import math
from pathlib import Path

RESULTS = Path(__file__).parent / "results" / "gate_retrieval.json"


def load_results(path: Path) -> dict:
    """`--gate` nests the run under "results"; a plain `--mode` run writes it at the top level."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw.get("results", raw)


def samples(results: dict) -> list[tuple[float, float]]:
    """(top-1 raw score, was the top-1 passage correct).

    An unanswerable item has no correct passage, so its label is 0 whatever retrieval returned.
    Dropping those would fit the curve only on questions that have an answer and leave the
    confidence score free to be high on the ones that do not.
    """
    out = []
    for item in results["items"]:
        score = item.get("top_score")
        if score is None:
            continue
        out.append((float(score), float(item.get("hit_at_1", 0.0))))
    return out


def _logit(p: float, eps: float = 1e-6) -> float:
    p = min(max(p, eps), 1.0 - eps)
    return math.log(p / (1.0 - p))


def _sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z)) if z > -700 else 0.0


def fit(data: list[tuple[float, float]], iterations: int = 500, lr: float = 0.1) -> tuple[float, float]:
    """Newton-free logistic fit. 60 points and two parameters: gradient descent is enough, and
    it keeps this file free of a scipy dependency the rest of the repo does not carry."""
    xs = [_logit(s) for s, _ in data]
    ys = [y for _, y in data]
    a, b = 1.0, 0.0
    n = len(data)
    for _ in range(iterations):
        ga = gb = 0.0
        for x, y in zip(xs, ys, strict=True):
            error = _sigmoid(a * x + b) - y
            ga += error * x
            gb += error
        a -= lr * ga / n
        b -= lr * gb / n
    return a, b


def brier(data: list[tuple[float, float]], a: float, b: float) -> float:
    return sum((_sigmoid(a * _logit(s) + b) - y) ** 2 for s, y in data) / len(data)


def ece(data: list[tuple[float, float]], a: float, b: float, bins: int = 5) -> float:
    """Expected calibration error: mean gap between stated confidence and observed accuracy."""
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
    for s, y in data:
        p = _sigmoid(a * _logit(s) + b)
        buckets[min(int(p * bins), bins - 1)].append((p, y))
    total = 0.0
    for bucket in buckets:
        if not bucket:
            continue
        mean_p = sum(p for p, _ in bucket) / len(bucket)
        mean_y = sum(y for _, y in bucket) / len(bucket)
        total += len(bucket) / len(data) * abs(mean_p - mean_y)
    return total


def loo(data: list[tuple[float, float]]) -> tuple[float, float]:
    """Leave-one-out Brier and ECE. Two parameters fitted on 60 points is not badly overfitted,
    but reporting the in-sample number alone would still be quoting the fit back to itself."""
    predictions = []
    for index in range(len(data)):
        held = data[:index] + data[index + 1 :]
        a, b = fit(held)
        score, label = data[index]
        predictions.append((_sigmoid(a * _logit(score) + b), label))
    brier_loo = sum((p - y) ** 2 for p, y in predictions) / len(predictions)
    # Reuse the binning by handing ece() an identity transform over the held-out predictions.
    return brier_loo, ece([(p, y) for p, y in predictions], 1.0, 0.0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", type=Path, default=RESULTS)
    args = parser.parse_args()

    results = load_results(args.results)
    data = samples(results)
    positives = sum(y for _, y in data)
    print(f"{len(data)} items, {positives:.0f} with a correct top-1 passage")

    a, b = fit(data)
    print(f"\nidentity   a=1.000 b=0.000  brier={brier(data, 1.0, 0.0):.4f}  ece={ece(data, 1.0, 0.0):.4f}")
    print(f"fitted     a={a:.3f} b={b:.3f}  brier={brier(data, a, b):.4f}  ece={ece(data, a, b):.4f}")
    brier_loo, ece_loo = loo(data)
    print(f"fitted/LOO                 brier={brier_loo:.4f}  ece={ece_loo:.4f}")
    print(
        "\nSet in src/core/config.py RetrievalConfig:"
        f"\n    calibration_logistic_a: float = {a:.3f}"
        f"\n    calibration_logistic_b: float = {b:.3f}"
    )


if __name__ == "__main__":
    main()
