"""What a program's own help says about a flag it was given and does not know.

The instructions tell a model to ask a program for its flags and not to invent any. A local model was seen to answer
"flag provided but not defined" with two more flags from memory. So the help comes to the model: when the output of a
command says that a program of the owner's tool folder does not know a flag, HunterAI asks that program for its help
itself and adds the lines closest to the flag to the result.

Only a program of that folder is asked, with the switch that prints its help and nothing else, with no input, and
once for each file as it is now. Nothing is looked up for a command whose output names no unknown flag."""
from __future__ import annotations

import asyncio
import difflib
import os
import re
import signal
import tempfile
from pathlib import Path

MAX_FLAGS = 3            # unknown flags of one output that are looked up
CLOSE = 6                # lines of a help given for one flag
LINE = 200               # characters of one of them
SPELLED = 0.7            # how alike two names are spelled before one is taken for the other (0 to 1)
HELP_SECONDS = 5.0       # a help that takes longer is not waited for
HELP_BYTES = 256 * 1024
_FLAG = r"-{1,2}[A-Za-z0-9][\w-]*"
# What a program says of a flag it does not know, by the family of its flag parser, with the switch that prints the
# help of a program of that family. A single letter is a flag of one dash.
SAID = (
    (re.compile(rf"flag provided but not defined: ({_FLAG})"), "-h"),              # Go's flag package
    (re.compile(rf"unknown flag: ({_FLAG})"), "--help"),                           # pflag, cobra
    (re.compile(r"unknown shorthand flag: '([A-Za-z0-9])'"), "--help"),
    (re.compile(rf"unrecognized arguments?: ({_FLAG})"), "-h"),                    # argparse
    (re.compile(rf"unrecogni[sz]ed option '?({_FLAG})"), "--help"),                # getopt
    (re.compile(r"invalid option -- '?([A-Za-z0-9])"), "--help"),
    (re.compile(rf"[Nn]o such option:? '?({_FLAG})"), "--help"),                   # click, optparse
    (re.compile(rf"unexpected argument '({_FLAG})'"), "--help"),                   # clap
)
_ENTRY = re.compile(rf"^[ \t]*({_FLAG}.*)$")
_GAP = re.compile(r"\s{2,}|\t")              # between the flags of a line and what the line says of them
_helps: dict[tuple, str] = {}


def unknown(output: str) -> list[tuple[str, str]]:
    """(flag, the switch that prints the help) for each flag the output says a program does not know: in the order
    they stand there, each once, the first few."""
    found = sorted((m.start(), m.group(1), switch) for said, switch in SAID for m in said.finditer(output))
    out: list[tuple[str, str]] = []
    for _, flag, switch in found:
        flag = flag if flag.startswith("-") else f"-{flag}"
        if flag not in [f for f, _ in out]:
            out.append((flag, switch))
    return out[:MAX_FLAGS]


def _entries(help_text: str) -> list[tuple[list[str], str]]:
    """(the flags of an entry, its text) for each line of a help that starts with a flag. A description that stands on
    the line after its flag, as Go's own flag package prints it, belongs to the entry."""
    lines, out, i = help_text.splitlines(), [], 0
    while i < len(lines):
        m = _ENTRY.match(lines[i])
        i += 1
        if not m:
            continue
        text = m.group(1).rstrip()
        parts = _GAP.split(text, maxsplit=1)
        if len(parts) == 1 and i < len(lines) and lines[i].strip() and lines[i][:1] in " \t" and not _ENTRY.match(lines[i]):
            text = f"{text}: {lines[i].strip()}"
            i += 1
        out.append((re.findall(rf"(?<![\w-])({_FLAG})", parts[0]), text))
    return out


def _words(name: str) -> list[str]:
    return [w for w in re.split(r"[-_]", name) if w]


def closest(help_text: str, flag: str) -> list[str]:
    """The entries of a help whose flags are nearest to `flag`, nearest first: those that share a word of its name,
    and those spelled nearly as it is. Empty when the help has none."""
    name = flag.lstrip("-").lower()
    words = [w for w in _words(name) if len(w) >= 3]
    scored = []
    for n, (flags, text) in enumerate(_entries(help_text)):
        names = [f.lstrip("-").lower() for f in flags]
        if not names:
            continue
        spelled = max(difflib.SequenceMatcher(None, name, other).ratio() for other in names)
        shared = sum(1 for w in words if any(w in _words(other) for other in names))
        if shared or spelled >= SPELLED:
            scored.append((-(shared + spelled), n, text[:LINE]))
    return [text for _, _, text in sorted(scored)[:CLOSE]]


def forget() -> None:
    """Drop the helps that were read. For tests."""
    _helps.clear()


async def _ask(program: Path, switch: str) -> str:
    """What `program switch` prints, or "" when it does not come in time or cannot be run. No input, the environment
    every command is given (no keys), a folder of its own, and its whole process group ended afterwards."""
    from . import procs

    with tempfile.TemporaryDirectory(prefix="hunterai-help-") as cwd:
        try:
            proc = await asyncio.create_subprocess_exec(
                str(program), switch, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, env=procs.child_env(), cwd=cwd, start_new_session=True)
        except OSError:
            return ""

        async def read() -> bytes:
            got = bytearray()
            while len(got) < HELP_BYTES:
                part = await proc.stdout.read(65536)
                if not part:
                    break
                got += part
            return bytes(got[:HELP_BYTES])
        try:
            raw = await asyncio.wait_for(read(), HELP_SECONDS)
        except TimeoutError:
            raw = b""
        finally:
            if proc.returncode is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
            await proc.wait()
    return raw.decode("utf-8", "replace")


async def help_of(program: Path, switch: str) -> str:
    """The help of a program, asked for once for each file as it is now."""
    try:
        st = program.stat()
    except OSError:
        return ""
    key = (str(program), st.st_mtime_ns, st.st_size, switch)
    if key not in _helps:
        _helps[key] = await _ask(program, switch)
    return _helps[key]


async def note(runs: list[tuple[str, str, Path]], output: str) -> str:
    """The lines for the end of a command's result when its output says a program does not know a flag. `runs`: (the
    program, its arguments as they are written, its file) for each program of the tool folder the command runs. A
    flag is laid to the program whose arguments hold it, or to the only program there is. "" when there is nothing
    to say."""
    lines = []
    for flag, switch in unknown(output) if runs else []:
        given = [r for r in runs if re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", r[1])] or (runs if len(runs) == 1 else [])
        if not given:
            continue
        program, _, path = given[0]
        ask = f"{program} {switch}"
        text = await help_of(path, switch)
        close = closest(text, flag)
        if not text.strip():
            lines.append(f"[{program} does not know the flag {flag}. Do not try another guess: read its help first, with: {ask}]")
        elif close:
            lines.append(f"[{program} does not know the flag {flag}. Its own help ({ask}) has these that are close:\n"
                         + "".join(f"  {entry}\n" for entry in close)
                         + f"Use a flag only as the help writes it; read all of it with: {ask}]")
        else:
            lines.append(f"[{program} does not know the flag {flag}, and its own help ({ask}) has no flag close to it. "
                         f"Do not try another guess: read the help first, with: {ask}]")
    return "".join(f"\n{line}" for line in lines)
