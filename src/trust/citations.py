import re
from dataclasses import dataclass

from src.core.config import SETTINGS
from src.retrieval.reranker import Reranker

_NUMBER_RE = re.compile(r"\d[\d,.]*%?")

_WORD_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
}
_WORD_RE = re.compile(r"\b(" + "|".join(_WORD_NUMBERS) + r")\b", re.IGNORECASE)


@dataclass
class CitationCheck:
    status: str


@dataclass
class ValidatedConflict:
    claim: str
    source_ids: list[int]


def validate_batch(sentences: list, source_texts: dict[int, str], reranker: Reranker) -> list[CitationCheck]:
    results: list[CitationCheck] = [CitationCheck("unsupported") for _ in sentences]

    pairs: list[tuple[str, str]] = []
    owners: list[int] = []
    for idx, sent in enumerate(sentences):
        if not sent.cites or not set(sent.cites) <= source_texts.keys():
            continue
        cited_text = " ".join(source_texts[c] for c in sent.cites)
        if _grounded(sent.text, cited_text) < SETTINGS.trust.grounding_threshold:
            continue
        for src_id in sent.cites:
            pairs.append((sent.text, source_texts[src_id]))
            owners.append(idx)

    if not pairs:
        return results

    scores = reranker.score(pairs)
    best: dict[int, float] = {}
    for idx, score in zip(owners, scores):
        best[idx] = max(best.get(idx, -1e9), score)

    for idx, score in best.items():
        status = "supported" if score >= SETTINGS.trust.support_threshold else "weak"
        results[idx] = CitationCheck(status)

    return results


def detect_conflicts(sentences: list[tuple[str, list[int]]], source_docs: dict[int, str]) -> list[ValidatedConflict]:
    conflicts = []

    for i in range(len(sentences)):
        text_a, cites_a = sentences[i]
        docs_a = {source_docs[c] for c in cites_a if c in source_docs}
        numbers_a = set(_NUMBER_RE.findall(text_a))
        if not docs_a:
            continue
        for j in range(i + 1, len(sentences)):
            text_b, cites_b = sentences[j]
            docs_b = {source_docs[c] for c in cites_b if c in source_docs}
            if not docs_b or docs_a == docs_b:
                continue
            numbers_b = set(_NUMBER_RE.findall(text_b))
            if numbers_a and numbers_b and numbers_a != numbers_b and _same_claim_shape(text_a, text_b):
                ids = sorted(set(cites_a) | set(cites_b))
                conflicts.append(ValidatedConflict("Sources disagree on this value.", ids))

    return conflicts


def _same_claim_shape(a: str, b: str) -> bool:
    strip = lambda t: set(_NUMBER_RE.sub("#", t).lower().split())
    words_a, words_b = strip(a), strip(b)
    if not words_a or not words_b:
        return False
    return len(words_a & words_b) / max(len(words_a), len(words_b)) >= 0.5


def _grounded(text: str, cited_text: str) -> float:
    # Fraction of numeric claims in the answer that appear in the cited source.
    # Paraphrase of surrounding prose is fine (semantic support is scored by the cross-encoder).
    claims = [_normalize_number(n) for n in _NUMBER_RE.findall(text)]
    claims += [str(_WORD_NUMBERS[w.lower()]) for w in _WORD_RE.findall(text)]
    if not claims:
        return 1.0
    cited_norm = _normalize_number(cited_text)
    matched = sum(1 for c in claims if c and c in cited_norm)
    return matched / len(claims)


def _normalize_number(text: str) -> str:
    # Drop thousands separators and trailing decimal zeros so "4,000" == "4000" and "8.0" == "8".
    def _fmt(match: re.Match) -> str:
        raw = match.group(0).replace(",", "")
        if "." in raw:
            raw = raw.rstrip("0").rstrip(".")
        return raw

    return _NUMBER_RE.sub(_fmt, text)
