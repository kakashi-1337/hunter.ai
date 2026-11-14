"""Git repositories as an ingest source: a shallow clone of a public GitHub or GitLab
repository into the job's staging folder. Nothing in the repository runs: no hooks, no
submodules, no credential prompts, and the .git folder is removed before the files are read."""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

from . import limits, tools

HOSTS = ("github.com", "gitlab.com")
CLONE_TIMEOUT = 600.0
_URL = re.compile(r"^https://(github\.com|gitlab\.com)/([A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*){1,5}?)(?:\.git)?/?$")


RESOLVE_RETRY_PAUSE = 5.0        # seconds before a clone whose host name did not resolve is tried once more


class GitError(Exception):
    pass


def validate(url: str) -> tuple[str, str]:
    """(clean https URL, "host/owner/repo" label). ValueError for anything else: other hosts,
    other schemes, credentials, queries, and paths that are not a repository."""
    m = _URL.match((url or "").strip())
    path = (m.group(2).removesuffix(".git") if m else "")
    if not m or ".." in path or path.count("/") < 1 or path.endswith("/"):
        raise ValueError("give the https URL of a GitHub or GitLab repository, like "
                         "https://github.com/owner/repo")
    return f"https://{m.group(1)}/{path}.git", f"{m.group(1)}/{path}"


async def clone(url: str, dest: Path, allowed_bytes: int) -> None:
    """Shallow-clone `url` (already validated) into `dest`, then drop its .git folder."""
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(dest.parent), "LANG": "C.UTF-8",
           "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_LFS_SKIP_SMUDGE": "1"}
    argv = ["git", "-c", "protocol.file.allow=never", "-c", "protocol.ext.allow=never", "-c", "core.symlinks=false",
            "-c", "core.hooksPath=/dev/null", "-c", "credential.helper=", "clone", "--depth", "1",
            "--single-branch", "--no-tags", "--no-recurse-submodules", "--", url, str(dest)]
    try:
        for attempt in (1, 2):
            res = await tools.run_tool(argv, timeout=CLONE_TIMEOUT, cwd=dest.parent, grow_dir=dest.parent,
                                       grow_limit=max(1, allowed_bytes), env=env)
            # A name that does not resolve at once often does a moment later: one more try.
            if res.returncode == 0 or attempt == 2 or b"Could not resolve host" not in res.stderr:
                break
            shutil.rmtree(dest, ignore_errors=True)
            await asyncio.sleep(RESOLVE_RETRY_PAUSE)
    except tools.ToolMissing:
        raise GitError("git is not installed") from None
    except tools.ToolTimeout:
        shutil.rmtree(dest, ignore_errors=True)
        raise GitError(f"git clone timed out after {CLONE_TIMEOUT:g} s") from None
    except tools.ToolLimit:
        shutil.rmtree(dest, ignore_errors=True)
        raise GitError(limits.byte_cap() + ": the repository is too large") from None
    if res.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        last = res.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["unknown error"]
        raise GitError("git clone failed: " + last[0][:200])
    shutil.rmtree(dest / ".git", ignore_errors=True)
