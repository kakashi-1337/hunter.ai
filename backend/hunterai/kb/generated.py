"""What a folder, a repository or an archive brings along that nobody wrote: the output of a build, a minified or
bundled script, a source map, a lock file. One build folder makes thousands of passages that answer no question,
and each of them can stand in the place of a passage that does.

Three ways to tell, and they are the same when a file is indexed and when a collection is cleaned (kb/clean.py):
the directory it stands in, its name, and, for a script or a style sheet, its shape. Whether they apply at all is
the owner's switch (Settings, "kb_skip_generated"): a target's bundle can be exactly what somebody wants to search.
What is never read (.git, node_modules, secret-like names) is another matter and no switch: kb/sources.py."""
from __future__ import annotations

import re
from collections.abc import Iterable

SUFFIX = " (not ingested; Settings can turn this off)"      # what the owner reads beside a skipped file: there is a switch
BUILD = "build output or tool cache" + SUFFIX
SOURCE_MAP = "source map" + SUFFIX
MINIFIED = "minified or bundled file" + SUFFIX
LONG_LINES = "minified: most of it stands in very long lines" + SUFFIX
LOCK = "lock file" + SUFFIX

# Only names that mean nothing else. "dist", "build", "out", "target" and "vendor" are not here: under such a name
# stands somebody's writing often enough, and a bundle under it is told by its name or its shape.
DIRS = frozenset({
    ".next", ".nuxt", ".output", ".svelte-kit", ".astro", ".docusaurus", ".angular", ".expo",      # what a framework builds
    ".vercel", ".netlify", ".serverless", ".wrangler",                                             # what is built for a host
    ".turbo", ".parcel-cache", ".vite", ".cache", ".sass-cache", ".nyc_output", ".ruff_cache",     # caches of tools
    ".ipynb_checkpoints", ".eggs", ".nox", ".gradle", ".terraform", ".dart_tool",
    ".yarn", ".pnpm-store", ".npm", "bower_components", "jspm_packages",                           # packages a tool fetched
})
LOCK_FILES = frozenset({
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lock", "bun.lockb", "deno.lock",
    "cargo.lock", "poetry.lock", "pipfile.lock", "uv.lock", "pdm.lock", "pixi.lock", "composer.lock", "gemfile.lock",
    "go.sum", "flake.lock", "packages.lock.json", "podfile.lock", "pubspec.lock", "mix.lock", "gradle.lockfile",
    "package.resolved",
})
_SOURCE_MAP = re.compile(r"\.(?:js|mjs|cjs|css|ts|jsx|tsx)\.map$")
_MINIFIED = re.compile(r"(?:[.-]min|\.bundle|\.chunk)\.(?:js|mjs|cjs|css)$")
SHAPED = (".js", ".mjs", ".cjs", ".css")    # where a long line means a minifier: in prose, a paragraph is one line too
LONG_LINE = 500                             # bytes: nobody writes a line of a script that long, and a minifier that
#                                             breaks its lines breaks them there or later
MIN_LONG = 2000                             # bytes in such lines before a file is judged: less is a passage or two


def by_name(name: str) -> str | None:
    """Why a file of this name is left out, or None: its name says nothing."""
    name = name.lower()
    if name in LOCK_FILES:
        return LOCK
    if _SOURCE_MAP.search(name):
        return SOURCE_MAP
    if _MINIFIED.search(name):
        return MINIFIED
    return None


def has_shape(name: str) -> bool:
    """Whether a file of this name is one whose shape is asked for: a script or a style sheet."""
    return name.lower().endswith(SHAPED)


def by_shape(name: str, pieces: Iterable[bytes]) -> str | None:
    """Why a script or a style sheet is left out for its shape, or None. Minified, whatever its name: more than half
    of it stands in lines longer than LONG_LINE, and that is MIN_LONG bytes or more. `pieces`: the file whole, or
    its passages as a collection holds them. A line that two passages share counts as two shorter ones, so a stored
    document is judged a little more mildly than its file would be.
    Measured on this project's own build and on the 2,826 scripts and style sheets under its node_modules: the
    two files of the build are found (their names say nothing). Of the 261 files named minified, 260 stand mostly
    in such lines; nearly all are smaller than MIN_LONG, pass here, and are told by their name. Among the other
    2,565, 23 are found: builds on one line, tables of data, and files that are mostly one long list."""
    if not has_shape(name):
        return None
    long = total = 0
    for piece in pieces:
        for line in piece.split(b"\n"):
            total += len(line)
            if len(line) > LONG_LINE:
                long += len(line)
    return LONG_LINES if long >= MIN_LONG and 2 * long > total else None


def of_uri(kind: str, uri: str) -> str | None:
    """Why a document that stands in a collection would be left out today, going by where it came from: a directory
    on its way that is build output, or its name. None when nothing there says so; its shape is another question
    (by_shape). `kind`: the document's source. A page of a crawled site was asked for and is never left out.
    Every directory of a path on this machine counts, also the ones above the folder that was indexed: which
    folder that was is not kept with the document."""
    if kind == "web":
        return None
    head, *members = uri.split("!/")                # an archive, then a member of it, then a member of that
    if kind == "upload":
        head = head.removeprefix("upload:")
    elif kind == "git":
        head = "/".join(head.removeprefix("git:").split("/")[3:])       # host/owner/repository are no directories of it
    paths = [head, *members]
    for path in paths:
        parts = [p for p in path.split("/") if p]
        if any(d in DIRS for d in parts[:-1]):
            return BUILD
    return by_name(paths[-1].rsplit("/", 1)[-1])
