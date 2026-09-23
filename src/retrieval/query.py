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
