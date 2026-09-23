from functools import lru_cache

from src.core.config import SETTINGS


@lru_cache(maxsize=1)
def _tokenizer():
    from transformers import AutoTokenizer

    return AutoTokenizer.from_pretrained(SETTINGS.models.embedder_name)


def count_tokens(text: str) -> int:
    return len(_tokenizer().encode(text, add_special_tokens=False))
