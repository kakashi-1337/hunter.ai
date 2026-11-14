"""Turn what a tool call names (a path, a URL, a host in a command) into what it really
touches. The resolved form is the only one that is checked, shown, and used."""
from __future__ import annotations

import asyncio
import fnmatch
import ipaddress
import os
import re
import socket
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
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
_NOT_TLDS = frozenset("sh py js ts txt json xml yml yaml md log html csv conf cfg out zip gz tar "
                      "php go rs c h".split())


def extract_hosts(command: str) -> list[HostUse]:
    """Best-effort hosts named in a shell string. Never resolved by HunterAI."""
    out: dict[tuple[str, int | None], HostUse] = {}

    def add(h: HostUse) -> None:
        out.setdefault((h.host, h.port), h)

    text = command.lower()
    for m in _URL.finditer(text):
        try:
            add(host_of_url(m.group(0)))
        except ToolError:
            pass
    rest = _URL.sub(" ", text)
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
    for m in _NAME_TOKEN.finditer(rest):
        name, last = m.group(1), m.group(1).rsplit(".", 1)[-1]
        if not (last.isalpha() and len(last) >= 2) or last in _NOT_TLDS:
            continue
        try:
            add(HostUse(normalize_host(name), int(m.group(2)) if m.group(2) else None))
        except ValueError:
            pass
    return list(out.values())


# ---------------------------------------------------------------- scope


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
    """`[scheme://]host|*.domain|ip|cidr[:port][/path-prefix]`; None when it fits no form."""
    t = re.sub(r"^[a-z][a-z0-9+.-]*://", "", (s or "").strip(), flags=re.I)
    if not t:
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
    port = None
    if network is None:
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
    return ScopeEntry(suffix=name, port=port, path=path) if suffix else \
        ScopeEntry(host=name, port=port, path=path)


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


def scope_verdict(target: Target | None, hosts: Sequence[HostUse],
                  url_path: str | None = None) -> ScopeVerdict:
    if target is None:
        return ScopeVerdict((), ())
    outs = [e for e in (target.scope.get("out") or []) if isinstance(e, str)]
    ins = [e for e in (target.scope.get("in") or []) if isinstance(e, str)]
    out = tuple(dict.fromkeys(
        h.host for h in hosts
        if any(matches(e, h, url_path, any_address=True, loose_path=True) for e in outs)))
    not_in: tuple[str, ...] = ()
    if ins:   # an empty in-list means only the out-list is enforced
        not_in = tuple(dict.fromkeys(
            h.host for h in hosts
            if not (_is_localhost_name(h.host) or is_loopback(h.host))   # never a scope matter
            and not any(matches(e, h, url_path) for e in ins)))
    return ScopeVerdict(out, not_in)
