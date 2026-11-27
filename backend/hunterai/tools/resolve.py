"""Turn what a tool call names (a path, a URL, a host in a command) into what it really
touches. The resolved form is the only one that is checked, shown, and used."""
from __future__ import annotations

import asyncio
import fnmatch
import ipaddress
import os
import re
import socket
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
import idna

from .. import config
from ..types import Target
from .base import HostUse, ToolError

Mode = Literal["read", "write"]

# ---------------------------------------------------------------- paths


def resolve_path(raw: str, base: Path) -> Path:
    if not raw or "\x00" in raw:
        raise ToolError("a path is required and cannot contain a NUL byte")
    p = Path(os.path.expanduser(raw))
    if not p.is_absolute():
        p = Path(base) / p
    real = os.path.realpath(p)
    if real[-1:].isspace():
        # Other programs (the chunker among them) strip it, so the file that is opened
        # would not be the file that was checked.
        raise ToolError("paths that end in whitespace are not supported")
    return Path(real)


def _real(p: Path | str) -> str:
    return os.path.realpath(os.path.expanduser(str(p)))


def is_inside(path: Path, root: Path) -> bool:
    a, b = _real(path), _real(root)
    return a == b or a.startswith(b.rstrip("/") + "/")


_PROC_SECRET = re.compile(r"^/proc/\d+/(task/\d+/)?(environ|mem)$")


def protected(path: Path, mode: Mode) -> str | None:
    """A reason when this path may never be used in this mode."""
    real = _real(path)
    if real == _real(config.env_file()):
        return "the HunterAI env file holds API keys and is never read or written by tools"
    if _PROC_SECRET.match(real):
        return f"{real} exposes process memory or environment"
    if mode == "write":
        db = _real(config.db_path())
        if real in (db, db + "-wal", db + "-shm"):
            return "the HunterAI database is not written by tools"
        if real == _real(config.mcp_file()):
            return "the MCP server list is edited by the owner only"
        if real == _real(config.home() / "mcp-pins.json"):
            return "the record of approved MCP tool definitions is changed by the owner only"
        if is_inside(Path(real), config.home() / "mcp"):
            return "installed MCP server code and its inspection records are changed by the owner only"
        if is_inside(Path(real), config.home() / "skills"):
            return "the files of installed skills are changed by the owner only"
        if is_inside(Path(real), config.PROJECT_ROOT / "anbu-system-one"):
            return "anbu-system-one is kept untouched as the reference"
        if is_inside(Path(real), config.chunker_path().parent):
            return "the folder of the file chunker MCP server is never modified"
    return None


SENSITIVE_NAMES = (".env", ".env.*", "*.pem", "*.key", "*.kdbx", "id_rsa*", "id_ed25519*",
                   ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".bash_history",
                   ".zsh_history")
_CRED_HOME_FILES = (".git-credentials", ".bash_history", ".zsh_history", ".pgpass", ".my.cnf",
                    ".vault-token")
_CRED_DIRS = (".ssh", ".gnupg", ".aws", ".kube", ".docker", ".claude", ".codex",
              ".config/gcloud", ".config/gh", ".password-store", ".local/share/keyrings",
              ".mozilla", ".config/google-chrome", ".config/chromium", ".azure",
              ".config/rclone", ".terraform.d", ".thunderbird", ".config/BraveSoftware",
              # snap-packaged browsers and mail keep their profiles here
              "snap/firefox", "snap/chromium", "snap/thunderbird", "snap/brave")
_CRED_NAME_GLOBS = (".env", ".env.*", "id_rsa*", "id_ed25519*", ".netrc", ".npmrc", ".pypirc",
                    "*.pem", "*.key", "*.kdbx")
_CODE_HOME_FILES = (".bashrc", ".bash_profile", ".profile", ".zshrc", ".zshenv", ".zprofile",
                    ".bash_logout", ".gitconfig")
_CODE_DIRS = (".config/autostart", ".config/systemd", ".local/bin", ".config/fish")


def sensitive_dirs() -> list[Path]:
    return [Path.home() / d for d in _CRED_DIRS]


