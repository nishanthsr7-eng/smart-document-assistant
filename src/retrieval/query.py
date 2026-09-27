import re

from src.core.config import SETTINGS
from src.generation.client import Provider
from src.generation.prompts import (
    CONDENSE_EXPAND_SYSTEM,
    CONDENSE_SYSTEM,
    EXPANSION_SYSTEM,
    HYDE_SYSTEM,
    build_condense_prompt,
)


def condense(client: Provider, history: list[tuple[str, str]], question: str) -> str:
    recent = history[-SETTINGS.generation.history_turns :]
    completion = client.generate(CONDENSE_SYSTEM, build_condense_prompt(recent, question))
    return completion.text.strip() or question


def condense_and_expand(client: Provider, history: list[tuple[str, str]], question: str) -> tuple[str, list[str]]:
    """Resolve a follow-up into a standalone question and generate search variants in one LLM call."""
    recent = history[-SETTINGS.generation.history_turns :]
    completion = client.generate(CONDENSE_EXPAND_SYSTEM, build_condense_prompt(recent, question))
    standalone = question
    variants: list[str] = []
    seen = {question.lower()}
    for line in completion.text.splitlines():
        line = line.strip()
        if line.upper().startswith("STANDALONE:"):
            candidate = line.split(":", 1)[1].strip()
            if candidate:
                standalone = candidate
                seen.add(candidate.lower())
        elif line.upper().startswith("VARIANT:"):
            candidate = line.split(":", 1)[1].strip()
            key = candidate.lower()
            if candidate and key not in seen:
                seen.add(key)
                variants.append(candidate)
    return standalone, variants[: SETTINGS.retrieval.expansion_variants]


def expand_query(client: Provider, question: str) -> list[str]:
    completion = client.generate(EXPANSION_SYSTEM, f"Question: {question}")
    seen = {question.lower()}
    variants: list[str] = []
    for line in completion.text.splitlines():
        cleaned = line.strip().lstrip("-*0123456789. ").strip()
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            variants.append(cleaned)
    return [question] + variants[: SETTINGS.retrieval.expansion_variants]


def hyde_passage(client: Provider, question: str) -> str:
    return client.generate(HYDE_SYSTEM, f"Question: {question}").text.strip()


# An imperative aimed at the assistant, not at the documents. Matching one of these means the
# clause is an instruction to follow rather than an information need to retrieve: it is dropped
# from the search query, from the cross-encoder pair and from the generation prompt. Deliberately
# literal -- a fuzzy classifier here would drop genuine questions about policies and permissions.
_INSTRUCTION_RE = re.compile(
    r"""(?ix)
    \b(
      ignore \s+ (all \s+|any \s+)? (previous|prior|preceding|earlier|above) |
      disregard \s+ (all \s+|any \s+|the \s+)? (previous|prior|earlier|above|source|instruction) |
      forget \s+ (everything|all|your) |
      you \s+ are \s+ now |
      system \s+ override |
      developer \s+ mode |
      (reveal|print|output|repeat|show|echo) \s+ (me \s+)? (your|the) \s+
        (hidden \s+|initial \s+|full \s+|verbatim \s+)? (system \s+ prompt|instructions|rules) |
      (say|reply \s+ with|respond \s+ with|answer \s+ with|output) \s+ ["']? injection |
      act \s+ as \s+ (if|though|a\b)
    )\b
    """
)

# Clause boundaries a question actually splits on: sentence ends, a coordinated second question,
# and a leading attribution preamble ("According to the FMLA example timeline, ...").
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
# A coordinated *second question*, not any coordination: the right side has to open like a
# question, or "the official office hours for FAS, RMA and FSA" splits into nonsense.
_COORDINATION_SPLIT_RE = re.compile(
    r",\s+(?:and|or|but)\s+(?="
    r"(?:how|what|when|who|whom|where|why|which|whose|is|are|was|were|does|do|did|can|could|"
    r"may|might|must|should|will|would|has|have|had|if)\b)",
    re.IGNORECASE,
)
_PREAMBLE_RE = re.compile(
    r"^\s*(?:according to|based on|per|in|from|using|as (?:stated|described|shown) in)\b[^,?.]{0,80},\s*",
    re.IGNORECASE,
)


def sanitize(question: str) -> str:
    """Drop clauses that instruct the assistant, keeping the clauses that ask about documents.

    A question is data about an information need; an imperative embedded in one is an injection
    attempt. Removing it here means the rest of the pipeline -- embedding, the cross-encoder pair
    and the prompt -- never sees it, rather than relying on a refusal downstream. If every clause
    looks like an instruction there is nothing to keep, so the original is returned unchanged and
    the abstain gate handles it.
    """
    clauses = [c for c in _SENTENCE_SPLIT_RE.split(question.strip()) if c.strip()]
    kept = [c.strip() for c in clauses if not _INSTRUCTION_RE.search(c)]
    if not kept or len(kept) == len(clauses):
        return question.strip()
    return " ".join(kept)


def subqueries(question: str) -> list[str]:
    """Split a question into the information needs it actually contains, longest first.

    The cross-encoder scores one (query, passage) pair: a compound or preamble-laden question
    dilutes every pair, so a passage that fully answers one half scores low on the whole. Each
    part is scored separately and the passage keeps its best. Returns the whole question first,
    so a question that does not split costs exactly one pair per passage as before.
    """
    parts: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(question.strip()):
        sentence = sentence.strip()
        if not sentence:
            continue
        for clause in _COORDINATION_SPLIT_RE.split(sentence):
            clause = clause.strip(" ,")
            stripped = _PREAMBLE_RE.sub("", clause).strip()
            if len(stripped.split()) >= SETTINGS.retrieval.min_subquery_words:
                parts.append(stripped)

    out = [question.strip()]
    for part in parts:
        if part.lower() not in {p.lower() for p in out}:
            out.append(part)
    return out[: SETTINGS.retrieval.max_subqueries]
