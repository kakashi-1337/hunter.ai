"""Errors in words the owner can act on: what is wrong, and what to do about it."""
from __future__ import annotations

import math
import time
import traceback
from pathlib import Path

from . import config, secrets

RESTART = "press Ctrl+C where HunterAI runs, then start it again (scripts/start.sh)"
SHOWN = 3                   # things wrong with one request that are spelled out
LOG_BYTES = 2_000_000       # the file of faults is started anew at this size; the one before it is kept beside it
MESSAGE = 200               # characters of an exception's own text that are shown
_PLACES = ("body", "query", "path", "header", "cookie")       # where FastAPI found the field: not part of its name
COLIBRI = "colibri"         # the provider that is a server of the owner's with one model: other things can be done about its errors


def _one(error: dict) -> str:
    loc = [str(p) for p in error.get("loc") or ()]
    name = ".".join(loc[1:] if loc and loc[0] in _PLACES else loc)
    kind, msg = error.get("type"), str(error.get("msg") or "is not valid").removeprefix("Value error, ")
    if kind == "extra_forbidden" and name:
        return f'"{name}" is not a setting this server knows'
    if kind == "missing" and name:
        return f'"{name}" is missing'
    msg = msg[:1].lower() + msg[1:]
    return f'"{name}": {msg}' if name else msg


def invalid(errors, stale: bool = False) -> str:
    """What a request holds that the server refuses, from pydantic's list of errors: each field by its name with
    what is wrong with it. `stale`: the server runs code older than what is on disk; a field it does not know is
    then one the page learned in the update."""
    errors = list(errors)
    said = "; ".join([_one(e) for e in errors[:SHOWN]] + ([f"and {len(errors) - SHOWN} more"] if len(errors) > SHOWN else [])) + "."
    if stale and any(e.get("type") == "extra_forbidden" for e in errors):
        said += f" The server still runs the code from before the last update: {RESTART}."
    return said


def _advice(e, who: str | None) -> str:
    text = (e.message or "").lower()
    if "not installed" in text:
        return "Install it, or pick another model for this chat."
    if e.kind == "auth":
        return ("Enter the key under Settings, or pick a model of another provider for this chat." if "is not set" in text
                else "The provider did not accept the key: check it under Settings.")
    if e.kind == "credits":
        return "The account has no credit for this model: add credit at the provider, or pick another model for this chat."
    if e.kind == "policy":
        return "The provider or the model refused it: reword the request, or pick another model for this chat."
    if e.kind == "rate_limit":
        wait = f"{math.ceil(e.retry_after)} s" if e.retry_after else "a minute"
        return f"The provider limits how often this key may ask: wait {wait}, then send the message again."
    if e.kind == "timeout":
        if who == COLIBRI:
            return ("No answer came in time. colibri works on one request at a time, so it may still be busy with another one "
                    "(its page, at the address under Settings, shows what it is doing): send the message again later.")
        return "No answer came in time: send the message again. If it keeps happening, the provider is slow or down."
    if e.kind == "unavailable":
        if who == "ollama":
            return ("Start Ollama (`ollama serve`) and see with `ollama ps` that the model fits this machine's memory, "
                    "then send the message again.")
        if who == COLIBRI:
            if getattr(e, "status", None):          # it answered, with a failure of its own: it runs
                return ("It failed on its side: send the message again. If it goes on, `c/coli logs` in the colibri folder "
                        "says why.")
            return ("Start it (./start-here.sh in the colibri folder) and see that the address under Settings is the one it "
                    "listens on, then send the message again.")
        return ("It could not be reached, or it failed on its side: check the internet connection (and the proxy for model "
                "providers under Settings, if you set one), then send the message again.")
    if who == COLIBRI:                              # it serves one model: there is no other one to pick in the list
        return ("colibri did not accept the request as it was sent: its words say what the model it serves does not do. For "
                "tools or pictures, have it serve another model (./start-here.sh --reconfigure in the colibri folder).")
    return ("The provider did not accept the request as it was sent: if its words name the model, pick another one; if they "
            "name an option, change it under Options.")