def sensitive(path: Path, mode: Mode = "read") -> str | None:
    """A reason to ask first: credentials (read and write), or something that runs later
    or is part of HunterAI (write only)."""
    real = Path(_real(path))
    home = Path.home()
    creds = (str(real) == _real(config.mcp_file())
             or any(str(real) == _real(home / f) for f in _CRED_HOME_FILES)
             or any(is_inside(real, d) for d in sensitive_dirs())
             or any(fnmatch.fnmatchcase(real.name, g) for g in _CRED_NAME_GLOBS))
    if creds:
        return f"{real} usually holds credentials"
    if mode != "write":
        return None
    root = config.PROJECT_ROOT
    code = (".git" in real.parts
            or any(is_inside(real, root / d) for d in ("backend", "scripts", "frontend"))
            or (is_inside(real, config.home()) and not is_inside(real, config.home() / "work"))
            or any(str(real) == _real(home / f) for f in _CODE_HOME_FILES)
            or any(is_inside(real, home / d) for d in _CODE_DIRS))
    return f"{real} can run code later or is part of HunterAI" if code else None


# ---------------------------------------------------------------- hosts

_LEGACY_V4 = re.compile(r"^(0x[0-9a-f]+|\d+)(\.(0x[0-9a-f]+|\d+)){0,3}$")
_NAME = re.compile(r"^[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?)*$")


def _canonical_ip(text: str) -> str | None:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        if not _LEGACY_V4.match(text):
            return None
        try:   # 2130706433, 0x7f.0.0.1, 0177.0.0.1: what the C resolver would connect to
            ip = ipaddress.ip_address(socket.inet_aton(text))
        except OSError:
            return None
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return str(ip)


def normalize_host(raw: str) -> str:
    h = (raw or "").strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    h = h.rstrip(".")
    if not h:
        raise ValueError("empty host")
    ip = _canonical_ip(h)
    if ip:
        return ip
    if not h.isascii():
        try:
            h = idna.encode(h, uts46=True).decode("ascii")
        except idna.IDNAError as e:
            raise ValueError(f"invalid host: {raw!r}") from e
    if not _NAME.match(h):
        raise ValueError(f"invalid host: {raw!r}")
    return h


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


_LOOPBACK = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "::1/128", "0.0.0.0/32", "::/128")]
_LOCAL = _LOOPBACK + [ipaddress.ip_network(n) for n in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16", "100.64.0.0/10",
    "fc00::/7", "fe80::/10")]


def _in(ip: str, nets) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr.version == n.version and addr in n for n in nets)


def is_loopback(ip: str) -> bool:
    return _in(ip, _LOOPBACK)


def is_local(ip: str) -> bool:
    return _in(ip, _LOCAL)


def _is_localhost_name(host: str) -> bool:
    return host in ("localhost", "ip6-localhost") or host.endswith(".localhost")


def host_of_url(url: str) -> HostUse:
    """The host a URL really names. Userinfo never counts. No lookup."""
    try:
        parts = urlsplit(url)
        scheme, hostname, port = parts.scheme.lower(), parts.hostname, parts.port
    except ValueError as e:
        raise ToolError(f"invalid URL: {e}") from None
    if scheme not in ("http", "https"):
        raise ToolError("only http and https URLs are supported")
    if not hostname:
        raise ToolError("the URL has no host")
    try:
        host = normalize_host(hostname)
    except ValueError as e:
        raise ToolError(str(e)) from None
    return HostUse(host, port or (443 if scheme == "https" else 80), (host,) if is_ip(host) else ())


_ENCODED = re.compile(r"%2e|%2f|%5c|\\", re.I)


def url_path(url: str) -> str:
    """The path the request will carry (dot segments removed, as httpx sends it)."""
    if _ENCODED.search(re.split(r"[?#]", url, maxsplit=1)[0]):
        raise ToolError("encoded dots or slashes in a URL path are not supported")
    try:
        return httpx.URL(url).path or "/"
    except (httpx.InvalidURL, ValueError, UnicodeError) as e:
        raise ToolError(f"invalid URL: {e}") from None


