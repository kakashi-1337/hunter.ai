"""Which line of a target's two scope lists decides for one host, address or URL, in words for the owner.

The permission check says "x is not in this target's in-scope list" and cannot say which line did that. This reads the
lists as that check reads them (tools/resolve.py gives the verdict; nothing here decides anything) and names the line,
what stands in the way, and what the program's own words would have to be for another answer. It only reads: no list is
changed, nothing is saved, and no name is looked up."""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from .tools import resolve
from .tools.base import HostUse, ToolError
from .types import Target

MAX_ASK = 2000
NEAR_SHOWN = 3      # lines that come close to the host and miss it: so many are spelled out, the rest are counted
NETS_SHOWN = 3
EXAMPLE = "Type one host, address or URL, like shop.example.com or https://shop.example.com/login."

_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_WITH_PORT = re.compile(r"^(\[[0-9a-f:.]+\]|[^:\[\]]+):(\d{1,5})$")


@dataclass(frozen=True)
class Explained:
    host: str
    port: int | None
    path: str | None
    verdict: str            # "in"; "out"; "not_in": asked about; "no_list": no In scope line; "local": this machine
    line: str | None        # the line that decides, as it is written: of In scope for "in", of Out of scope for "out"
    says: str
    command: dict | None    # {"verdict", "says"} where a command that names this address gets another answer than this
    hints: list[str]
    unreadable: list[dict]  # {"side", "line", "why"} for each line that fits no form and so is checked against nothing

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class _Line:
    text: str
    entry: resolve.ScopeEntry
    index: int

    @property
    def one(self) -> bool:
        """One host or one address, not a wildcard or a range: the lines that the two lists weigh differently."""
        e = self.entry
        return e.host is not None or (e.network is not None and e.network.num_addresses == 1)


def _clean(lines: Sequence[object]) -> list[str]:
    """The lines of a list as they are checked: trimmed, each once, in the order they stand."""
    return list(dict.fromkeys(x.strip() for x in lines if isinstance(x, str) and x.strip()))


def _read(lines: list[str]) -> list[_Line]:
    return [_Line(text, entry, i) for i, text in enumerate(lines) if (entry := resolve.parse_entry(text)) is not None]


def _bare(head: str) -> HostUse:
    port = None
    try:
        host = resolve.normalize_host(head)
    except ValueError:
        m = _WITH_PORT.match(head.lower())
        if not m or not 0 < int(m.group(2)) < 65536:
            raise
        host, port = resolve.normalize_host(m.group(1)), int(m.group(2))
    return HostUse(host, port)


def read_ask(text: object) -> tuple[HostUse, str | None]:
    """The host a typed text names, and its path when it brings one. A URL is read as web_fetch reads it. A host with
    no scheme before it gets no port and no path it did not bring: that is how a command names one."""
    t = text.strip() if isinstance(text, str) else ""
    if len(t) > MAX_ASK or re.search(r"\s", t):      # a space: more than one thing was typed, or a sentence pasted
        raise ValueError(EXAMPLE)
    try:
        if _SCHEME.match(t):
            host, address = resolve.host_of_url(t), t
        else:
            head, slash, _ = t.partition("/")
            host, address = _bare(head), ("http://x" + t[len(head):] if slash else None)
    except (ToolError, ValueError):
        raise ValueError(EXAMPLE) from None
    if address is None:
        return host, None
    try:
        return host, resolve.url_path(address)
    except ToolError as e:
        raise ValueError(f"The path of this address cannot be read ({e}). Type the host alone to check it.") from None


def _listed(lines: list[_Line]) -> str:
    texts = [line.text for line in lines]
    more = len(texts) - NETS_SHOWN
    return ", ".join(texts[:NETS_SHOWN]) + (f" and {more} more" if more > 0 else "")


def _near(lines: list[_Line], h: HostUse, path: str | None) -> list[str]:
    """Why each In scope line that comes close to this host does not cover it."""
    found = []
    for line in lines:
        e = line.entry
        if resolve.matches(line.text, HostUse(h.host, e.port)):      # its host, so the port or the path is what differs
            if e.port is not None and e.port != h.port:
                found.append(f"The In scope line {line.text} is only for port {e.port}, and this "
                             + (f"goes to port {h.port}." if h.port is not None else "names no port."))
            else:       # the port is the line's, so its path is not: a line with neither would have covered the host
                found.append(f"The In scope line {line.text} is only for {e.path} and what is under it, and this has the "
                             f"path {path}.")
        elif e.suffix is not None and h.host == e.suffix:
            found.append(f"The In scope line {line.text} covers the names under {e.suffix}, not {e.suffix} itself. If the "
                         f"program covers {e.suffix} too, add it on a line of its own.")
        elif e.host is not None and h.host.endswith("." + e.host):
            found.append(f"The In scope line {line.text} names that one host. If the program covers the names under it "
                         f"too, add *.{e.host} on a line of its own.")
    more = len(found) - NEAR_SHOWN
    if more > 0:
        found = [*found[:NEAR_SHOWN], f"{more} more In scope line{' comes' if more == 1 else 's come'} close in the same way."]
    return found


def _out_addresses(lines: list[_Line], h: HostUse) -> list[str]:
    nets = [line for line in lines if line.entry.network is not None]
    if resolve.is_ip(h.host) or not nets:
        return []
    return [(f"Out of scope lines that are addresses or networks ({_listed(nets)}) are checked against the addresses of a "
             "name once a tool that fetches a URL has looked it up. No name is looked up here, and none is for a command.")]


