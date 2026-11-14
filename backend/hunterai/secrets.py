"""Keep HunterAI's own key values out of anything stored, published, or sent to a tool."""
from __future__ import annotations

from . import config

KEY_NAMES = ("ABLIT_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY",
             "GEMINI_API_KEY")
MIN_LEN = 8          # shorter values would redact ordinary words
REDACTED = "[redacted]"


def values() -> list[str]:
    found = (config.env(n) for n in KEY_NAMES)
    return sorted({v for v in found if v and len(v) >= MIN_LEN}, key=len, reverse=True)


def scrub(text: str) -> str:
    for v in values():
        text = text.replace(v, REDACTED)
    return text


def scrub_obj(value):
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {scrub_obj(k): scrub_obj(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_obj(v) for v in value]
    return value
