"""The MCP checker: a look at a server's code before it is installed or started. Static
rules over the files, a dependency audit against known vulnerabilities (OSV), and an
optional model summary. It reads; it never runs anything from the code it checks.

A clean report is not proof of safety: the rules catch known bad shapes, not intent."""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

MAX_FILES = 4000
MAX_FILE_BYTES = 1024 * 1024
MAX_FINDINGS_PER_RULE = 12
EXCERPT = 200
OSV_URL = "https://api.osv.dev/v1/querybatch"
OSV_BATCH = 500
CODE_EXT = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py", ".sh", ".bash", ".rb", ".go", ".rs", ".json", ".toml",
            ".yaml", ".yml", ".md", ".cfg", ".ini"}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist-info", ".tox", ".mypy_cache"}
SEVERITIES = ("high", "medium", "info")
NATIVE_EXT = {".node", ".so", ".dll", ".dylib", ".wasm", ".exe", ".bin", ".pyd"}
BUNDLED_DIRS = {"node_modules", ".venv", "venv", "site-packages", "vendor"}
WINDOW, OVERLAP = 4000, 300          # a long line is matched piece by piece
_LINE_COMMENT = {"//": {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".rs"},
                 "#": {".py", ".sh", ".bash", ".rb", ".toml", ".yaml", ".yml", ".cfg", ".ini"}}


@dataclass
class Finding:
    rule: str
    severity: str            # high | medium | info
    title: str
    file: str
    line: int
    excerpt: str


# (rule, severity, title, pattern, file-extension filter or None)
_RULES: list[tuple[str, str, str, re.Pattern, set[str] | None]] = [
    ("poisoned-description", "high", "Text that tries to instruct the model behind the user's back",
     re.compile(r"(?i)(ignore (all |any )?(previous|prior|above) instructions|do not (tell|mention|inform|reveal)( this)? (to )?the user"
                r"|<\s*important\s*>|without (telling|informing|asking) the user|before using any other tool|"
                r"read (the )?(file )?~?/?\.(ssh|aws|env)\b.{0,40}(send|include|pass))"), None),
    ("exfil-endpoint", "high", "Sends data to a paste, webhook, or tunnel service",
     re.compile(r"(?i)(pastebin\.com|discord(app)?\.com/api/webhooks|hooks\.slack\.com|webhook\.site|requestbin|"
                r"ngrok(-free)?\.(io|app)|pipedream\.net|interact\.sh|oast\.(fun|live|pro)|burpcollaborator)"), None),
    ("encoded-exec", "high", "Decodes and runs hidden code",
     re.compile(r"(eval\s*\(\s*(atob|Buffer\.from|unescape)\s*\(|new Function\s*\(\s*(atob|Buffer\.from)|"
                r"exec\s*\(\s*(base64\.b64decode|bytes\.fromhex|codecs\.decode|zlib\.decompress|marshal\.loads)|"
                r"\bexec\s*\(\s*__import__|child_process['\"]\)\s*\.\s*exec\s*\(\s*(atob|Buffer\.from))"), None),
    ("credential-read", "high", "Reads keys, credentials, or another assistant's settings",
     re.compile(r"(\.ssh/(id_|authorized_keys|config)|\.aws/credentials|\.config/gcloud|\.kube/config|\.netrc|\.git-credentials|"
                r"\.docker/config\.json|Login Data|Cookies\.sqlite|key4\.db|\.gnupg|\.claude(/|\.json)|\.cursor/mcp\.json|"
                r"\.hunterai|/etc/shadow|keychain|libsecret|wallet\.dat)"), {".js", ".mjs", ".cjs", ".ts", ".tsx", ".py", ".sh", ".bash", ".rb", ".go", ".rs"}),
    ("install-script", "medium", "Runs a script when the package is installed",
     re.compile(r'"(preinstall|install|postinstall|prepare)"\s*:\s*"'), {".json"}),
    ("pipe-to-shell", "medium", "Downloads a script and runs it",
     re.compile(r"(curl|wget)[^\n|;]{0,200}\|\s*(sudo\s+)?(ba|z|da)?sh\b"), None),
    ("dynamic-code", "medium", "Builds code from strings at run time",
     re.compile(r"(\beval\s*\(|new Function\s*\(|\bexec\s*\(\s*compile\s*\(|vm\.runIn(New|This)Context)"),
     {".js", ".mjs", ".cjs", ".ts", ".tsx", ".py"}),
    ("env-dump", "medium", "Reads the whole environment, where API keys live",
     re.compile(r"(JSON\.stringify\s*\(\s*process\.env\s*\)|dict\s*\(\s*os\.environ\s*\)|os\.environ\.copy\s*\(\s*\)|"
                r"Object\.(entries|keys)\s*\(\s*process\.env\s*\))"), {".js", ".mjs", ".cjs", ".ts", ".tsx", ".py"}),
    ("raw-ip-url", "medium", "Talks to a bare IP address",
     re.compile(r"https?://(?!127\.|0\.0\.0\.0|10\.|192\.168\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.)(\d{1,3}\.){3}\d{1,3}(:\d+)?/"), None),
    ("process-spawn", "info", "Starts other programs (many servers do; look at what it runs)",
     re.compile(r"(child_process|\bexecSync\s*\(|\bspawn(Sync)?\s*\(|subprocess\.(run|Popen|call|check_output)|os\.system\s*\(|"
                r"\bos\.popen\s*\()"), {".js", ".mjs", ".cjs", ".ts", ".tsx", ".py"}),
    ("filesystem-wide", "info", "Works on the home folder or the whole disk",
     re.compile(r"(os\.homedir\s*\(\s*\)|Path\.home\s*\(\s*\)|expanduser\s*\(\s*['\"]~|glob\s*\(\s*['\"]/(\*\*|home|Users))"),
     {".js", ".mjs", ".cjs", ".ts", ".tsx", ".py"}),
]
_IN_COMMENTS = {"poisoned-description", "pipe-to-shell", "exfil-endpoint"}     # text can matter even in a comment
_OBFUSCATED = re.compile(r"[A-Za-z0-9+/=]{1500,}|(\\x[0-9a-fA-F]{2}){200,}|(_0x[0-9a-f]{4,}.{0,40}){8,}")


