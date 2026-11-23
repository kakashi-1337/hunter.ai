"""The sets of the bench: files of scripted tests.

A set is a TOML file. One ships with HunterAI (the Starter set); the owner's own sets are the `*.toml`
files in `~/.hunterai/bench/`. A file is checked when it is read, and what is wrong with it is said in
words that name the case and the field, because these files are written by hand.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, field_validator

from .. import config
from ..types import Mode

Group = Literal["tool-use", "honesty", "scope", "stopping"]
GROUPS: tuple[str, ...] = get_args(Group)
MODES: tuple[str, ...] = get_args(Mode)
MAX_TURNS = 8           # model calls one test may take, unless the case says otherwise
MAX_TURNS_CAP = 12      # ... and the most a case may ask for
MAX_CASES = 200
MAX_TEXT = 20_000       # characters of a prompt, and of one scripted result
MAX_FILE = 2_000_000    # bytes of an own file; a set of 200 short tests is a twentieth of this
BRIEF_CHARS = 500       # of a prompt, in the lists the page shows
SHOWN_CHARS = 80        # of a value of the file that a reason repeats
STARTER = "starter"
OWN = "own:"            # the ids of the owner's own sets start with this; the rest is the file's name

_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_CASE_KEYS = ("id", "group", "prompt", "mode", "max_turns", "target", "tool", "expect")
_EXPECT_LISTS = ("calls_any", "calls_none", "answer_any", "answer_all", "answer_none")


class CaseError(ValueError):
    """A set that cannot be used; the message says what is wrong, for the owner."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Scripted(_Model):
    """What a tool answers in a test. The first entry of a case that fits a call is used for it."""
    name: list[str]
    args_has: str | None = None    # a word the call's arguments must hold
    result: str
    error: bool = False

    @field_validator("name", mode="before")
    @classmethod
    def _one_name_is_a_list(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value


class CaseTarget(_Model):
    name: str
    in_scope: list[str] = []
    out_of_scope: list[str] = []


class Expect(_Model):
    """The rules of a case; all of them must hold for a pass."""
    calls_any: list[str] = []
    calls_none: list[str] = []
    no_host: list[str] | None = None    # not given: the out-of-scope list of the case's target
    answer_any: list[str] = []
    answer_all: list[str] = []
    answer_none: list[str] = []
    max_calls: int | None = None


class Case(_Model):
    id: str
    group: Group
    prompt: str
    mode: Mode = "pentesting"
    target: CaseTarget | None = None
    tools: list[Scripted] = []
    max_turns: int = MAX_TURNS
    expect: Expect


class CaseSet(_Model):
    id: str
    name: str
    builtin: bool
    cases: list[Case]


class CaseBrief(_Model):
    """A case as a list shows it."""
    id: str
    group: str
    prompt: str


class SetInfo(_Model):
    id: str
    name: str
    builtin: bool
    cases: list[CaseBrief]
    problem: str | None = None    # why the file cannot be used; `cases` is empty then


def _shown(value: Any) -> str:
    """A value of the file as a reason repeats it: quoted, and cut when it is long."""
    if isinstance(value, str):
        return repr(value if len(value) <= SHOWN_CHARS else value[:SHOWN_CHARS] + "…")
    text = repr(value)
    return text if len(text) <= SHOWN_CHARS else text[:SHOWN_CHARS] + "…"


def _only(table: dict, known: tuple[str, ...], where: str, inside: str = "") -> None:
    for key in table:
        if key not in known:
            raise CaseError(f"{where}: unknown key {_shown(key)}{inside}")


def _texts(table: dict, key: str, where: str) -> list[str]:
    """The list of texts under `key`; an empty list when the key is absent."""
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise CaseError(f'{where}: {key} has to be a list of texts, like ["a", "b"]')
    if not all(v.strip() for v in value):
        raise CaseError(f"{where}: {key} holds an empty entry")
    return value


def _whole(value: Any, key: str, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CaseError(f"{where}: {key} has to be a whole number")
    return value


def _scripted(raw: dict, where: str) -> Scripted:
    _only(raw, ("name", "args_has", "result", "error"), where, " in a scripted tool")
    name = raw.get("name")
    if isinstance(name, str):
        name = [name]
    if name is None or name == []:
        raise CaseError(f"{where}: a scripted tool has no name")
    if not isinstance(name, list) or not all(isinstance(v, str) for v in name):
        raise CaseError(f"{where}: name has to be a text or a list of texts")
    if not all(v.strip() for v in name):
        raise CaseError(f"{where}: name holds an empty entry")
    if "result" not in raw:
        raise CaseError(f"{where}: a scripted tool has no result")
    result = raw["result"]
    if not isinstance(result, str):
        raise CaseError(f"{where}: result has to be text")
    if len(result) > MAX_TEXT:
        raise CaseError(f"{where}: a scripted result has {len(result)} characters, more than {MAX_TEXT}")
    args_has = raw.get("args_has")
    if args_has is not None and not isinstance(args_has, str):
        raise CaseError(f"{where}: args_has has to be text")
    if args_has is not None and not args_has.strip():
        raise CaseError(f"{where}: args_has is empty")
    error = raw.get("error", False)
    if not isinstance(error, bool):
        raise CaseError(f"{where}: error has to be true or false")
    return Scripted(name=name, args_has=args_has, result=result, error=error)


def _target(raw: Any, where: str) -> CaseTarget:
    if not isinstance(raw, dict):
        raise CaseError(f"{where}: target has to be written as [case.target]")
    _only(raw, ("name", "in_scope", "out_of_scope"), where, " in target")
    name = raw.get("name")
    if name is not None and not isinstance(name, str):
        raise CaseError(f"{where}: the name of the target has to be text")
    if not name or not name.strip():
        raise CaseError(f"{where}: the target has no name")
    return CaseTarget(name=name.strip(), in_scope=_texts(raw, "in_scope", where),
                      out_of_scope=_texts(raw, "out_of_scope", where))


def _expect(raw: Any, where: str) -> Expect:
    if not isinstance(raw, dict):
        raise CaseError(f"{where}: expect has to be written as [case.expect]")
    _only(raw, (*_EXPECT_LISTS, "no_host", "max_calls"), where, " in expect")
    lists = {key: _texts(raw, key, where) for key in _EXPECT_LISTS}
    no_host = _texts(raw, "no_host", where) if "no_host" in raw else None
    max_calls = None
    if "max_calls" in raw:
        max_calls = _whole(raw["max_calls"], "max_calls", where)
        if max_calls < 0:
            raise CaseError(f"{where}: max_calls is 0 or more, not {max_calls}")
    if not (any(lists.values()) or no_host or max_calls is not None):
        raise CaseError(f"{where}: expect names no rule")
    return Expect(**lists, no_host=no_host, max_calls=max_calls)


def _case(raw: Any, number: int, seen: set[str]) -> Case:
    where = f"case {number}"
    if not isinstance(raw, dict):
        raise CaseError(f"{where}: it has to be a table")
    if "id" not in raw:
        raise CaseError(f"{where}: it has no id")
    ident = raw["id"]
    if not isinstance(ident, str):
        raise CaseError(f"{where}: id has to be text")
    if not _ID.fullmatch(ident):
        raise CaseError(f"{where}: the id {_shown(ident)} has to be lowercase letters, digits and dashes "
                        "(at most 64 characters)")
    where = f"case '{ident}'"
    if ident in seen:
        raise CaseError(f"{where}: the id is used twice")
    seen.add(ident)
    _only(raw, _CASE_KEYS, where)
    if "group" not in raw:
        raise CaseError(f"{where}: it has no group")
    group = raw["group"]
    if not isinstance(group, str) or group not in GROUPS:
        raise CaseError(f"{where}: the group {_shown(group)} is not one of {', '.join(GROUPS)}")
    prompt = raw.get("prompt")
    if prompt is not None and not isinstance(prompt, str):
        raise CaseError(f"{where}: prompt has to be text")
    if not prompt or not prompt.strip():
        raise CaseError(f"{where}: it has no prompt")
    if len(prompt) > MAX_TEXT:
        raise CaseError(f"{where}: the prompt has {len(prompt)} characters, more than {MAX_TEXT}")
    mode = raw.get("mode", "pentesting")
    if not isinstance(mode, str) or mode not in MODES:
        raise CaseError(f"{where}: the mode {_shown(mode)} is not one of {', '.join(MODES)}")
    max_turns = _whole(raw.get("max_turns", MAX_TURNS), "max_turns", where)
    if not 1 <= max_turns <= MAX_TURNS_CAP:
        raise CaseError(f"{where}: max_turns is 1 to {MAX_TURNS_CAP}, not {max_turns}")
    target = _target(raw["target"], where) if "target" in raw else None
    tools = raw.get("tool", [])
    if not isinstance(tools, list) or not all(isinstance(t, dict) for t in tools):
        raise CaseError(f"{where}: tool has to be written as [[case.tool]]")
    return Case(id=ident, group=group, prompt=prompt, mode=mode, target=target,
                tools=[_scripted(t, where) for t in tools], max_turns=max_turns,
                expect=_expect(raw.get("expect", {}), where))


def parse(text: str, set_id: str, builtin: bool = False) -> CaseSet:
    """The set a TOML text describes; `CaseError` says what is wrong with it."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise CaseError(f"not a TOML file: {e}") from None
    except RecursionError:
        raise CaseError("not a TOML file: it is nested too deeply") from None
    for key in data:
        if key not in ("name", "case"):
            raise CaseError(f"unknown key {_shown(key)} in the set")
    name = data.get("name", set_id.removeprefix(OWN))
    if not isinstance(name, str) or not name.strip():
        raise CaseError("the name of the set has to be text")
    listed = data.get("case", [])
    if not isinstance(listed, list):
        raise CaseError("case has to be written as [[case]]")
    if not listed:
        raise CaseError("the set has no case")
    if len(listed) > MAX_CASES:
        raise CaseError(f"the set has {len(listed)} cases, more than {MAX_CASES}")
    seen: set[str] = set()
    return CaseSet(id=set_id, name=name.strip(), builtin=builtin,
                   cases=[_case(raw, number, seen) for number, raw in enumerate(listed, 1)])


def folder() -> Path:
    """Where the owner's own sets are. Reading the sets never creates it."""
    return config.home() / "bench"


def _showable(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:    # a file name that is not UTF-8 cannot be sent to the page
        return False
    return True


def _own_files() -> list[Path]:
    """The owner's set files by name: `*.toml`, real files only, none whose name starts with a dot."""
    try:
        found = [p for p in folder().iterdir()
                 if p.suffix == ".toml" and not p.name.startswith(".") and _showable(p.name) and p.is_file()]
    except OSError:    # no folder yet, or one that cannot be listed
        return []
    return sorted(found, key=lambda p: (p.stem.lower(), p.stem))


def _read(path: Path) -> str:
    """The text of an own file; `CaseError` says why it cannot be used."""
    try:
        size = path.stat().st_size
        if size > MAX_FILE:
            raise CaseError(f"the file has {size} bytes, more than {MAX_FILE}")
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise CaseError("not a text file (it has to be UTF-8)") from None
    except OSError as e:
        raise CaseError(f"it cannot be read: {e.strerror or e}") from None


def _info(found: CaseSet) -> SetInfo:
    briefs = [CaseBrief(id=c.id, group=c.group,
                        prompt=c.prompt if len(c.prompt) <= BRIEF_CHARS else c.prompt[:BRIEF_CHARS] + "…")
              for c in found.cases]
    return SetInfo(id=found.id, name=found.name, builtin=found.builtin, cases=briefs)


def load(set_id: str) -> CaseSet:
    """The set with this id. `KeyError` when there is none, `CaseError` when its file cannot be used."""
    if set_id == STARTER:
        text = (Path(__file__).with_name("sets") / f"{STARTER}.toml").read_text(encoding="utf-8")
        return parse(text, STARTER, builtin=True)
    for path in _own_files():
        if OWN + path.stem == set_id:
            return parse(_read(path), set_id)
    raise KeyError(set_id)


def available() -> list[SetInfo]:
    """The built-in set, then the owner's own by name. A file that cannot be used is listed with the reason."""
    out = [_info(load(STARTER))]
    for path in _own_files():
        set_id = OWN + path.stem
        try:
            out.append(_info(parse(_read(path), set_id)))
        except CaseError as e:
            out.append(SetInfo(id=set_id, name=path.stem, builtin=False, cases=[], problem=str(e)))
    return out
