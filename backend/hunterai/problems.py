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
LLAMACPP = "llamacpp"       # the owner's llama-server: he starts it, with the model and the context he chooses
_SPOKEN = {LLAMACPP: ("llama.cpp", "llama-server")}     # how a provider is named in words, where its name in the code is none


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
        if who == LLAMACPP:
            return ("No answer came in time. A model that has to be loaded first takes its time, and so does a long request "
                    "on a small card: send the message again. If it goes on, the terminal llama-server runs in says what it "
                    "is doing.")
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
        if who == LLAMACPP:
            if getattr(e, "status", None):          # it answered, with a failure of its own: it runs
                return ("It failed on its side: send the message again. If it goes on, the terminal it runs in says why.")
            return ("Start llama-server and see that the address under Settings is the one it listens on (it prints it when "
                    "it starts), then send the message again.")
        return ("It could not be reached, or it failed on its side: check the internet connection (and the proxy for model "
                "providers under Settings, if you set one), then send the message again.")
    if who == COLIBRI:                              # it serves one model: there is no other one to pick in the list
        return ("colibri did not accept the request as it was sent: its words say what the model it serves does not do. For "
                "tools or pictures, have it serve another model (./start-here.sh --reconfigure in the colibri folder).")
    if who == LLAMACPP:
        return ("llama-server did not accept the request as it was sent: its words say what the model, or the chat template it "
                "was started with, does not do.")
    return ("The provider did not accept the request as it was sent: if its words name the model, pick another one; if they "
            "name an option, change it under Options.")


def model_call(e) -> str:
    """A model call that failed, for the owner: the provider's own words (with its name, when they lack it), then
    what to do. An error whose message says what to do itself (`complete`) is left as it is."""
    said = (e.message or "").strip()
    if getattr(e, "complete", False):
        return said
    who = getattr(e, "provider", None)
    names = _SPOKEN.get(who, (who,))
    if who and not any(name.lower() in said.lower() for name in names):
        said = f"{names[0]}: {said}"
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


def only_thought(blocks, stop: str | None) -> bool:
    """What a model call wrote (`blocks`) is thinking and nothing after it: neither text nor a tool call, and the call
    ended by itself. Seen with Huihui-Qwen3.5-9B on the owner's laptop, whose turn after a tool result was that with
    thinking on. Not a call that was cut off or ended by a filter, which has its own word, and not thinking its
    provider sealed: that goes back to the provider as it came, with what follows it."""
    why = (stop or "").lower()
    thought = [b for b in blocks if b.type == "thinking"]
    return (why not in _AT_LIMIT | _WINDOW_FULL | _FILTERED and any(b.text.strip() for b in thought)
            and not any(b.opaque for b in thought)
            and not any(b.type == "tool_call" or (b.type == "text" and b.text.strip()) for b in blocks))


ASKED_AGAIN = ("The model ended a turn with thinking only and no answer, so HunterAI asked it once more with thinking switched off. "
               "Thinking stays off for the rest of this run. To have it off from the start, switch Thinking off for this chat under "
               "Options.")


def no_answer(asked: bool, refused: str | None = None) -> str:
    """The word under a turn that stayed thinking only. `asked`: it was asked once more with thinking off; `refused`:
    what that second call failed with."""
    then = "What it thought stands above. Send the message again, or pick another model for this chat."
    if refused:
        return ("The model ended its turn with thinking only and gave no answer. HunterAI asked it once more with thinking switched "
                f"off, and that call failed ({refused[:300]}). {then}")
    if asked:
        return ("The model ended its turn with thinking only and gave no answer, also when it was asked once more with thinking "
                f"switched off. {then}")
    return f"The model ended its turn with thinking only and gave no answer. {then}"