async def _system_getaddrinfo(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return [i[4][0] for i in infos]


_getaddrinfo = _system_getaddrinfo   # tests replace this; nothing else looks names up


async def resolve_ips(host: str) -> tuple[str, ...]:
    """Addresses of a host; the first one is where a request is sent."""
    if is_ip(host):
        return (host,)
    if _is_localhost_name(host):
        return ("127.0.0.1",)
    try:
        found = await _getaddrinfo(host)
    except OSError:
        raise ToolError(f"cannot resolve {host}") from None
    ips = list(dict.fromkeys(_canonical_ip(a) or a for a in found))
    if not ips:
        raise ToolError(f"cannot resolve {host}")
    v4 = next((a for a in ips if "." in a and ":" not in a), None)
    if v4:
        ips.remove(v4)
        ips.insert(0, v4)
    return tuple(ips)


def is_own_api(h: HostUse) -> bool:
    """True when the call addresses HunterAI's own API, in any host form."""
    if h.port not in (config.port(), config.DEV_PORT):
        return False
    return (_is_localhost_name(h.host) or is_loopback(h.host)
            or any(is_loopback(ip) for ip in h.ips))


_URL = re.compile(r"https?://[^\s'\"<>|;&)]+", re.I)
_V4_TOKEN = re.compile(r"(?<![\w.-])(\d{1,3}(?:\.\d{1,3}){3})(?::(\d{1,5}))?(?![\w.-])")
_LOCAL_TOKEN = re.compile(r"(?<![\w.-])(localhost|ip6-localhost|\[::1\])(?::(\d{1,5}))?(?![\w.-])")
_NAME_TOKEN = re.compile(r"(?<![\w.@-])([a-z0-9-]+(?:\.[a-z0-9-]+)+)(?::(\d{1,5}))?(?![\w.-])")
# File endings. Some are top-level domains as well (.zip, .mov, .map, .java, .py, .sh, .rs, .md): a name that ends
# so is far more often a file of the work than a host, and a host of that ending is still found in a URL.
_NOT_TLDS = frozenset("sh py js ts txt json xml yml yaml md log html csv conf cfg out zip gz tar "
                      "php go rs c h map java mov".split())
# The last part of a host name: a top-level domain of the internet (tools/tlds.txt says where the list is from),
# or one of the endings people give the hosts of their own networks and labs.
TLDS = frozenset(line for line in Path(__file__).with_name("tlds.txt").read_text().split() if line.isalpha())
PRIVATE_ENDINGS = frozenset(("local", "internal", "lan", "home", "corp", "localdomain", "intranet", "private", "test",
                             "example", "invalid", "onion", "alt", "htb", "thm", "lab"))
# Where program text stands in a command. Its names are the properties of its objects (object.keys, self.name),
# and thousands of them end like a domain: in 187 source files of this project 906 such names did, 613 of them
# after every name that is called or assigned to was left out. A host a program talks to stands in its quotes.
_HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)([a-z_][a-z0-9_]*)\1")
# A program that runs code, with nothing but its own switches behind it: "python3 -u", not "python3 exploit.py",
# whose -c and whose lines on standard input are the script's own.
_RUNS_CODE = r"(?:^|[\s;|&(])(?:python[\d.]*|node|nodejs|bun|deno|perl|ruby|php|lua|rscript)(?:[ \t]+-[\w=.:,-]+)*[ \t]+"
_CODE_FLAG = re.compile(_RUNS_CODE + r"(?:-[a-z]{0,3}[cepr]|--eval|--print|eval)[ \t]*$")       # in front of a quoted text
_CODE_FED = re.compile(_RUNS_CODE.removesuffix(r"[ \t]+") + r"(?:[ \t]+-)?[ \t]*$")              # in front of <<
_CODE_FILE = re.compile(r">[ \t]*['\"]?[^\s'\"<>|;&]+\.(?:py|js|mjs|cjs|ts|tsx|jsx|rb|pl|php|go|rs|java|c|cc|cpp|cs|lua|kt|swift)"
                        r"(?![\w.-])")                                                           # the line writes a source file