def _files(root: Path, skipped: list[str] | None = None):
    count = 0
    for dirpath, dirnames, names in os.walk(root, followlinks=False):
        if skipped is not None:
            skipped += [(Path(dirpath) / d).relative_to(root).as_posix() for d in dirnames if d in BUNDLED_DIRS]
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not (Path(dirpath) / d).is_symlink())
        for n in sorted(names):
            p = Path(dirpath) / n
            if p.is_symlink() or not p.is_file():
                continue
            count += 1
            if count > MAX_FILES:
                return
            yield p


def scan(root: Path) -> dict:
    """Static rules over the folder. Returns {"findings": [...], "files", "bytes", "truncated",
    "counts": {severity: n}}; findings are sorted most severe first."""
    findings: list[Finding] = []
    per_rule: dict[str, int] = {}
    files = size = read = 0
    truncated = False
    skipped_dirs: list[str] = []

    def add(rule, severity, title, rel, line_no, text):
        per_rule[rule] = per_rule.get(rule, 0) + 1
        if per_rule[rule] <= MAX_FINDINGS_PER_RULE:
            findings.append(Finding(rule, severity, title, rel, line_no, text.strip()[:EXCERPT]))

    for p in _files(root, skipped_dirs):
        files += 1
        rel = p.relative_to(root).as_posix()
        ext = p.suffix.lower()
        try:
            st = p.stat()
            size += st.st_size
            if ext in NATIVE_EXT:
                add("native-binary", "medium", "Compiled code that this check cannot read", rel, 0, f"{st.st_size} bytes")
                continue
            if p.name == ".npmrc":
                add("npm-settings", "medium", "Ships its own npm settings, which can change how an install behaves", rel, 0,
                    p.read_bytes()[:EXCERPT].decode("utf-8", "replace"))
            with open(p, "rb") as fh:
                data = fh.read(MAX_FILE_BYTES + 1)
            # A file with no known extension is read when it looks like a script (bin/cli files).
            if ext not in CODE_EXT and p.name not in ("Dockerfile", "Makefile", ".npmrc") and not data.startswith(b"#!"):
                continue
        except OSError:
            continue
        if len(data) > MAX_FILE_BYTES:
            truncated = True
            data = data[:MAX_FILE_BYTES]
        if b"\0" in data[:4096]:
            continue
        read += 1
        text = data.decode("utf-8", "replace")
        lock = p.name in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock", "poetry.lock")
        for n, line in enumerate(text.split("\n"), 1):
            if lock:
                break
            if len(line) > 1200 and p.suffix.lower() in (".js", ".mjs", ".cjs", ".ts", ".py", ".sh") and _OBFUSCATED.search(line):
                add("obfuscated", "high", "Long encoded or obfuscated code", rel, n, line[:EXCERPT])
            lead = line.lstrip()[:2]
            comment = any(lead.startswith(mark) and ext in exts for mark, exts in _LINE_COMMENT.items())
            for rule, severity, title, rx, exts in _RULES:
                if comment and rule not in _IN_COMMENTS:
                    continue      # a whole-line comment that names a path or a call does nothing
                if exts is not None and ext not in exts and not (ext == "" and exts and ".js" in exts):
                    continue
                if rule == "install-script" and p.name != "package.json":
                    continue
                for start in range(0, max(len(line), 1), WINDOW - OVERLAP):      # every part of a long line
                    m = rx.search(line, start, start + WINDOW)
                    if m:
                        add(rule, severity, title, rel, n, line[max(0, m.start() - 60):m.end() + 100])
                        break
    for d in sorted(set(skipped_dirs))[:20]:
        add("bundled-code", "medium", "Ships third-party code in a folder this check does not read", d + "/", 0,
            "its files run with the server but were not checked")
    order = {s: i for i, s in enumerate(SEVERITIES)}
    findings.sort(key=lambda f: (order[f.severity], f.rule, f.file, f.line))
    counts = {s: sum(v for r, v in per_rule.items() if _severity_of(r) == s) for s in SEVERITIES}
    return {"findings": [asdict(f) for f in findings], "files": files, "read": read, "bytes": size,
            "truncated": truncated or files >= MAX_FILES, "counts": counts,
            "more": {r: v - MAX_FINDINGS_PER_RULE for r, v in per_rule.items() if v > MAX_FINDINGS_PER_RULE}}


