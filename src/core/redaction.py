import re

from src.core.config import SETTINGS

# Conservative on purpose. These patterns run over log messages, span attributes and the prompt
# captures shipped to Langfuse -- text that is read when something is already wrong -- so a
# pattern that eats an ordinary document number costs more than one that misses an exotic ID
# format. Anything broader belongs in a dedicated PII model, not in a regex on the log path.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")),
    # Bearer tokens and API keys that reached a log line by accident.
    ("SECRET", re.compile(r"\b(?:Bearer\s+|sk-|gsk_|AIza)[A-Za-z0-9._\-]{16,}")),
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # 13-19 digits in card-shaped groups. Luhn is not checked: this runs on a hot path, and a
    # false positive here is a masked number in a log line, not a failed request.
    ("CARD", re.compile(r"\b(?:\d[ -]?){12,18}\d\b")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
    # Last, and deliberately so: the phone shape is the loosest of these and would otherwise
    # swallow the left-hand end of an SSN or a card number before their own patterns ran.
    # International and NANP shapes, with separators. Requires 9+ digits so that years, amounts
    # and page counts survive.
    ("PHONE", re.compile(r"(?<![\w-])\+?\d[\d\s().-]{8,16}\d(?![\w-])")),
)


def scrub(text: str) -> str:
    """Mask personal data and credentials. A no-op when REDACT_PII is off."""
    if not SETTINGS.security.redact_pii or not text:
        return text
    for label, pattern in _PATTERNS:
        text = pattern.sub(f"[{label}]", text)
    return text


def scrub_value(value: object) -> object:
    """Mask inside a log event value, walking the containers structlog actually carries."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {key: scrub_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(scrub_value(item) for item in value)
    return value
