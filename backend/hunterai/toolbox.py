"""The owner's tool folders: `bin/` in the HunterAI folder, and `~/.hunterai/bin`. A program
(or a link to one) put in either is on the PATH of every command the shell tool runs, ahead
of everything else, and the model is told its name. The first is next to the code, where it
is easy to find, and is not part of the repository (git ignores it); a name in both is
taken from the first. HunterAI never downloads a program into them by itself: what runs
from there is what the owner chose to put there."""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import config

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
OWN = {"caido-cli"}          # HunterAI's own helpers, not tools for the model
MAX_LISTED = 60

# What the well-known ones are for: the model picks better with a word about each.
KNOWN = {
    "nuclei": "vulnerability scanner driven by templates",
    "httpx": "probes hosts for live HTTP services, titles, status, technologies",
    "subfinder": "passive subdomain enumeration",
    "katana": "crawler that lists URLs and endpoints, JavaScript included",
    "naabu": "port scanner",
    "dnsx": "DNS resolver and record lookups in bulk",
    "tlsx": "TLS certificate and configuration grabber",
    "cdncheck": "says whether an address belongs to a CDN, cloud, or WAF",
    "asnmap": "maps organisations and ASNs to network ranges",
    "mapcidr": "CIDR range arithmetic",
    "alterx": "subdomain wordlist permutations",
    "shuffledns": "subdomain brute force and resolution through massdns",
    "uncover": "queries Shodan, Censys, FOFA and similar for exposed hosts",
    "chaos": "ProjectDiscovery's subdomain dataset client",
    "interactsh-client": "out-of-band interaction catcher (DNS, HTTP callbacks)",
    "notify": "sends output to chat services",
    "urlfinder": "passive URL discovery",
    "ffuf": "web fuzzer for paths, parameters, and virtual hosts",
    "waybackurls": "URLs of a domain known to the Wayback Machine",
    "gau": "URLs of a domain from several archives",
    "gitleaks": "finds secrets (keys, tokens, passwords) in a git repository's history or in a folder",
    "trufflehog": "finds secrets in repositories and files; it tries each one against the real service unless given --no-verification",
    "scorecard": "OpenSSF checks of a public repository's security practices",
    "cloud-audit": "read-only AWS account security scanner (needs AWS credentials of that account)",
    "anew": "appends only the lines a file does not have yet",
    "gf": "grep with named patterns for interesting parameters and strings",
    "qsreplace": "replaces query string values in a list of URLs",
    "hakrawler": "fast web crawler for links and endpoints",
    "cvemap": "ProjectDiscovery's CVE search client",
    "pdtm": "ProjectDiscovery's installer and updater for its tools",
}


PROJECT_BIN: Path | None = config.PROJECT_ROOT / "bin"      # used when it exists; nothing creates it


def folder() -> Path:
    """`~/.hunterai/bin`: always there."""
    p = config.home() / "bin"
    p.mkdir(parents=True, exist_ok=True)
    return p


def folders() -> list[Path]:
    """Where programs are looked for, in order."""
    project = PROJECT_BIN if PROJECT_BIN is not None and PROJECT_BIN.is_dir() else None
    return [*([project] if project else []), folder()]


def primary() -> Path:
    """The folder to name when the owner is told where to put a program."""
    return folders()[0]


def installed() -> list[dict]:
    """The runnable programs in the tool folder, by name."""
    out, seen = [], set()
    entries = []
    for root in folders():
        try:
            entries += sorted(root.iterdir(), key=lambda p: p.name)
        except OSError:
            continue
    for p in entries:
        if p.name in OWN or p.name in seen or not _NAME.match(p.name):
            continue
        try:
            if not p.is_file() or not os.access(p, os.X_OK):
                continue
        except OSError:
            continue
        seen.add(p.name)
        out.append({"name": p.name, "about": KNOWN.get(p.name, ""), "known": p.name in KNOWN})
    return sorted(out, key=lambda t: t["name"])


def where(name: str) -> Path | None:
    """The file a program of the tool folder runs as; None when the folder has no such program."""
    if not any(t["name"] == name for t in installed()):
        return None
    return next((root / name for root in folders() if (root / name).is_file()), None)


def prompt_line() -> str:
    """What the model is told about the folder; "" when it holds nothing."""
    tools = installed()
    if not tools:
        return ""
    listed = ", ".join(f"{t['name']} ({t['about']})" if t["about"] else t["name"] for t in tools[:MAX_LISTED])
    more = f", and {len(tools) - MAX_LISTED} more" if len(tools) > MAX_LISTED else ""
    return ("Programs in the owner's tool folder, found by name in every shell command: " + listed + more +
            ". Prefer them to writing your own scanner. Ask a program for its flags with -h when unsure; do not invent flags.")


def path_with_folder(path: str) -> str:
    own = [str(p) for p in folders()]
    parts = [p for p in path.split(os.pathsep) if p and p not in own]
    return os.pathsep.join([*own, *parts])
