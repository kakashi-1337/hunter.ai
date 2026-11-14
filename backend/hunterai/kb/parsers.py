"""From a file's name and bytes to clean text. A file that has no text to give is a Skip with
a reason, never an error and never dropped quietly."""
from __future__ import annotations

import codecs
import io
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Literal

Kind = Literal["text", "markdown", "code", "csv", "sql", "html", "pdf"]


@dataclass
class Parsed:
    kind: Kind
    text: str
    title: str | None = None
    note: str | None = None


class Skip(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _exts(kind: Kind, names: str) -> dict[str, Kind]:
    return {e: kind for e in names.split()}


EXTENSIONS: dict[str, Kind] = {
    **_exts("markdown", ".md .markdown .mdx"), **_exts("csv", ".csv .tsv"), **_exts("sql", ".sql"),
    **_exts("html", ".html .htm .xhtml .mhtml .mht"), **_exts("pdf", ".pdf"),
    **_exts("text", ".txt .text .log .rst .nfo"),
    **_exts("code", ".py .js .mjs .cjs .ts .tsx .jsx .go .rs .java .kt .c .h .cc .cpp .hpp .cs .rb .php "
                    ".sh .bash .zsh .ps1 .pl .lua .swift .scala .sol .dart .r .asm .s .vue .svelte .json "
                    ".jsonl .yaml .yml .toml .ini .cfg .conf .xml .gradle .tf .proto .graphql .bat .cmake"),
}
FILENAMES: dict[str, Kind] = {n.lower(): "code" for n in
                              ("Dockerfile", "Makefile", "CMakeLists.txt", "Jenkinsfile", "Vagrantfile")}
ARCHIVE_SUFFIXES = (".tar.gz", ".tar.xz", ".zip", ".tar", ".tgz", ".txz", ".gz", ".xz", ".7z")

MAX_MEMBER_BYTES = 32 * 1024**2      # one XML part of an Office file, unpacked
MAX_OFFICE_BYTES = 96 * 1024**2      # all the parts read from one Office file
MAX_OFFICE_CHARS = 10_000_000        # text taken from one Office file
MAX_CELL_CHARS = 32_767              # Excel's own limit for one cell
OCR_TIMEOUT = 60.0                   # one image or one PDF page
OCR_DEADLINE = 600.0                 # all the OCR of one document
CONVERT_TIMEOUT = 120.0              # LibreOffice converting one legacy file
MAX_OCR_PAGES = 50
MAX_OCR_IMAGE_BYTES = 20 * 1024**2
MIN_OCR_IMAGE_BYTES = 10 * 1024      # smaller pictures are icons, not pages

_BOMS = ((codecs.BOM_UTF8, "utf-8-sig", "utf-8"), (codecs.BOM_UTF32_LE, "utf-32", "utf-32"),
         (codecs.BOM_UTF32_BE, "utf-32", "utf-32"), (codecs.BOM_UTF16_LE, "utf-16", "utf-16"),
         (codecs.BOM_UTF16_BE, "utf-16", "utf-16"))


def _base(name: str) -> str:
    return name.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _ext(name: str) -> str:
    base = _base(name)
    return "." + base.rsplit(".", 1)[1] if "." in base.strip(".") else ""


OFFICE_EXTENSIONS = (".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".doc", ".xls", ".ppt", ".rtf", ".epub")
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".gif")


def kind_for(name: str) -> Kind | Literal["archive", "office", "image"] | None:
    base = _base(name)
    if base in FILENAMES:
        return FILENAMES[base]
    if any(base.endswith(s) for s in ARCHIVE_SUFFIXES):
        return "archive"
    ext = _ext(name)
    if ext in OFFICE_EXTENSIONS:
        return "office"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return EXTENSIONS.get(ext)


def decode_text(data: bytes) -> tuple[str, str] | None:
    for bom, codec, label in _BOMS:
        if data.startswith(bom):
            try:
                return data.decode(codec), label
            except UnicodeError:
                return None
    if b"\x00" in data[:8192]:
        return None
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    for codec in ("cp1252", "latin-1"):
        try:
            text = data.decode(codec)
        except UnicodeDecodeError:
            continue
        controls = sum(1 for c in text if unicodedata.category(c) == "Cc" and c not in "\t\n\r\f")
        if controls <= len(text) * 0.01:
            return text, codec
        return None
    return None


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f]")


def clean(text: str) -> str:
    return _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


def _decoded(name: str, data: bytes) -> str:
    got = decode_text(data)
    if got is None:
        raise Skip(f"binary or undecodable content in a {_ext(name) or _base(name)} file")
    return clean(got[0])


def _plain(kind: Kind) -> Callable[[str, bytes], Parsed]:
    return lambda name, data: Parsed(kind, _decoded(name, data))


_DROP = {"script", "style", "noscript", "template", "svg"}
_BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre",
          "table", "ul", "ol", "blockquote"}


_FURNITURE = {"nav", "footer", "aside"}     # menus, footers, side boxes: on every page of a site, about none of them
MIN_BODY_CHARS = 200


