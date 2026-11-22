"""A model can get stuck asking for the same thing from a server that is down, or running a
command that cannot work: the same call, the same error, again, until its turns are spent.
This keeps count, per agent (the main loop of a run, or one subagent), and steps in:

- the same call (tool and arguments) that failed twice with the same result is not run a
  third time: the model is told so and asked to do something else. A command that ends with
  an error code is a failed call, and how long it took is no part of its result. Two failing
  calls taken in turns are counted each for itself; any call that works in between starts
  every count again, because something may have changed;
- a host whose every answer has been a server error or a timeout gets a line added to the
  result from the fourth failure on, and is not called again after the eighth;
- a model that answers GIVE_UP refusals in a row with one more call that is refused is
  stuck: `stuck()` says so, and the loop that asked ends the run with that word for the owner.

A call that succeeds may be repeated as often as the model likes (polling a process, reading
a page again). Only web tools that name their host are counted per host; a command the model
runs in a shell is not looked into for the hosts it names."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

SAME_FAILURES = 2            # identical failed calls allowed before the next one is refused
GIVE_UP = 3                  # refusals in a row after which the model is taken to be stuck
SHOWN = 160                  # characters of a call that are shown to the owner
HOST_WARN = 4                # failures in a row on one host before the result says so
HOST_STOP = 8                # failures in a row on one host after which it is not called again
_BAD = re.compile(r"(?:^HTTP |^Status: |HTTP/\d(?:\.\d)? )(5\d\d|429)\b", re.M)
_TOOK = re.compile(r"^duration: [\d.]+s$", re.M)       # in the head of a command's result (tools/shell.py)


def failed(text: str, is_error: bool, exit_code: int | None = None) -> bool:
    """Whether a tool result is a failure: an error, a 5xx or a 429 of the other side, or a command that ended
    with an error code."""
    return bool(is_error) or bool(exit_code) or bool(_BAD.search((text or "")[:400]))


def host_of(args: dict) -> str | None:
    if not isinstance(args, dict):
        return None
    if isinstance(args.get("url"), str):
        try:
            host = urlsplit(args["url"]).hostname
        except ValueError:
            host = None
        return host.lower() if host else None
    if isinstance(args.get("host"), str) and args["host"].strip():
        return args["host"].strip().lower()
    return None


def _key(tc) -> str:
    return tc.name + "\n" + json.dumps(tc.arguments, sort_keys=True, ensure_ascii=False, default=str)


def _shown(tc) -> str:
    """A call in a line, for the owner."""
    args = tc.arguments if isinstance(tc.arguments, dict) else {}
    said = args.get("command") if isinstance(args.get("command"), str) else json.dumps(args, ensure_ascii=False, default=str)
    said = " ".join(said.split())
    return f"{tc.name}: {said[:SHOWN]}{'...' if len(said) > SHOWN else ''}"


class Guard:
    """One per agent loop."""

    def __init__(self):
        self._failing: dict[str, tuple[str, int]] = {}     # call -> (digest of its failed result, how often it came)
        self._refused = 0                                  # refusals in a row
        self._refused_call = ""
        self._hosts: dict[str, int] = {}

    def before(self, tc) -> str | None:
        """The refusal for this call, or None when it may run."""
        said = self._refusal(tc)
        if said is not None:
            self._refused += 1
            self._refused_call = _shown(tc)
        return said

    def _refusal(self, tc) -> str | None:
        times = self._failing.get(_key(tc), ("", 0))[1]
        if times >= SAME_FAILURES:
            return (f"Not run: you made this exact call {times} times and it failed the same way each time. "
                    "Do not repeat it. Try something different, or say in your answer that this is unavailable and why.")
        host = host_of(tc.arguments)
        if host and self._hosts.get(host, 0) >= HOST_STOP:
            return (f"Not run: {host} has failed {self._hosts[host]} times in a row in this run (server errors or timeouts). "
                    "It is down or refusing you. Stop calling it, work on what does not need it, and report that it was unavailable.")
        return None

    def stuck(self) -> str | None:
        """The word for the owner when the model answered refusal after refusal with a call that was refused
        again; None while it is not stuck."""
        if self._refused < GIVE_UP:
            return None
        return (f"Stopped: the model kept sending a call that could not work, and was refused {self._refused} times in a row "
                f"({self._refused_call}). It is not getting anywhere with it. Tell it what to do instead, or give the "
                "task to another model.")

    def after(self, tc, text: str, is_error: bool, exit_code: int | None = None) -> str:
        """Record the outcome of a call that ran; the line to add to the result ("" for none)."""
        self._refused = 0
        bad = failed(text, is_error, exit_code)
        key = _key(tc)
        if bad:
            digest = hashlib.sha256(_TOOK.sub("", (text or "")[:2000]).encode("utf-8", "replace")).hexdigest()
            before = self._failing.get(key)
            self._failing[key] = (digest, before[1] + 1 if before and before[0] == digest else 1)
        else:
            self._failing.clear()
        host = host_of(tc.arguments)
        if not host:
            return ""
        if not bad:
            self._hosts.pop(host, None)
            return ""
        n = self._hosts[host] = self._hosts.get(host, 0) + 1
        if n < HOST_WARN:
            return ""
        return (f"\n\n[{host} has failed {n} times in a row in this run. It is probably down or refusing you: stop retrying it, "
                f"and report that. After {HOST_STOP} failures in a row it is not called again.]")