# The file a program is given to run stands first behind it and its switches: "perl nikto.pl", "bash deploy.app".
_RUNS_FILE = re.compile(r"(?:^|[\s;|&('\"])(?:python[\d.]*|node|nodejs|bun|deno|perl|ruby|php|lua|rscript|bash|sh|zsh|dash|ksh)"
                        r"(?:[ \t]+-[\w=.:,-]+)*[ \t]+")
# The name of a setting stands first behind "git config" and its switches: user.email, core.editor.
_GIT_SETTING = re.compile(r"(?:^|[\s;|&('\"])git[ \t]+config(?:[ \t]+-[\w=.:,-]+)*[ \t]+")
# user@host names a host where a command logs into it, and in git's user@host:path. Anywhere else it is an email
# address: the user name of a login form, the author of a commit.
_AT_HOST = re.compile(r"(?<![\w.@-])[\w.+-]*\w@([a-z0-9-]+(?:\.[a-z0-9-]+)+)(?![\w.-])")
_LOGS_IN = re.compile(r"(?:^|[\s/'\"(])(?:ssh|autossh|scp|sftp|rsync|mosh|ssh-copy-id|telnet|ftp|lftp)[ \t]")
_GIT = re.compile(r"(?:^|[\s/'\"(])git[ \t]")
_NEXT_COMMAND = re.compile(r"[|;&\n]")
_CALL = re.compile(r"(?<![$\w.])[a-z_][\w.]*\((?!\))")       # a name with a bracket behind it that holds something
_ARROW = re.compile(r"=>")
CALLS_OF_A_PROGRAM = 2      # in a quoted text or a here-document that no known program runs: from here on it is one


def _regions(text: str) -> list[tuple[int, int, str, bool]]:
    """What a command quotes: (start, end, how it is quoted, whether a program that runs code was given it).
    How: ' or " for a quoted text of the shell, "<" for the lines of a here-document. Read as a shell would read
    them, as far as that is needed here: a backslash keeps the character behind it out of it."""
    out, i, n = [], 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\":
            i += 2
        elif ch == "'":
            end = text.find("'", i + 1)
            end = n if end < 0 else end
            out.append((i + 1, end, "'", bool(_CODE_FLAG.search(text[max(0, i - 200):i]))))
            i = end + 1
        elif ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append((i + 1, min(j, n), '"', bool(_CODE_FLAG.search(text[max(0, i - 200):i]))))
            i = j + 1
        elif text.startswith("<<", i) and not text.startswith("<<<", i) and (m := _HEREDOC.match(text, i)):
            line_end = text.find("\n", m.end())
            if line_end < 0:
                break
            line_start = text.rfind("\n", 0, i) + 1
            close = re.compile(r"^[ \t]*" + re.escape(m.group(2)) + r"[ \t]*$", re.MULTILINE).search(text, line_end + 1)
            end = close.start() if close else n
            given = bool(_CODE_FED.search(text[line_start:i]) or _CODE_FILE.search(text[line_start:line_end]))
            out.append((line_end + 1, end, "<", given))
            i = close.end() if close else n
        else:
            i += 1
    return out


def _programs(text: str, regions: list[tuple[int, int, str, bool]]) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """(the program texts of a command, the stretches of them that stand outside the program's own quotes). A
    name in such a stretch is a name of the program, not a host. Program text: what is quoted for a program that
    runs code (python -c, node -e), a here-document for one or into a source file, and any quoted text or
    here-document that calls CALLS_OF_A_PROGRAM things or has an arrow function."""
    programs: list[tuple[int, int]] = []
    bare: list[tuple[int, int]] = []
    for start, end, how, given in regions:
        body = text[start:end]
        if not (given or len(_CALL.findall(body)) >= CALLS_OF_A_PROGRAM or _ARROW.search(body)):
            continue
        programs.append((start, end))
        inside, at, i = None, start, start      # inside: the quote of the program that is open; at: where bare text began
        while i < end:
            ch = text[i]
            escaped = ch == "\\" and i + 1 < end
            quote = text[i + 1] if escaped and how == '"' and text[i + 1] == '"' else (None if escaped else ch if ch in "'\"`" else None)
            if quote is not None and inside is None:
                bare.append((at, i))
                inside = quote
            elif quote is not None and quote == inside:
                inside, at = None, i + (2 if escaped else 1)
            elif ch == "\n" and inside in ("'", '"'):
                inside, at = None, i            # a quote that was never closed on its line (an apostrophe in a comment)
            i += 2 if escaped else 1
        if inside is None:
            bare.append((at, end))
    return programs, bare