class _Html(HTMLParser):
    def __init__(self, furniture: bool = False):
        super().__init__(convert_charrefs=True)
        self.furniture = furniture       # leave out menus, footers, and side boxes of the page itself
        self._content = 0                # depth inside <article> or <main>: what is there belongs to the text
        self._skip: list[str] = []       # open furniture elements being left out
        self.out: list[str] = []
        self.title: list[str] = []
        self._drop = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in ("article", "main"):
            self._content += 1
        if self.furniture and tag in _FURNITURE and (self._skip or not self._content):
            self._skip.append(tag)
        if tag in _DROP:
            self._drop += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("article", "main"):
            self._content = max(0, self._content - 1)
        if self._skip and tag == self._skip[-1]:
            self._skip.pop()
        if tag in _DROP:
            self._drop = max(0, self._drop - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title.append(data)
        elif not self._drop and not self._skip:
            self.out.append(data)


def _html_text(source: str, furniture: bool = False) -> tuple[str, str | None]:
    p = _Html(furniture)
    p.feed(source)
    p.close()
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in "".join(p.out).split("\n")]
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return body, re.sub(r"\s+", " ", "".join(p.title)).strip() or None


def _html_source(source: str) -> Parsed:
    """Page furniture is left out when the page has a body without it; a page that is only
    a menu keeps its menu."""
    body, title = _html_text(source, furniture=True)
    full, title = _html_text(source)
    # Kept whole when little is left without the furniture: a page that is only a menu, or
    # markup broken in a way that makes a menu swallow the text after it.
    if len(body) < MIN_BODY_CHARS or 2 * len(body) < len(full):
        body = full
    return Parsed("html", f"{title}\n{body}" if title else body, title=title)


def _html(name: str, data: bytes) -> Parsed:
    return _html_source(_decoded(name, data))


def _mhtml(name: str, data: bytes) -> Parsed:
    """A page saved by a browser as one file: the HTML parts are read, the rest is left."""
    import email
    import email.policy
    try:
        message = email.message_from_bytes(data, policy=email.policy.default)
        pages = []
        for part in message.walk():
            if part.get_content_type() in ("text/html", "application/xhtml+xml") and len(pages) < 50:
                raw = part.get_payload(decode=True) or b""
                got = decode_text(raw)
                if got is not None:
                    pages.append(_html_source(got[0]))
    except Exception as e:  # noqa: BLE001 - a malformed message may raise anything
        raise Skip(f"unreadable saved page: {type(e).__name__}") from None
    text = "\n\n".join(p.text for p in pages if p.text.strip())
    return Parsed("html", text, title=next((p.title for p in pages if p.title), None))


def _pdf(name: str, data: bytes) -> Parsed:
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise Skip("PDF is encrypted")
        pages = [clean(page.extract_text() or "") for page in reader.pages]
    except Skip:
        raise
    except BaseException as e:  # noqa: BLE001 - a hostile PDF may raise anything, RecursionError included
        if isinstance(e, (KeyboardInterrupt, SystemExit)):
            raise
        raise Skip(f"unreadable PDF: {type(e).__name__}") from None
    blank = [i for i, t in enumerate(pages) if not t.strip()]
    why = "OCR is off"
    if blank:
        from . import office
        pages, why = office.ocr_pdf_pages(data, pages, blank)
    empty = sum(1 for t in pages if not t.strip())
    suffix = "have no text layer (OCR is off)" if why == "OCR is off" else f"have no text ({why})"
    if not pages or empty == len(pages):
        raise Skip("PDF " + suffix.replace("have", "has", 1))
    note = f"{empty} of {len(pages)} pages {suffix}" if empty else (
        "some pages were read by OCR; they may contain errors" if blank else None)
    return Parsed("pdf", "\n\n".join(pages), note=note)


PARSERS: dict[Kind, Callable[[str, bytes], Parsed]] = {
    "text": _plain("text"), "markdown": _plain("markdown"), "code": _plain("code"), "csv": _plain("csv"),
    "sql": _plain("sql"), "html": _html, "pdf": _pdf,
}


def _sniff(name: str, data: bytes) -> Kind:
    if data.startswith(b"%PDF-"):
        return "pdf"
    got = decode_text(data)
    if got is None:
        raise Skip(f"unknown binary type ({_ext(name) or 'no extension'})")
    head = got[0][:2048].lstrip("﻿").lstrip().lower()
    return "html" if head.startswith(("<!doctype html", "<html")) else "text"


def parse(name: str, data: bytes) -> Parsed:
    """Raises Skip, and nothing else, for input it cannot turn into text."""
    if not data:
        raise Skip("empty file")
    kind = kind_for(name)
    if kind in ("office", "image"):
        from . import office
        parsed = (office.parse_office if kind == "office" else office.parse_image)(name, data)
        parsed.text = clean(parsed.text)
        if not parsed.text.strip():
            raise Skip("no text content")
        return parsed
    if kind is None or kind == "archive":
        kind = _sniff(name, data)
    parsed = _mhtml(name, data) if _ext(name) in (".mhtml", ".mht") else PARSERS[kind](name, data)
    if not parsed.text.strip():
        raise Skip("no text content")
    return parsed