def cut_off(reason: str | None, *, provider: str | None, max_tokens: int | None = None, output_tokens: int = 0,
            window: int | None = None, circles: bool = False) -> tuple[str, str] | None:
    """What to say under an answer that did not end by itself: (kind, text), or None when it did. `max_tokens` is
    what the chat's options set, `output_tokens` what the call produced, `window` the context window of a local model.
    `circles`: the window could have been made larger for it, and was not, because the answer repeats itself."""
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
        tokens = "token" if max_tokens == 1 else "tokens"
        return "cut_off", (f'The answer was cut off at the {max_tokens:,} output {tokens} this chat allows. Send "continue" to '
                           'have the model go on. For longer answers raise "Max output tokens" under Options, or empty that '
                           "field: this chat then sets no limit of its own.")
    if provider == LOCAL:           # Ollama writes until the window is full: the request and the answer share it
        size = f" ({window:,} tokens)" if window else ""
        if circles:
            return "cut_off", (f"The answer was cut off: the context window of the local model{size} was full. Its end says "
                               "the same thing over and over, so it was not written again in a larger window. Send the "
                               "message again, or ask in another way.")
        return "cut_off", (f"The answer was cut off: the context window of the local model{size} was full. It holds the request "
                           'and the answer together. Raise "Context window" under Options for this chat (a larger window needs '
                           "more memory), or continue in a new chat.")
    if provider == COLIBRI:         # the request and the answer share the context colibri was started with
        from .providers import colibri
        size = f" ({window:,} tokens)" if window else ""
        larger = colibri.context_for(window + 1) if window else None
        more = f"; for more room start colibri with a larger context ({colibri.START} --ctx {larger} in the colibri folder)" if larger else ""
        return "cut_off", (f"The answer was cut off: colibri ended it at its limit. Its context window{size} holds the request and "
                           "the answer together and was full, or colibri was started with a limit for one answer (--ngen). Send "
                           f'"continue" to have the model go on{more}.')
    if provider == LLAMACPP:        # the request and the answer share the context llama-server was started with
        from .providers import llamacpp
        size = f" ({window:,} tokens)" if window else ""
        larger = f"-c {llamacpp.context_to_start(window, window)}" if window else "-c with a larger number"
        return "cut_off", (f"The answer was cut off: llama-server ended it at its limit. Its context window{size} holds the request "
                           'and the answer together and was full. Send "continue" to have the model go on; for more room start '
                           f"llama-server with a larger context ({larger}; a larger context needs more memory).")
    return "cut_off", ('The answer was cut off at the limit the model has for one answer. Send "continue" to have it go on. If '
                       'the model takes "Max output tokens" under Options, a higher value there gives it more room.')


def window_grew(before: int, after: int, *, again: bool = False, kept: bool = True) -> str:
    """What to say under an answer for which the context window of a local model was made larger by itself, from
    `before` tokens to `after`. `again`: the answer had filled the window and was written once more. `kept`: the
    size was written into the chat as its own (not for a model that answered in place of the chat's)."""
    why = ("the answer filled the smaller one and was written again" if again
           else "the chat did not fit the smaller one")
    whose = ("It is this chat's own now (Context window under Options)." if kept
             else "It holds for this answer: the chat's own model was not the one that answered.")
    return (f"The context window of this chat was raised from {before:,} to {after:,} tokens: {why}. {whose} "
            'A window grows by itself up to "Largest context window for local models" in Settings (a larger window '
            "needs more memory; 0 there keeps a window as it is set).")


def window_stayed(now: int, tried: int, said: str) -> str:
    """What to say under an answer for which the context window of a local model could not be made larger: the model
    did not answer with one of `tried` tokens (`said`: the words of the call that failed) and does with `now`."""
    said = " ".join((said or "").split())
    said = said[:MESSAGE] + ("…" if len(said) > MESSAGE else "")
    return (f"The context window of this chat stays at {now:,} tokens: the local model did not answer with one of "
            f"{tried:,} ({said}). HunterAI does not try that size again for this model until it is started again; what "
            "does not fit the window is shortened as before.")