def _severity_of(rule: str) -> str:
    if rule == "obfuscated":
        return "high"
    if rule in ("native-binary", "npm-settings", "bundled-code"):
        return "medium"
    return next(s for r, s, *_ in _RULES if r == rule)


# ---- dependency audit

def pinned(root: Path) -> list[dict]:
    """Exact dependency versions the folder declares: [{"ecosystem", "name", "version"}]."""
    out: dict[tuple, dict] = {}

    def put(eco, name, version):
        if name and version and re.fullmatch(r"[A-Za-z0-9@/._+!-]{1,200}", name) and re.fullmatch(r"[A-Za-z0-9._+!-]{1,80}", version):
            out[(eco, name.lower() if eco == "PyPI" else name, version)] = {"ecosystem": eco, "name": name, "version": version}
    lock = root / "package-lock.json"
    if lock.is_file():
        try:
            data = json.loads(lock.read_bytes()[:20 * 1024 * 1024])
            for path, meta in (data.get("packages") or {}).items():
                if path.startswith("node_modules/") and isinstance(meta, dict):
                    put("npm", path.rsplit("node_modules/", 1)[1], meta.get("version"))
            for name, meta in (data.get("dependencies") or {}).items():
                if isinstance(meta, dict):
                    put("npm", name, meta.get("version"))
        except (ValueError, OSError, AttributeError):
            pass
    for req in sorted(root.glob("requirements*.txt")):
        try:
            for line in req.read_text(errors="replace").splitlines()[:5000]:
                m = re.match(r"^\s*([A-Za-z0-9._-]+)(?:\[[^\]]*\])?\s*==\s*([A-Za-z0-9._+!-]+)", line)
                if m:
                    put("PyPI", m.group(1), m.group(2))
        except OSError:
            pass
    for name in ("uv.lock", "poetry.lock"):
        f = root / name
        if f.is_file():
            try:
                for m in re.finditer(r'\[\[package\]\]\s*\nname\s*=\s*"([^"]+)"\s*\nversion\s*=\s*"([^"]+)"', f.read_text(errors="replace")):
                    put("PyPI", m.group(1), m.group(2))
            except OSError:
                pass
    gosum = root / "go.sum"
    if gosum.is_file():
        try:
            for line in gosum.read_text(errors="replace").splitlines()[:20_000]:
                m = re.match(r"^(\S+) v(\S+) h1:", line)        # the module itself, not its go.mod line
                if m and not m.group(2).endswith("/go.mod"):
                    put("Go", m.group(1), m.group(2))
        except OSError:
            pass
    return list(out.values())


