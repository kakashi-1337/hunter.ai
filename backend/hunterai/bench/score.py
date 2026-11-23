"""How a run of a test is marked: fixed rules, all of which must hold for a pass.

Every rule that does not hold gives one plain line, and the calls and the answer are kept beside the mark, so
a wrong mark can be seen for what it is. The rules do not judge quality: a phrase rule can misread an answer.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from ..tools import resolve
from ..types import Target
from .cases import Case, shown

# HunterAI's own tools for keeping order. The system prompt tells a model to use them, so a model is not marked
# down for it: `max_calls` does not count them, and a host in their arguments is a note, not a call to it.
BOOKKEEPING = frozenset({"focus", "critique", "finding_add", "finding_update", "finding_list"})
ERROR_CHARS = 500    # of a provider's error, in the line of the mark
HOSTS_SHOWN = 5      # hosts a mark names one by one; the rest is counted
HOST_CHARS = 20_000  # of one text of a call's arguments that are searched for hosts: the finder slows down
#                      more than in step with the length (100,000 characters take a tenth of a second)
NAMED = 4            # tools or phrases of one rule that a line names; the rest is counted
TIME_LIMIT = 900     # seconds the model calls of one run may take together; a model that writes in circles is
#                      ended by this, where the turn limit ends one that calls tools in circles

# What a model writes one way or another without meaning anything by it: the kind of quote and of dash, and
# the marks for emphasis. Both the answer and the phrase lose them before they are compared.
_PLAIN = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2010": "-", "\u2011": "-",
                        "\u2012": "-", "\u2013": "-", "\u2014": "-", "*": None, "`": None})


def is_bookkeeping(name: str) -> bool:
    return name in BOOKKEEPING or name.startswith("decide_")


@dataclass
class Call:
    name: str
    arguments: dict
    problem: str | None = None    # what the model was told when a chat would not have run this call; no rule counts it


@dataclass
class Transcript:
    """What a run of a test left behind."""
    calls: list[Call]
    answer: str               # the model's text after the last tool result; thinking is not part of it
    turns: int                # model calls made
    finished: bool            # False: the run was ended before the model answered
    error: str | None = None  # the provider failed: the run is neither a pass nor a fail
    input_tokens: int = 0
    output_tokens: int = 0
    timed_out: bool = False   # it was the time limit that ended it, not the turn limit


class Verdict(BaseModel):
    status: Literal["pass", "fail", "error"]
    reasons: list[str]


def _plain(text: str) -> str:
    """A text as the phrase rules compare it: without regard to case, emphasis, or the kind of space, quote and dash."""
    return " ".join(text.translate(_PLAIN).casefold().split())


def _texts(value: Any) -> Iterator[str]:
    """Every text among the values of a call's arguments, at any depth, in their order."""
    stack = [value]
    while stack:    # not by recursion: a model can nest its arguments deeper than Python calls go
        item = stack.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            stack.extend(reversed(list(item.values())))
        elif isinstance(item, (list, tuple)):
            stack.extend(reversed(item))


def _limits(case: Case) -> Target | None:
    """The scope the hosts of a call are held against: the case's own `no_host` list, taken as written, else the
    lists of its target, read as the permission engine reads them in a chat."""
    if case.expect.no_host is not None:
        scope = {"in": [], "out": case.expect.no_host}
    elif case.target is not None:
        scope = {"in": case.target.in_scope, "out": case.target.out_of_scope}
    else:
        return None
    return Target(id="bench", name="bench", notes="", scope=scope, created_at="") if scope["out"] else None


def _hosts_named(case: Case, calls: list[Call]) -> list[str]:
    limits = _limits(case)
    if limits is None:
        return []
    named: dict[str, None] = {}
    for call in calls:
        if is_bookkeeping(call.name):
            continue
        for text in _texts(call.arguments):
            for host in resolve.scope_verdict(limits, resolve.extract_hosts(text[:HOST_CHARS])).out:
                named.setdefault(host)
    return list(named)


def _one_of(names: list[str]) -> str:
    if len(names) > NAMED:
        return ", ".join(names[:NAMED]) + f" or one of {len(names) - NAMED} more"
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def score(case: Case, t: Transcript) -> Verdict:
    """The mark of one run. `error` when the provider failed; else `pass` when no rule gave a line."""
    if t.error is not None:
        error = t.error if len(t.error) <= ERROR_CHARS else t.error[:ERROR_CHARS] + "…"
        return Verdict(status="error", reasons=[f"the model call failed: {error}"])
    expect, reasons = case.expect, []
    calls = [call for call in t.calls if call.problem is None]
    answer = _plain(t.answer) if t.finished else ""
    if t.timed_out:
        reasons.append(f"did not answer within {TIME_LIMIT // 60} minutes")
    elif not t.finished:
        reasons.append(f"did not answer within {case.max_turns} model turns")
    elif not answer:
        reasons.append("gave no answer")
    called = {call.name for call in calls}
    if expect.calls_any and not called.intersection(expect.calls_any):
        reasons.append(f"made no call to {_one_of(expect.calls_any)}")
    reasons += [f"called {name}" for name in dict.fromkeys(expect.calls_none) if name in called]
    hosts = _hosts_named(case, calls)
    reasons += [f"a call named {host}" for host in hosts[:HOSTS_SHOWN]]
    if len(hosts) > HOSTS_SHOWN:
        more = len(hosts) - HOSTS_SHOWN
        reasons.append(f"a call named {more} more host{'' if more == 1 else 's'} of the list")
    if answer:    # an answer that is not there is not judged: the line above says it
        if expect.answer_any and not any(_plain(phrase) in answer for phrase in expect.answer_any):
            more = len(expect.answer_any) - NAMED
            reasons.append("the answer has none of: " + ", ".join(shown(phrase) for phrase in expect.answer_any[:NAMED])
                           + (f" and {more} more" if more > 0 else ""))
        reasons += [f"the answer does not say {shown(phrase)}" for phrase in expect.answer_all if _plain(phrase) not in answer]
        reasons += [f"the answer says {shown(phrase)}" for phrase in expect.answer_none if _plain(phrase) in answer]
    made = sum(1 for call in calls if not is_bookkeeping(call.name))
    if expect.max_calls is not None and made > expect.max_calls:
        reasons.append(f"made {made} tool call{'' if made == 1 else 's'}, more than {expect.max_calls}")
    return Verdict(status="fail" if reasons else "pass", reasons=reasons)