def _redirected(text: str, at: int) -> bool:
    """Whether the word at `at` stands behind > or <: what a command writes to or reads from. Not behind <<<,
    which hands the program the word itself."""
    i = at - 1
    while i >= 0 and text[i] in " \t":
        i -= 1
    return i >= 0 and (text[i] == ">" or (text[i] == "<" and not text.startswith("<", i - 1)))


def extract_hosts(command: str) -> list[HostUse]:
    """Best-effort hosts named in a shell string. Never resolved by HunterAI.

    A URL and an address count wherever they stand. A bare name (shop.acme.dev) counts when it ends like a host
    (a top-level domain, an ending of a private network, or any ending with a port behind it), is not called
    (os.open(...)), and does not stand in program text outside the program's own quotes. It does not count where
    the command itself says it is something else: outside quotes the last part of a path (out/app.page) and the
    file a command writes to or reads from (> scan.dev); the file an interpreter runs (perl nikto.pl); the name
    of a setting (user.email=..., git config user.name). A name behind user@ counts where a command logs into it."""
    out: dict[tuple[str, int | None], HostUse] = {}

    def add(h: HostUse) -> None:
        out.setdefault((h.host, h.port), h)

    text = command.lower()
    for m in _URL.finditer(text):
        try:
            add(host_of_url(m.group(0)))
        except ToolError:
            pass
    rest = _URL.sub(lambda m: " " * len(m.group(0)), text)      # the same length: places in it are places in the command
    for m in _V4_TOKEN.finditer(rest):
        ip = _canonical_ip(m.group(1))
        if ip:
            add(HostUse(ip, int(m.group(2)) if m.group(2) else None, (ip,)))
    for m in _LOCAL_TOKEN.finditer(rest):
        port = int(m.group(2)) if m.group(2) else None
        if m.group(1) == "[::1]":
            add(HostUse("::1", port, ("::1",)))
        else:
            add(HostUse(m.group(1), port))
    regions = _regions(text)
    programs, bare = _programs(text, regions)
    first_word = {x.end() for pattern in (_RUNS_FILE, _GIT_SETTING) for x in pattern.finditer(rest)}
    for m in _NAME_TOKEN.finditer(rest):
        name, last = m.group(1), m.group(1).rsplit(".", 1)[-1]
        if not (last.isalpha() and len(last) >= 2) or last in _NOT_TLDS:
            continue
        if not (last in TLDS or last in PRIVATE_ENDINGS or m.group(2)):
            continue                                            # shot.png, nginx.service, object.keys
        if rest.startswith("(", m.end()) or any(a <= m.start() < b for a, b in bare):
            continue                                            # re.search(...), and self.name in a program
        if rest.startswith("{", m.start() - 1) and any(a <= m.start() < b for a, b in programs):
            continue                                            # f"{self.name}" and `${user.id}`: put into a text, not named in it
        if rest.startswith("=", m.end()) or m.start() in first_word:
            continue                                            # user.name=..., git config user.email; perl nikto.pl
        # What the shell itself reads as a file. Not in a quoted text, where / and > are the text's own:
        # sed 's/old.acme.io/new.acme.io/', curl -d '<host>evil.dev</host>'.
        if not any(a <= m.start() < b for a, b, _, _ in regions):
            if rest.startswith("/", m.start() - 1) and not rest.startswith("//", max(0, m.start() - 2)):
                continue                                        # out/app.page: the last part of a path (//host/share is a host)
            if _redirected(rest, m.start()):
                continue                                        # > scan.dev: what the command writes to
        try:
            add(HostUse(normalize_host(name), int(m.group(2)) if m.group(2) else None))
        except ValueError:
            pass
    for m in _AT_HOST.finditer(rest):
        began = max((x.end() for x in _NEXT_COMMAND.finditer(rest, 0, m.start())), default=0)
        command = rest[began:m.start()]                          # the command this stands in, as far as it has come
        if _LOGS_IN.search(command) or (_GIT.search(command) and rest.startswith(":", m.end())):
            try:
                add(HostUse(normalize_host(m.group(1))))
            except ValueError:
                pass
    return list(out.values())