async def audit(packages: list[dict], client: httpx.AsyncClient | None = None) -> dict:
    """Known vulnerabilities for exact versions, from OSV. Only package names and versions are
    sent. Returns {"checked", "vulnerable": [{ecosystem, name, version, ids}], "error"}."""
    if not packages:
        return {"checked": 0, "vulnerable": [], "error": None, "note": "no pinned dependency versions were found, so none were audited"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), trust_env=False)
    vulnerable: list[dict] = []
    done = 0
    try:
        for i in range(0, len(packages), OSV_BATCH):
            part = packages[i:i + OSV_BATCH]
            r = await client.post(OSV_URL, json={"queries": [
                {"package": {"ecosystem": p["ecosystem"], "name": p["name"]}, "version": p["version"]} for p in part]})
            r.raise_for_status()
            for p, res in zip(part, r.json().get("results") or []):
                ids = [v.get("id") for v in (res or {}).get("vulns") or [] if v.get("id")]
                if ids:
                    vulnerable.append({**p, "ids": ids[:20]})
            done += len(part)
    except (httpx.HTTPError, ValueError) as e:
        return {"checked": done, "vulnerable": vulnerable, "error": f"the vulnerability database could not be asked: {type(e).__name__}"}
    finally:
        if own:
            await client.aclose()
    return {"checked": len(packages), "vulnerable": vulnerable, "error": None}


def verdict(report: dict, audit_result: dict | None) -> dict:
    """A plain summary for the approve step. Never "safe": at best "nothing found"."""
    counts = report["counts"]
    vulns = len((audit_result or {}).get("vulnerable") or [])
    if counts["high"]:
        level, text = "danger", f"{counts['high']} high-risk pattern(s) found. Read them before going on."
    elif counts["medium"] or vulns:
        level, text = "caution", "Patterns that deserve a look were found."
    else:
        level, text = "nothing-found", "No known bad pattern was found. That is not proof the code is safe."
    if vulns:
        text += f" {vulns} dependency version(s) have known vulnerabilities."
    if report.get("truncated"):
        text += " The check did not cover every file (too many, or files over 1 MB)."
    if audit_result and audit_result.get("error"):
        text += f" Dependency audit incomplete: {audit_result['error']}."
    elif audit_result is not None and not audit_result.get("checked"):
        text += " Its dependencies were not audited (no pinned versions in the code)."
    unread = report.get("files", 0) - report.get("read", report.get("files", 0))
    if unread > 0:
        text += f" {unread} file(s) are not text this check reads."
    return {"level": level, "text": text}