@dataclass(frozen=True)
class _Answer:
    verdict: str
    decides: _Line | None
    says: str
    hints: list[str]


def _decide(h: HostUse, path: str | None, in_text: list[str], out_text: list[str]) -> _Answer:
    """The verdict of the permission check for this host and path, the line behind it, and what else bears on it."""
    in_lines, out_lines = _read(in_text), _read(out_text)
    v = resolve.scope_verdict(Target(id="", name="", notes="", scope={"in": in_text, "out": out_text}, created_at=""), [h], path)
    in_hits = [line for line in in_lines if resolve.matches(line.text, h, path)]
    out_hits = [line for line in out_lines if resolve.matches(line.text, h, path, any_address=True, loose_path=True)]
    x = h.host
    hints: list[str] = []
    if v.out:
        # A line that names the host first: nothing on In scope lifts that one. Then a line for every path.
        decides = min(out_hits, key=lambda line: (not line.one, line.entry.path is not None, line.index))
        says = f"{x} is out of scope: the Out of scope line {decides.text} {'names' if decides.one else 'covers'} it."
        named = next((line for line in in_hits if line.one), None)
        if named is not None:      # it would have lifted the host over wider lines, so a line that names the host decided
            hints.append(f"The In scope line {named.text} names it too. A host named on both lists is out.")
        elif not decides.one and decides.entry.path is None:
            hints.append(f"If the program lists {x} itself as in scope, write {x} on a line of its own in In scope: a host "
                         "named there goes before a wider Out of scope line.")
        if path is None and all(line.entry.path is not None for line in out_hits):
            there = f"[{x}]" if ":" in x else x
            hints.append(f"That line is only for {decides.entry.path} and what is under it. A command names a host without "
                         f"a path, so every command that names {x} is asked about. To check one path, type the full "
                         f"address, like https://{there}/login.")
        if v.not_in:
            hints.append("It is not on the In scope list either.")
        return _Answer("out", decides, says, hints)
    if v.not_in:
        hints += _near(in_lines, h, path)
        nets = [line for line in in_lines if line.entry.network is not None]
        if nets and not resolve.is_ip(x):
            hints.append(f"In scope lines that are addresses or networks ({_listed(nets)}) cover a call that names the "
                         "address. No name is looked up for them before you are asked.")
        return _Answer("not_in", None, f"{x} is not on the In scope list, so a call that names it is asked about.", hints)
    if not in_text:
        hints.append("Auto-approve answers only for hosts on an In scope list: while this one is empty, it asks you about "
                     "every call that names a host.")
        return _Answer("no_list", None, f"The In scope list is empty, so only the Out of scope list is checked, and {x} is "
                       "not on it.", hints + _out_addresses(out_lines, h))
    if not in_hits:     # no line covers it and the check still has nothing to say: only this machine is treated so
        return _Answer("local", None, f"{x} is this machine. The In scope list is not asked about it.",
                       [("A call to this machine can still be asked about as a local address: that check is apart from the "
                         "scope lists.")])
    # Only wider Out of scope lines cover it, or it would be out: a line that names it lifted it, and that line is the
    # one to show. (An address line is among these only where the address itself was typed: no name is looked up.)
    lifted = bool(out_hits)
    decides = min(in_hits, key=lambda line: (lifted and not line.one, line.entry.path is not None, not line.one, line.index))
    if lifted:
        wide = min(out_hits, key=lambda line: (line.entry.path is not None, line.index))
        hints.append(f"The Out of scope line {wide.text} covers it as well. A host that In scope names goes before a wider "
                     "Out of scope line.")
    if path is None and all(line.entry.path is not None for line in in_hits):
        hints.append(f"That line is only for {decides.entry.path} and what is under it: a full address of {x} with another "
                     "path is asked about.")
    return _Answer("in", decides, f"{x} is in scope: the In scope line {decides.text} {'names' if decides.one else 'covers'} it.",
                   hints + _out_addresses(out_lines, h))


def explain(ins: Sequence[object], outs: Sequence[object], ask: object) -> Explained:
    """What the two lists say about one host, address or URL, as they are given: saved or not.

    A host alone is checked as a command names one. A full address is checked with its path, as web_fetch and the
    browser name one, and the answer says so when a command that names it gets another one. ValueError, with words for
    the owner, when the text names no host."""
    h, path = read_ask(ask)
    in_text, out_text = _clean(ins), _clean(outs)
    a = _decide(h, path, in_text, out_text)
    hints, command = list(a.hints), None
    if path is not None:
        bare = _decide(h, None, in_text, out_text)
        if bare.verdict != a.verdict:       # then it is in or out there: a path can only narrow what a line covers
            command = {"verdict": bare.verdict, "says": f"In a command, {bare.says}"}
            hints.append("A command is checked without the path of an address it names, so a line with a path counts for "
                         "the whole host there.")
    unreadable = [{"side": name, "line": line, "why": why}
                  for name, lines in (("in", in_text), ("out", out_text)) for line, why in resolve.unreadable(lines)]
    return Explained(h.host, h.port, path, a.verdict, a.decides.text if a.decides else None, a.says, command, hints, unreadable)