def model_call(e) -> str:
    """A model call that failed, for the owner: the provider's own words (with its name, when they lack it), then
    what to do. An error whose message says what to do itself (`complete`) is left as it is."""
    said = (e.message or "").strip()
    if getattr(e, "complete", False):
        return said
    who = getattr(e, "provider", None)
    if who and who.lower() not in said.lower():
        said = f"{who}: {said}"
    return f"{said.rstrip('.')}. {_advice(e, who)}"


def _kept(e: BaseException, doing: str) -> Path | None:
    """The fault with its traceback, added to logs/errors.log. None when that could not be done."""
    try:
        path = config.logs_dir() / "errors.log"
        if path.exists() and path.stat().st_size >= LOG_BYTES:
            path.replace(path.with_name("errors.log.1"))
        trace = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} while {doing}\n{secrets.scrub(trace)}\n")
        return path
    except Exception:  # noqa: BLE001 - saying the fault matters more than keeping it
        return None


def own_failure(e: BaseException, doing: str) -> str:
    """An exception nothing in HunterAI expected: a fault of the program, which the owner cannot fix by changing
    what he sent. Its traceback goes to logs/errors.log, and the text says so."""
    text = secrets.scrub(str(e))
    text = text[:MESSAGE] + ("…" if len(text) > MESSAGE else "")
    path = _kept(e, doing)
    where = f"the full error is at the end of {path}, and " if path else ""
    return (f"HunterAI failed while {doing} ({type(e).__name__}: {text}). This is a fault in HunterAI, not in what you sent. "
            f'Trying again may work. To report it: {where}"Copy diagnostics" in the setup check gives the state of this install.')


# Why a model call ended, as the providers name it, where the answer did not end by itself.
_AT_LIMIT = {"length", "max_tokens"}                        # OpenAI-compatible and Ollama; Anthropic and Gemini
_WINDOW_FULL = {"model_context_window_exceeded"}            # Anthropic
_FILTERED = {"content_filter", "safety", "recitation", "prohibited_content", "blocklist", "spii", "image_safety"}
LOCAL = "ollama"


def at_limit(reason: str | None) -> bool:
    return (reason or "").lower() in _AT_LIMIT


def cut_off(reason: str | None, *, provider: str | None, max_tokens: int | None = None, output_tokens: int = 0,
            window: int | None = None) -> tuple[str, str] | None:
    """What to say under an answer that did not end by itself: (kind, text), or None when it did. `max_tokens` is
    what the chat's options set, `output_tokens` what the call produced, `window` the context window of a local model."""
    why = (reason or "").lower()
    if why in _FILTERED:
        return "filtered", (f"The answer stops here because the content filter of the provider ended it ({reason}). Reword the "
                            "request, or pick another model for this chat.")
    if why in _WINDOW_FULL:
        return "cut_off", ("The answer was cut off: the context window of the model was full. Continue in a new chat, or choose "
                           "a model with a larger context window.")
    if not at_limit(reason):
        return None
    if max_tokens and output_tokens >= max_tokens:
        return "cut_off", (f'The answer was cut off at the {max_tokens:,} output tokens this chat allows. Send "continue" to '
                           'have the model go on, or raise "Max output tokens" under Options.')
    if provider == LOCAL:           # Ollama writes until the window is full: the request and the answer share it
        size = f" ({window:,} tokens)" if window else ""
        return "cut_off", (f"The answer was cut off: the context window of the local model{size} was full. It holds the request "
                           'and the answer together. Raise "Context window for local models" in Settings (a larger window needs '
                           "more memory), or continue in a new chat.")
    if provider == COLIBRI:         # the request and the answer share the context colibri was started with
        from .providers import colibri
        size = f" ({window:,} tokens)" if window else ""
        larger = colibri.context_for(window + 1) if window else None
        more = f"; for more room start colibri with a larger context ({colibri.START} --ctx {larger} in the colibri folder)" if larger else ""
        return "cut_off", (f"The answer was cut off: colibri ended it at its limit. Its context window{size} holds the request and "
                           "the answer together and was full, or colibri was started with a limit for one answer (--ngen). Send "
                           f'"continue" to have the model go on{more}.')
    return "cut_off", ('The answer was cut off at the limit the model has for one answer. Send "continue" to have it go on. If '
                       'the model takes "Max output tokens" under Options, a higher value there gives it more room.')
