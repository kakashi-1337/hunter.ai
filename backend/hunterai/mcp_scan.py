"""The MCP scanner: a look at what a running server tells the model. The checker
(mcp_check) reads a server's code before it is installed; this reads the tool names,
descriptions, and argument schemas a server sends once it runs, because that text goes
into every request and a server can change it at any time.

Two jobs. Rules over the text find the known shapes of a poisoned tool (hidden
instructions, invisible characters, requests for secrets, mentions of other tools).
Pins remember what the owner has accepted: a tool whose definition is new or has changed
on a known server is held back, not offered to the model, until the owner approves it.

Clean is not safe: the rules catch known shapes. A server can also misbehave in what its
tools do and return, which no reading of descriptions shows."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import unicodedata

from . import config

EXCERPT = 240
LONG_TEXT = 3000
MAX_TEXT = 20_000           # characters of one string that are read; more than that cannot be shown to the owner
MAX_NODES = 5000            # schema keys and values looked at for one tool
MAX_FINDINGS = 40           # per tool
WALK_DEPTH = 32
SHOWN_CHARS = 60_000        # of a description or a schema kept in a report row

# Orders that have no place in a description of what a tool does.
_HIDDEN = re.compile(
    r"""(?ix)
      \bignore\s+(?:all\s+|any\s+)?(?:the\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?)
    | \b(?:overrides?|supersedes?|replaces?)\s+(?:the\s+|any\s+|all\s+)?(?:previous|prior|other|system)\s+(?:instructions?|rules?|prompts?)
    | \bdo\s+not\s+(?:tell|mention|inform|reveal|show|disclose)\b[^.\n]{0,40}\buser
    | \bwithout\s+(?:telling|informing|asking|notifying)\s+the\s+user
    | \b(?:hide|conceal|keep)\s+(?:this|it|that)\s+(?:secret\s+)?from\s+the\s+user
    | \buser\s+(?:will|would|must|should)\s+not\s+(?:see|know|notice)
    | <\s{0,3}/?\s{0,3}(?:important|system|secret|instructions?|admin)\s{0,3}>
    | \[\s{0,3}(?:system|assistant)\s{0,3}\] | <\|(?:im_start|system|endoftext)\|>
    """)
# Normal in many honest servers ("you must call resolve first"), so worth a look, not a hold.
_IMPERATIVE = re.compile(
    r"""(?ix)
      \b(?:before|prior\s+to)\s+(?:using|calling|invoking|running)\s+(?:any|this|the|all)\s+(?:other\s+)?tools?
    | \b(?:you|the\s+assistant|the\s+model|the\s+ai|the\s+agent)\s+must\s+(?:always|first|never|run|call|use)
    | \b(?:always|first)\s+(?:call|use|invoke|run)\s+this\s+tool
    """)
_KEY_FILES = re.compile(
    r"""(?ix)
      \.ssh\b | \bid_(?:rsa|ed25519|ecdsa)\b | \.aws/credentials | \.netrc\b | \.git-credentials | /etc/(?:passwd|shadow)\b
    | (?:~|\$HOME|/home/[^/\s]{1,64})/\.(?:cursor|claude|hunterai|kube|docker|gnupg)\b | \bmcp\.json\b
    """)
_VERB = r"\b(?:read|include|pass|send|attach|provide|append|copy|forward|upload)\b[^.\n]{0,60}"
_ASKS_HIGH = re.compile(rf"(?ix){_VERB}\b(?:environment\s+variables?|ssh\s+keys?|private\s+keys?|system\s+prompt|chat\s+history|"
                        r"conversation\s+history|(?:entire|full|whole|previous)\s+conversation)\b")
_ASKS = re.compile(rf"(?ix){_VERB}\b(?:credentials?|api\s+keys?|access\s+tokens?|passwords?|cookies?)\b|\.env\b")
_EXFIL = re.compile(r"(?i)(pastebin\.com|discord(app)?\.com/api/webhooks|hooks\.slack\.com|webhook\.site|requestbin|"
                    r"ngrok(-free)?\.(io|app)|pipedream\.net|interact\.sh|oast\.(fun|live|pro)|burpcollaborator|"
                    r"https?://(?!127\.|10\.|192\.168\.)(\d{1,3}\.){3}\d{1,3})")
_URL = re.compile(r"(?i)\bhttps?://[^\s\"'<>)]{1,500}")
_INSTEAD = re.compile(r"(?i)\b(?:instead\s+of|rather\s+than|in\s+place\s+of|replaces?)\s+(?:the\s+)?[\w.-]{1,64}\s+tool\b")
_BLOB = re.compile(r"[A-Za-z0-9+/]{120,}={0,2}")
_PARAM = re.compile(r"(?i)^(side_?note|notes?_for_(?:ai|model|assistant|llm)|conversation(?:_history)?|chat_history|"
                    r"system_prompt|credentials?|ssh_key|private_key|all_env|environment_variables?)$")
_MIXED = re.compile(r"(?=[^\W\d_]*[A-Za-z])(?=[^\W\d_]*[Ͱ-ϿЀ-ӿ])[^\W\d_]{2,}")
# Latin look-alikes among Cyrillic and Greek letters, folded before the rules read the text.
_FOLD = str.maketrans("аеорсхуіјѕԁһոоΑΒΕΖΗΙΚΜΝΟΡΤΥΧανορ", "aeopcxyijsdhnoABEZHIKMNOPTYXavop")

TITLES = {
    "hidden-instruction": ("high", "Text that gives the model orders behind the user's back"),
    "invisible-text": ("high", "Invisible, direction-changing, or control characters"),
    "mixed-script": ("high", "A word that mixes Latin letters with look-alike letters of another alphabet"),
    "sensitive-data": ("high", "Asks for key files, the environment, the system prompt, or the conversation"),
    "exfil-host": ("high", "Names a paste, webhook, or tunnel service, or a bare IP address"),
    "shadowing": ("high", "Talks about another tool, which can change how the model uses that tool"),
    "unreadable-definition": ("high", "Too long, too deep, or too large to read in full, so it was not"),
    "imperative": ("medium", "Tells the model what it must do (common in honest servers; read it)"),
    "asks-for-secret": ("medium", "Mentions passing a key, token, password, or cookie, or a .env file"),
    "suspicious-parameter": ("medium", "An argument that asks the model for side information"),
    "encoded-blob": ("medium", "A long encoded string in text meant to be read"),
    "url": ("info", "Mentions a web address"),
    "long-text": ("info", "Very long text for one tool"),
}


def _hidden_char(c: str) -> bool:
    """A character a reader does not see, or that changes what a reader sees."""
    o = ord(c)
    if c in "\n\t" or o == 0xFE0F:                 # FE0F only chooses the emoji form of the sign before it
        return False
    cat = unicodedata.category(c)
    return (cat in ("Cc", "Cf", "Zl", "Zp") or 0xFE00 <= o <= 0xFE0E or 0xE0100 <= o <= 0xE01EF
            or o in (0x3164, 0x115F, 0x1160, 0xFFA0, 0x180E))


def _finding(rule: str, where: str, text: str, at: int = 0, severity: str | None = None) -> dict:
    start = max(0, at - 60)
    excerpt = text[start:start + EXCERPT]
    # Shown to the owner: make hidden characters visible instead of letting them hide again.
    excerpt = "".join(f"\\u{ord(c):04x}" if _hidden_char(c) or (not c.isprintable() and c not in "\n\t") else c for c in excerpt)
    return {"rule": rule, "severity": severity or TITLES[rule][0], "title": TITLES[rule][1], "where": where,
            "excerpt": excerpt}


def _scan_text(raw: str, where: str, others: set[str]) -> list[dict]:
    out: list[dict] = []
    if len(raw) > MAX_TEXT:
        out.append(_finding("unreadable-definition", where, raw[:EXCERPT]))
        raw = raw[:MAX_TEXT]
    raw = raw.replace("\r\n", "\n")
    at = next((i for i, c in enumerate(raw) if _hidden_char(c)), None)
    if at is not None:
        out.append(_finding("invisible-text", where, raw, at))
    m = _MIXED.search(raw)
    if m:
        out.append(_finding("mixed-script", where, raw, m.start()))
    # The rules read what a model would: compatibility forms folded, hidden characters gone.
    text = "".join(c for c in unicodedata.normalize("NFKC", raw) if not _hidden_char(c)).translate(_FOLD)
    for rule, pattern in (("hidden-instruction", _HIDDEN), ("imperative", _IMPERATIVE)):
        m = pattern.search(text)
        if m:
            out.append(_finding(rule, where, text, m.start()))
    m = _KEY_FILES.search(text) or _ASKS_HIGH.search(text)
    if m:
        out.append(_finding("sensitive-data", where, text, m.start()))
    else:
        m = _ASKS.search(text)
        if m:
            out.append(_finding("asks-for-secret", where, text, m.start()))
    m = _EXFIL.search(text)
    if m:
        out.append(_finding("exfil-host", where, text, m.start()))
    else:
        m = _URL.search(text)
        if m:
            out.append(_finding("url", where, text, m.start()))
    named = next((n for n in sorted(others) if "_" in n and len(n) >= 6 and n in text
                  and re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text)), None)
    if named:
        out.append(_finding("shadowing", where, text, text.index(named)))
    else:
        m = _INSTEAD.search(text)
        if m:
            out.append(_finding("shadowing", where, text, m.start(), "medium"))
    m = _BLOB.search(text)
    if m:
        out.append(_finding("encoded-blob", where, text, m.start()))
    if len(raw) > LONG_TEXT:
        out.append(_finding("long-text", where, text))
    return out


def _walk(value, where: str, budget: list[int], depth: int = 0, under_properties: bool = False):
    """(where, text, is a parameter name) for every key and every string in a schema: nothing
    is skipped for being in an unusual place. Raises OverflowError when it is too much to read."""
    budget[0] -= 1
    if budget[0] < 0 or depth > WALK_DEPTH:
        raise OverflowError
    if isinstance(value, str):
        yield where, value, False
    elif isinstance(value, dict):
        for key, sub in value.items():
            key = str(key)
            inner = f"parameter {where.removeprefix('parameter ') + '.' if where.startswith('parameter ') else ''}{key[:80]}" \
                if under_properties else where
            yield inner, key, under_properties
            yield from _walk(sub, inner, budget, depth + 1, key in ("properties", "patternProperties", "$defs", "definitions")
                             and isinstance(sub, dict) and not under_properties)
    elif isinstance(value, (list, tuple)):
        for sub in value:
            yield from _walk(sub, where, budget, depth + 1)


def scan_tool(name: str, description: str, schema: dict, *, others: set[str]) -> list[dict]:
    """Findings for one tool definition. `others` are the names of tools it has no business
    mentioning: HunterAI's own and other servers'."""
    out = _scan_text(f"{name}\n{description or ''}", "description", others)
    try:
        for where, text, is_name in _walk(schema or {}, "schema", [MAX_NODES]):
            if is_name and _PARAM.match(text):
                out.append(_finding("suspicious-parameter", where, text))
            if len(text) >= 4:
                out += _scan_text(text, where, others)
    except (OverflowError, RecursionError):
        out.append(_finding("unreadable-definition", "schema", "the argument schema is too deep or too large to read in full"))
    seen, unique = set(), []
    for f in sorted(out, key=lambda f: ("high", "medium", "info").index(f["severity"])):
        key = (f["rule"], f["where"])
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique[:MAX_FINDINGS]


def _shown(schema: dict) -> str:
    try:
        return json.dumps(schema or {}, indent=1, sort_keys=True, ensure_ascii=False, default=str)[:SHOWN_CHARS]
    except (ValueError, RecursionError):
        return "(the schema could not be shown)"


def digest(name: str, description: str, schema: dict) -> str:
    blob = json.dumps({"name": name, "description": description or "", "schema": schema or {}},
                      sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8", errors="surrogatepass")).hexdigest()


def launch_digest(cfg) -> str:
    """What decides which code answers: a server started another way is a server not seen before."""
    blob = json.dumps([cfg.transport, cfg.command, list(cfg.args), cfg.cwd, cfg.url], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def judge(server: str, launch: str, tools: list[dict], *, pins: dict | None, exposed: bool = True,
          others: set[str] | None = None) -> dict:
    """Compare what a server sent with what was accepted. Returns the report, with the pin
    record to save under "pins" (first sight pins the tools that have no high finding;
    after that only the owner adds a pin). `pins` None means the record of what was accepted
    could not be read: then nothing counts as accepted and nothing is accepted on sight."""
    unreadable = pins is None
    known = (pins or {}).get(server) if isinstance((pins or {}).get(server), dict) else None
    first = not unreadable and (known is None or known.get("launch") != launch)
    kept = {} if first or unreadable else dict(known.get("tools") or {})
    now = int(time.time())
    rows = []
    for t in tools:
        name, description, schema = t["name"], t.get("description") or "", t.get("schema") or {}
        d = digest(name, description, schema)
        findings = scan_tool(name, description, schema, others=others or set())
        shown = _shown(schema)
        if len(description) > SHOWN_CHARS or len(shown) >= SHOWN_CHARS:
            findings.insert(0, _finding("unreadable-definition", "description", "the definition is too long to show in full"))
        high = any(f["severity"] == "high" for f in findings)
        pin = kept.get(name)
        row = {"name": name, "digest": d, "findings": findings, "description": description[:SHOWN_CHARS], "schema": shown}
        if pin and pin.get("digest") == d:
            state = "ok"
        elif pin:
            state, row["previous"], row["previous_schema"] = "changed", pin.get("description", ""), pin.get("schema", "")
        elif first and not high:
            state = "ok"
            kept[name] = {"digest": d, "description": row["description"], "schema": shown, "at": now, "by": "first sight"}
        else:
            state = "flagged" if first else "new"
        row["state"], row["held"] = state, exposed and state != "ok"
        rows.append(row)
    listed = {t["name"] for t in tools}
    report = {"server": server, "launch": launch, "first_sight": first, "exposed": exposed, "scanned_at": now, "tools": rows,
              "held": sum(1 for r in rows if r["held"]), "removed": sorted(n for n in kept if n not in listed),
              "pins": {"launch": launch, "tools": kept}}
    if unreadable:
        report["note"] = ("The record of accepted tool definitions could not be read, so every tool is held until you "
                          "accept it again.")
    return report


def with_approved(pin_record: dict, report: dict, approved: dict[str, str]) -> dict:
    """The pin record with the owner's approvals added. Each approval names the digest the
    owner was shown; a definition that has changed since is not approved by it."""
    rows = {r["name"]: r for r in report["tools"]}
    tools = dict(pin_record.get("tools") or {})
    for name, shown in approved.items():
        row = rows.get(name)
        if row is None or row["state"] == "ok":
            raise ValueError(f'"{name}" is not waiting for approval')
        if row["digest"] != shown:
            raise ValueError(f'"{name}" has changed since it was shown; look at it again')
        tools[name] = {"digest": row["digest"], "description": row["description"], "schema": row["schema"],
                       "at": int(time.time()), "by": "owner"}
    return {"launch": report["launch"], "tools": tools}


def _file():
    return config.home() / "mcp-pins.json"


def load_pins() -> dict | None:
    """What was accepted, by server. {} when nothing ever was; None when the file is there
    and cannot be read, which must not be taken for "nothing was accepted"."""
    try:
        raw = _file().read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _write(pins: dict) -> None:
    tmp = _file().with_suffix(".tmp")
    tmp.write_text(json.dumps(pins, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, _file())


def save_pin(server: str, record: dict) -> None:
    pins = load_pins()
    if pins is None:                       # keep what could not be read for the owner to look at
        try:
            os.replace(_file(), _file().with_name(_file().name + ".unreadable"))
        except OSError:
            pass
        pins = {}
    pins[server] = record
    _write(pins)


def forget(server: str) -> None:
    pins = load_pins()
    if pins and pins.pop(server, None) is not None:
        _write(pins)
