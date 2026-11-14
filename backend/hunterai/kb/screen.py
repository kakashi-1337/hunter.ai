"""Two checks on text before it is kept or shown to a model.

`redact` masks strings shaped like credentials before a document is chunked: the index is
plain text on disk, and every retrieved passage is sent to the model provider. `flags` marks
text shaped like instructions to a model, so a retrieved passage that tries to give orders
is labelled as what it is. Both are pattern checks: they catch known shapes, not everything."""
from __future__ import annotations

import re

_WHOLE = (
    ("private-key", re.compile(r"-----BEGIN [A-Z ]{0,30}PRIVATE KEY-----[^-]{0,20000}-----END [A-Z ]{0,30}PRIVATE KEY-----")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{50,}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b")),
    ("openai-key", re.compile(r"\bsk-(?=[A-Za-z_-]{0,200}[0-9])[A-Za-z0-9_-]{20,200}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("stripe-key", re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9A-Za-z]{16,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer-token", re.compile(r"(?i)\b(?:bearer|basic)[ \t]{1,4}(?=[A-Za-z._~+/=-]{0,500}[0-9])[A-Za-z0-9._~+/=-]{16,500}")),
)
# name = value, where the name says secret (DB_PASSWORD, "password", api-key) and the value looks
# like one and not like code: no dots, one digit at least, not the start of a call or a lookup,
# and on the same line. Every repeat is bounded: documents can be many megabytes of anything.
_ASSIGN = re.compile(
    r"""(?ix)
    (?<![A-Za-z0-9_-]) [A-Za-z0-9_-]{0,40}?
    (?:api[_-]?key|apikey|secret|password|passwd|pwd|token|access[_-]?key|private[_-]?key|passphrase)
    [A-Za-z0-9_-]{0,40} ["']?
    [ \t]{0,8} (?<![=!<>:]) [:=] (?![=]) [ \t]{0,8} (?P<q>["']?)
    (?P<v>(?=[A-Za-z_~+/!@#$%^&*-]{0,200}[0-9])[A-Za-z0-9_~+/!@#$%^&*-][A-Za-z0-9_~+/=!@#$%^&*-]{9,200})
    (?P=q) (?![\w.(\[])
    """)
_DB_URL = re.compile(r"(?i)(?<![a-z0-9+.-])(?P<scheme>[a-z][a-z0-9+.-]{1,20}://[^:\s/@]{1,255}):(?P<v>[^@\s/]{6,200})@")

_INJECTION = re.compile(
    r"""(?ix)
      ignore\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?)
    | disregard\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above)\s+(?:instructions?|prompts?|rules?)
    | (?:reveal|print|show|repeat|output)\s+(?:me\s+)?(?:your|the)\s+(?:system\s+)?(?:prompt|instructions)
    | you\s+are\s+now\s+(?:a|an|the)\s
    | new\s+(?:system\s+)?instructions?\s*:
    | <\|(?:im_start|system|endoftext)\|>
    | \[\s*(?:system|assistant)\s*\]
    | send\s+(?:this|the|all)\s+.{0,40}\bto\s+https?://
    """)


def redact(text: str) -> tuple[str, dict[str, int]]:
    """(text with credential-shaped strings masked, {kind: count}). The number of lines does
    not change, so line references into the text stay right."""
    counts: dict[str, int] = {}

    def note(kind: str) -> None:
        counts[kind] = counts.get(kind, 0) + 1

    for kind, pattern in _WHOLE:
        def whole(m: re.Match, kind=kind) -> str:
            note(kind)
            return f"[REDACTED:{kind}]" + "\n" * m.group(0).count("\n")
        text = pattern.sub(whole, text)

    def value(kind: str):
        def repl(m: re.Match) -> str:
            note(kind)
            start, end = m.start("v") - m.start(0), m.end("v") - m.start(0)
            return m.group(0)[:start] + f"[REDACTED:{kind}]" + m.group(0)[end:]
        return repl

    text = _ASSIGN.sub(value("secret"), text)
    text = _DB_URL.sub(value("db-password"), text)
    return text, counts


def flags(text: str) -> list[str]:
    return ["instructions"] if _INJECTION.search(text) else []
