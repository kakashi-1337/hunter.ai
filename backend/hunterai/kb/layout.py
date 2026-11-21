"""Where a passage stands in its document: under which title and which headings, and which part it is of how many.

A passage is some 1,200 characters cut out of a document, and a model is shown it with its path and its lines. What
stood above it in the document is gone by then. The fourth passage of a page about one product says "Improved
self-correction: when it makes a mistake mid-task ..." and never names the product, because the page's title is the
first line of its first passage and of no other; a table of font sizes under "3. Typography > Hierarchy" of a
design system is a table of font sizes. So a passage is shown with the title of its page and the headings above
it, and with its part number, which says that it is a piece and that the pieces beside it exist.

It is made by rules from what the collection already holds, with no model, when a passage is shown and for the few
that are shown:

- a web page or an HTML file: the first line of its first passage, where the parser puts the title;
- a Markdown file: the headings that stand above the passage in the passages before it, outside code blocks. A
  passage that begins with a heading stands under the headings above that one, not beside its sisters;
- anything else (source code, a PDF, plain text): nothing yet. Its path says what is known of it.

What it does not do: the search does not look at any of this. Counted on a collection of 606,939 passages with the
kept test of scripts/rag-test.sh (45 questions), the same lines put into the keyword index beside each passage
brought 29 passages into a message where the order of today brings 29 (30 with both), found one passage that only
the name of its file names, and lost one behind the passages of its sister files. Embedded with the passages (a
hint only: among the passages the search already brought), the right one stood first for 17 of the 45 instead of
18, and among the first three for 24 instead of 23. This is for the reader of a passage, not for finding it.

Everything here is the document's own text, as untrusted as the passage under it: it goes inside the reference
block, is screened with the passage, and can close no delimiter (kb/retrieve.py)."""
from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit

from ..repo import kb
from ..types import Hit
from . import parsers

TITLE_MAX = 150             # characters: a first line longer than this is the page's text, not its title
HEADING_MAX = 80            # of one heading
WHERE_MAX = 160             # of all that is shown above a passage; of a deep place the nearest headings are kept

_HEADING = re.compile(r" {0,3}(#{1,6})[ \t]+(.*\S)")
_FENCES = ("```", "~~~")


def form(uri: str) -> str:
    """How the document at this address is laid out, as far as it matters here: "markdown", "html" or ""."""
    web = uri.startswith(("http://", "https://"))
    last = (urlsplit(uri).path if web else uri.split("!/")[-1]).rsplit("/", 1)[-1]
    kind = parsers.kind_for(last)
    if kind == "html" or (web and kind is None):
        return "html"
    return "markdown" if kind == "markdown" else ""


def headings(before: Iterable[str], text: str) -> list[str]:
    """The headings a Markdown passage stands under, the outermost first. `before`: the passages of its document
    that stand before it, in their order. A line inside a code block is no heading, also when the block began in an
    earlier passage."""
    stack: list[tuple[int, str]] = []
    fenced = False
    for passage in before:
        for line in passage.split("\n"):
            if line.lstrip().startswith(_FENCES):
                fenced = not fenced
                continue
            found = None if fenced else _HEADING.match(line)
            name = " ".join(found.group(2).rstrip("#").split())[:HEADING_MAX] if found else ""
            if name:
                level = len(found.group(1))
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, name))
    own = None if fenced else _HEADING.match(text)
    return [name for level, name in stack if own is None or level < len(own.group(1))]


def title(first: str) -> str:
    """The title of a page: the first line of its first passage, or "" when that line is too long to be one."""
    line = " ".join(first.split("\n", 1)[0].split())
    return line if len(line) <= TITLE_MAX else ""


def _capped(where: str) -> str:
    return where if len(where) <= WHERE_MAX else "... " + where[-WHERE_MAX:]


async def annotate(conn, hits: list[Hit]) -> list[Hit]:
    """The same hits in their order, each with what stands above it in its document (`where`, or "" when nothing
    is known). The passages of one document are read once, however many of the hits are its."""
    until: dict[str, int] = {}
    for h in hits:
        kind = form(h.source_uri)
        if kind and h.ordinal > 0:
            until[h.document_id] = max(until.get(h.document_id, 0), h.ordinal if kind == "markdown" else 1)
    before = {document: await kb.texts_until(conn, document, ordinal) for document, ordinal in until.items()}
    out: list[Hit] = []
    for h in hits:
        texts = before.get(h.document_id) or []
        where = ""
        if texts and h.ordinal > 0:
            where = (_capped(" > ".join(headings(texts[:h.ordinal], h.text))) if form(h.source_uri) == "markdown"
                     else title(texts[0]))
        out.append(h if where == h.where else h.model_copy(update={"where": where}))
    return out