# ---------------------------------------------------------------- scope


HOST_CHARS = 20_000      # of one text of a call's arguments that are searched for hosts: the finder slows down more
#                          than in step with the length (100,000 characters take a tenth of a second)


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


def hosts_in(arguments: Any) -> list[HostUse]:
    """Every host among the texts of a call's arguments, in their order, each text read as a command is read
    (extract_hosts). For a call whose tool does not say where it goes."""
    return [host for text in _texts(arguments) for host in extract_hosts(text[:HOST_CHARS])]


@dataclass(frozen=True)
class ScopeEntry:
    host: str | None = None          # exact name
    suffix: str | None = None        # "*.domain": any subdomain, not the apex
    network: ipaddress.IPv4Network | ipaddress.IPv6Network | None = None
    port: int | None = None
    path: str | None = None


def _network(text: str):
    try:
        return ipaddress.ip_network(text, strict=False)
    except ValueError:
        return None


def parse_entry(s: str) -> ScopeEntry | None:
    """`[scheme://]host|*.domain|ip|cidr[:port][/path-prefix]`; None when it fits no form.

    A `*` is read in three places and nowhere else: as the first label (`*.example.com`), for any
    port (`:*`), and alone at the end of a path (`/api/*`: everything under it). A line that cannot
    be read matches nothing, so `unreadable` exists to tell the owner about it."""
    t = re.sub(r"^[a-z][a-z0-9+.-]*://", "", (s or "").strip(), flags=re.I)
    if not t or re.search(r"\s", t):
        return None
    head, slash, tail = t.partition("/")
    head = head.lower()          # hosts are case-insensitive; a path keeps its case
    path: str | None = ("/" + tail) if slash else None
    network = None
    if slash:   # "10.0.0.0/24[/path]" or "fd00::/8": the first segment may be a prefix length
        first, _, more = tail.partition("/")
        if first.isdigit() and _canonical_ip(head.strip("[]")) and _network(f"{head.strip('[]')}/{first}"):
            network = _network(f"{head.strip('[]')}/{first}")
            path = ("/" + more) if more else None
    if path is not None:
        if path.endswith("/*"):
            path = path[:-2]
        if "*" in path:
            return None
    port = None
    if network is None:
        if head.endswith(":*"):
            head = head[:-2]
        m = re.match(r"^(\[[0-9a-f:.]+\]|[^:]+):(\d{1,5})$", head)
        if m:
            head, port = m.group(1), int(m.group(2))
        bare = head.strip("[]")
        ip = _canonical_ip(bare)
        if ip:
            network = _network(ip)
    if path in ("/", ""):
        path = None
    if network is not None:
        return ScopeEntry(network=network, port=port, path=path)
    suffix = head.startswith("*.")
    name = head[2:] if suffix else head
    try:
        name = normalize_host(name)
    except ValueError:
        return None
    if is_ip(name) or "*" in name:
        return None
    if name.rpartition(".")[2].isdigit():     # "10.0.0.1-10.0.0.50", "999.1.1.1": numbers that are no address, not a name
        return None
    return ScopeEntry(suffix=name, port=port, path=path) if suffix else \
        ScopeEntry(host=name, port=port, path=path)


_ADDRESS_RANGE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}-\d{1,3}(\.\d{1,3}){0,3}$|^\d{1,3}(\.(\d{1,3}|\*)){3}$")


def why_unreadable(line: str) -> str | None:
    """What is wrong with a scope line that fits no form, in words for the owner. None when it is read."""
    t = (line or "").strip()
    if parse_entry(t) is not None:
        return None
    body = re.sub(r"^[a-z][a-z0-9+.-]*://", "", t, flags=re.I)
    if _ADDRESS_RANGE.match(re.sub(r"\s*-\s*", "-", body)):
        return "write a range of addresses as networks, like 10.0.0.0/26"
    if re.search(r"[\s,;()]", body):
        return "one host, address or network on a line, with nothing after it"
    if "*" in body:
        return "a * is read as the first label (*.example.com), as any port (:*), or alone at the end of a path (/api/*)"
    return "not a host, an address or a network"


