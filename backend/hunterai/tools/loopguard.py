"""A model can get stuck asking for the same thing from a server that is down: the same call,
the same error, again, until its turns are spent. This keeps count, per agent (the main loop
of a run, or one subagent), and steps in:

- the same call (tool and arguments) that failed twice in a row with the same result is not
  run a third time: the model is told so and asked to do something else;
- a host whose every answer has been a server error or a timeout gets a line added to the
  result from the fourth failure on, and is not called again after the eighth.

A call that succeeds may be repeated as often as the model likes (polling a process, reading
a page again). Only web tools that name their host are counted per host; a command the model
runs in a shell is not looked into."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

SAME_FAILURES = 2            # identical failed calls allowed before the next one is refused
HOST_WARN = 4                # failures in a row on one host before the result says so
HOST_STOP = 8                # failures in a row on one host after which it is not called again
_BAD = re.compile(r"(?:^HTTP |^Status: |HTTP/\d(?:\.\d)? )(5\d\d|429)\b", re.M)


def failed(text: str, is_error: bool) -> bool:
    """Whether a tool result is a failure of the other side: an error, a 5xx, or a 429."""
    return bool(is_error) or bool(_BAD.search((text or "")[:400]))


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


class Guard:
    """One per agent loop."""

    def __init__(self):
        self._last: tuple[str, str] | None = None      # (call, digest of its failed result)
        self._repeats = 0
        self._hosts: dict[str, int] = {}

    def before(self, tc) -> str | None:
        """The refusal for this call, or None when it may run."""
        if self._last is not None and self._last[0] == _key(tc) and self._repeats >= SAME_FAILURES:
            return (f"Not run: you made this exact call {self._repeats} times in a row and it failed the same way each time. "
                    "Do not repeat it. Try something different, or say in your answer that this is unavailable and why.")
        host = host_of(tc.arguments)
        if host and self._hosts.get(host, 0) >= HOST_STOP:
            return (f"Not run: {host} has failed {self._hosts[host]} times in a row in this run (server errors or timeouts). "
                    "It is down or refusing you. Stop calling it, work on what does not need it, and report that it was unavailable.")
        return None

    def after(self, tc, text: str, is_error: bool) -> str:
        """Record the outcome; the line to add to the result ("" for none)."""
        bad = failed(text, is_error)
        key = _key(tc)
        if bad:
            digest = hashlib.sha256((text or "")[:2000].encode("utf-8", "replace")).hexdigest()
            self._repeats = self._repeats + 1 if self._last == (key, digest) else 1
            self._last = (key, digest)
        else:
            self._last, self._repeats = None, 0
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
