"""The state of this install as text the owner can copy into a report: the version of the code, what
runs and what does not, the machine, the models in use. Nothing of the work is in it: no key, no proxy
address, no target, no message."""
from __future__ import annotations

import platform
import re
import subprocess
import time
from pathlib import Path

from . import config

STARTED = time.time()       # when this server loaded its code
REASON = 200                # characters of an MCP server's error
_LOGIN = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^\s/@]*@", re.I)          # user:password@ inside an address
_QUERY = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s?]*)\?\S*", re.I)        # what follows the ? of an address


def _scrubbed(text: str) -> str:
    """Without what a path or an address can give away: the home folder is written ~, an address loses its login
    and what follows its question mark (keys are passed there)."""
    text = _QUERY.sub(r"\1?...", _LOGIN.sub(r"\1", text))
    home = str(Path.home())
    return text.replace(home, "~") if len(home) > 1 else text


def _git(*args: str) -> str | None:
    """What git says about HunterAI's folder; None when the folder is no checkout or git is not there."""
    try:
        done = subprocess.run(["git", "-C", str(config.PROJECT_ROOT), *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip("\n") if done.returncode == 0 else None


def lasted(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 86400:
        return f"{seconds // 3600} h {seconds % 3600 // 60} min"
    return f"{seconds // 86400} d {seconds % 86400 // 3600} h"


def _version() -> str:
    commit = _git("rev-parse", "--short", "HEAD")
    if not commit:
        return "unknown (this folder is not a git checkout)"
    lines = [line for line in (_git("status", "--porcelain") or "").splitlines() if line.strip()]
    own = len([line for line in lines if line.startswith("??")])
    return (f"{commit} ({_git('log', '-1', '--format=%cs') or 'date unknown'}) on {_git('rev-parse', '--abbrev-ref', 'HEAD') or 'no branch'}; "
            f"{len(lines) - own} file(s) changed here, {own} file(s) of the owner's own")


def _loaded(m: dict) -> str:
    """A model Ollama holds in memory, as the status describes it; an older status names only what is on the GPU."""
    if m.get("processor") is None:
        return f"{m['name']} ({m['vram_mb']} MB on the GPU)"
    window = f", window {m['context']:,}" if m.get("context") else ""
    return f"{m['name']} ({m['size_mb'] / 1024:.1f} GB, {m['processor']}{window})"


def _servers(servers) -> list[str]:
    if servers is None:
        return ["MCP servers: switched off"]
    lines = ["MCP servers:" if servers else "MCP servers: none"]
    for s in servers:
        error = (s.error or "").strip().splitlines()
        said = f"{len(s.tools)} tools" if s.state == "ready" else error[0].strip()[:REASON] if error else ""
        lines.append(f"  {s.state:<8} {s.name}" + (f": {said}" if said else ""))
    return lines


def _local(local: dict | None) -> list[str]:
    """The local model in use: its window, what a window costs for it, and what a new chat sends before the chat."""
    if not local:
        return []
    said = f"Local model in use: {local['model']}"
    if not local.get("installed"):
        said += ", not installed" if local.get("answers") else " (Ollama does not answer)"
    said += f", window {local['window']:,}" + (f" of its own {local['limit']:,}" if local.get("limit") else "")
    if local.get("bytes_per_token"):
        said += f"; about {local['bytes_per_token'] * 8192 / 2**30:.1f} GB for each 8,192 tokens of window"
    over = local.get("overhead") or {}
    more = f", and {over['on_demand']:,} loaded on demand" if over.get("on_demand") else ""
    return [said, f"A new chat sends about {over.get('tokens', 0):,} tokens of rules and tools ({over.get('tools', 0):,} tools{more}); "
                  f"a window of {local.get('enough', 0):,} leaves room for the chat"]


def text(check: dict, status: dict, settings: dict, servers, tools, now: float | None = None, local: dict | None = None) -> str:
    """`check` is the setup check, `status` the machine status, `settings` what the Settings dialog is sent,
    `servers` what the MCP manager says about its servers (None: there is none), `tools` every registered tool,
    `local` what /api/system/local says about the local model in use."""
    now = time.time() if now is None else now
    at = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t))      # noqa: E731
    machine = settings.get("machine") or {}
    models = status.get("local_models")
    lines = [
        f"HunterAI diagnostics, {at(now)}",
        f"Version: {_version()}",
        f"Server started {at(STARTED)}, running {lasted(now - STARTED)}",
        f"Restart needed: {'yes: the code on disk changed after the server started' if status.get('restart_needed') else 'no'}",
        f"System: {platform.platform()}, Python {platform.python_version()}, {machine.get('cores') or '?'} cores, "
        f"{machine.get('memory_gb') or '?'} GB",
    ]
    if status.get("cpu_temp") is not None:
        lines.append(f"Processor: {status['cpu_temp']:.0f} C (limit {status.get('cpu_limit') or '?'} C)")
    lines.append("Local models loaded: " + ("Ollama does not answer" if models is None else ", ".join(_loaded(m) for m in models) or "none"))
    lines += [
        f"Default model: {settings.get('default_provider')}/{settings.get('default_model')}",
        f"Fallback model: {settings.get('fallback_model') or 'none'}",
        f"Context window for local models: {settings.get('local_window')}",
        f"Largest context window for local models: {settings.get('local_window_max')}"
        + ("" if settings.get("local_window_max") else " (a window stays as it is set)"),
        *_local(local),
        "Keys: " + "; ".join(f"{name}: {'set' if has else 'none'}" for name, has in sorted((settings.get("keys") or {}).items())),
        f"Proxy for targets: {'set' if settings.get('upstream_proxy') else 'none'}",
        f"Proxy for model providers: {'set' if settings.get('provider_proxy') else 'none'}",
        "Checks:",
    ]
    for item in check.get("items") or []:
        state = "ok" if item["ok"] else "STOPPED" if item.get("stopped") else "missing"
        lines.append(f"  {state:<8} {item['label']}: {item['detail']}")
    own = len([t for t in tools if t.source == "native"])
    lines += [*_servers(servers), f"Tools: {own} of HunterAI's own, {len(tools) - own} from MCP servers"]
    return _scrubbed("\n".join(lines) + "\n")