def unreadable(lines: Sequence[object]) -> list[tuple[str, str]]:
    """(line, what is wrong with it) for each line of a scope list that `parse_entry` cannot read, once each."""
    found: dict[str, str] = {}
    for raw in lines:
        line = raw.strip() if isinstance(raw, str) else ""
        if line and line not in found:
            why = why_unreadable(line)
            if why:
                found[line] = why
    return list(found.items())


def _loose(path: str) -> str:
    return re.sub(r"/{2,}", "/", path).lower()


def matches(entry: str, h: HostUse, url_path: str | None = None, *,
            any_address: bool = False, loose_path: bool = False) -> bool:
    """`loose_path` compares the path without case and with repeated slashes collapsed: for
    the out-of-scope list, where a near miss must still warn."""
    e = parse_entry(entry)
    if e is None:
        return False
    if e.port is not None and e.port != h.port:
        return False
    if e.network is not None:
        if is_ip(h.host):
            candidates = [h.host]
        else:   # a name: the address the request goes to, or any of them
            candidates = list(h.ips) if any_address else list(h.ips[:1])
        if not any(_in(ip, [e.network]) for ip in candidates):
            return False
    elif e.host is not None:
        if h.host != e.host:
            return False
    elif not h.host.endswith("." + e.suffix):
        return False
    if e.path is not None and url_path is not None:
        prefix, wanted = e.path, url_path
        if loose_path:
            prefix, wanted = _loose(prefix), _loose(wanted)
        if not (wanted == prefix or wanted.startswith(prefix.rstrip("/") + "/")):
            return False
    return True


@dataclass(frozen=True)
class ScopeVerdict:
    out: tuple[str, ...]       # on the target's out-of-scope list
    not_in: tuple[str, ...]    # not on its in-scope list
    unread: tuple[str, ...] = ()   # out-of-scope lines that cannot be read: no host was checked against them


def _names_one(entry: str) -> bool:
    """One host or one address, not a wildcard or a range."""
    e = parse_entry(entry)
    return e is not None and (e.host is not None or (e.network is not None and e.network.num_addresses == 1))


def _excluded(h: HostUse, url_path: str | None, ins: Sequence[str], outs: Sequence[str]) -> bool:
    """A program that excludes `*.example.com` and lists `shop.example.com` means the shop: the host
    the in-scope list names beats the wider exclusion. Named on both lists, it is out."""
    hit = [e for e in outs if matches(e, h, url_path, any_address=True, loose_path=True)]
    if not hit or any(_names_one(e) for e in hit):
        return bool(hit)
    for e in ins:
        named = parse_entry(e)
        if named is None or not _names_one(e) or not matches(e, h, url_path):
            continue
        if named.host is not None or is_ip(h.host):   # a name that only resolves to the address was not named
            return False
    return True


def scope_verdict(target: Target | None, hosts: Sequence[HostUse],
                  url_path: str | None = None) -> ScopeVerdict:
    if target is None:
        return ScopeVerdict((), ())
    outs = [e for e in (target.scope.get("out") or []) if isinstance(e, str)]
    ins = [e for e in (target.scope.get("in") or []) if isinstance(e, str)]
    out = tuple(dict.fromkeys(h.host for h in hosts if _excluded(h, url_path, ins, outs)))
    not_in: tuple[str, ...] = ()
    if ins:   # an empty in-list means only the out-list is enforced
        not_in = tuple(dict.fromkeys(
            h.host for h in hosts
            if not (_is_localhost_name(h.host) or is_loopback(h.host))   # never a scope matter
            and not any(matches(e, h, url_path) for e in ins)))
    # An in-scope line that cannot be read only fails to include, and its hosts ask as "not in scope".
    # An out-of-scope line that cannot be read fails to exclude, and nothing else would say so.
    unread = tuple(line for line, _ in unreadable(outs)) if hosts else ()
    return ScopeVerdict(out, not_in, unread)
