"""Office and OpenDocument files, and text in pictures. Office files are zip archives of
XML: they are read member by member with a size cap and no document types. Old binary
formats go through LibreOffice, and OCR through tesseract; both run with a timeout and are
killed as a process group."""
from __future__ import annotations

import io
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from .. import config
from . import parsers
from .parsers import Parsed, Skip

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
ODF_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
ODF_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"
LEGACY = {".doc": "docx", ".rtf": "docx", ".xls": "xlsx", ".ppt": "pptx"}


class _Bad(Exception):
    pass


_read = {"bytes": 0}      # of the Office file being read (one at a time per thread is enough: reset per file)


def _member(z: zipfile.ZipFile, member: str) -> bytes:
    with z.open(member) as f:
        data = f.read(parsers.MAX_MEMBER_BYTES + 1)
    _read["bytes"] += len(data)
    if len(data) > parsers.MAX_MEMBER_BYTES or _read["bytes"] > parsers.MAX_OFFICE_BYTES:
        raise _Bad("a part of the document is too large")
    return data


def _epub(z: zipfile.ZipFile) -> str:
    """An e-book: its chapters are HTML files, read in the order of their names."""
    def order(name: str):
        return [(0, int(p), "") if p else (1, 0, "") for p in re.findall(r"[0-9]+", name)] + [(1, 0, name.lower())]
    chapters = sorted((n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))), key=order)[:5000]
    out = []
    for name in chapters:
        got = parsers.decode_text(_member(z, name))
        if got is not None:
            text = parsers._html_source(got[0]).text
            if text.strip():
                out.append(text)
    return "\n\n".join(out)


def _xml(z: zipfile.ZipFile, member: str) -> ElementTree.Element:
    data = _member(z, member)
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or data[:4] in (b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00"):
        raise _Bad("XML that is not UTF-8 is not accepted")
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise _Bad("XML with a document type is not accepted")
    return ElementTree.fromstring(data)


def _docx(z: zipfile.ZipFile) -> str:
    if "word/document.xml" not in z.namelist():
        raise _Bad("not a docx document")
    lines = []
    for p in _xml(z, "word/document.xml").iter(W + "p"):
        parts = []
        for el in p.iter():
            if el.tag == W + "t":
                parts.append(el.text or "")
            elif el.tag in (W + "tab",):
                parts.append("\t")
            elif el.tag in (W + "br", W + "cr"):
                parts.append("\n")
        lines.append("".join(parts))
    return "\n".join(lines)


def _numbered(names: list[str], pattern: str) -> list[str]:
    found = [(int(m.group(1)), n) for n in names if (m := re.fullmatch(pattern, n))]
    return [n for _, n in sorted(found)]


def _xlsx(z: zipfile.ZipFile) -> str:
    names = z.namelist()
    if "xl/workbook.xml" not in names:
        raise _Bad("not an xlsx document")
    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        for si in _xml(z, "xl/sharedStrings.xml").iter(S + "si"):
            shared.append("".join(t.text or "" for t in si.iter(S + "t")))
    titles = [s.get("name") or "" for s in _xml(z, "xl/workbook.xml").iter(S + "sheet")]
    out = []
    size = 0
    for i, member in enumerate(_numbered(names, r"xl/worksheets/sheet(\d+)\.xml")):
        rows = []
        for row in _xml(z, member).iter(S + "row"):
            cells = []
            for c in row.iter(S + "c"):
                v = c.find(S + "v")
                if c.get("t") == "s" and v is not None and (v.text or "").isdigit() and int(v.text) < len(shared):
                    cells.append(shared[int(v.text)][:parsers.MAX_CELL_CHARS])
                elif c.get("t") == "inlineStr":
                    cells.append("".join(t.text or "" for t in c.iter(S + "t")))
                else:
                    cells.append(v.text or "" if v is not None else "")
            if any(cells):
                rows.append("\t".join(cells))
                size += len(rows[-1])
                if size > parsers.MAX_OFFICE_CHARS:
                    raise _Bad("the document holds too much text")
        if rows:
            out.append(f"# {titles[i] if i < len(titles) and titles[i] else f'Sheet {i + 1}'}\n" + "\n".join(rows))
    return "\n\n".join(out)


def _pptx(z: zipfile.ZipFile) -> str:
    slides = _numbered(z.namelist(), r"ppt/slides/slide(\d+)\.xml")
    if not slides:
        raise _Bad("not a pptx document")
    out = []
    for n, member in enumerate(slides, 1):
        lines = ["".join(t.text or "" for t in p.iter(A + "t")) for p in _xml(z, member).iter(A + "p")]
        lines = [line for line in lines if line.strip()]
        if lines:
            out.append(f"## Slide {n}\n" + "\n".join(lines))
    return "\n\n".join(out)


def _odf(z: zipfile.ZipFile) -> str:
    if "content.xml" not in z.namelist():
        raise _Bad("not an OpenDocument file")
    lines: list[str] = []

    def walk(el, in_row: bool) -> None:
        if el.tag == ODF_TABLE + "table-row":
            cells = ["".join(c.itertext()) for c in el if c.tag == ODF_TABLE + "table-cell"]
            if any(cells):
                lines.append("\t".join(cells))
            return
        if el.tag in (ODF_TEXT + "p", ODF_TEXT + "h") and not in_row:
            lines.append("".join(el.itertext()))
            return
        for child in el:
            walk(child, in_row)
    walk(_xml(z, "content.xml"), False)
    return "\n".join(lines)


_READERS = {".docx": _docx, ".xlsx": _xlsx, ".pptx": _pptx, ".odt": _odf, ".ods": _odf, ".odp": _odf, ".epub": _epub}


def _run(argv: list[str], timeout: float, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    """One external program: argv only, own process group, all of it killed on timeout."""
    proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True, env=env)
    try:
        out, err = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(argv, proc.returncode, out[:8 * 1024**2], err[-2000:])
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if proc.poll() is None:
            proc.wait()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


def _scratch() -> tempfile.TemporaryDirectory:
    return tempfile.TemporaryDirectory(dir=config.tmp_dir())


def _convert_legacy(name: str, data: bytes, target: str) -> bytes:
    if not config.legacy_office():
        raise Skip(f"old Office format ({parsers._ext(name)}) is not read; save it as "
                   f"{target} or set HUNTERAI_LEGACY_OFFICE=on to convert with LibreOffice")
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if exe is None:
        raise Skip(f"old Office format ({parsers._ext(name)}) needs LibreOffice, which is not installed")
    with _scratch() as tmp:
        src = Path(tmp) / ("input" + parsers._ext(name))
        src.write_bytes(data)
        try:
            r = _run([exe, f"-env:UserInstallation=file://{tmp}/profile", "--headless", "--norestore",
                      "--convert-to", target, "--outdir", tmp, str(src)], parsers.CONVERT_TIMEOUT, Path(tmp))
        except subprocess.TimeoutExpired:
            raise Skip("unreadable Office file: LibreOffice timed out") from None
        out = Path(tmp) / f"input.{target}"
        if r.returncode != 0 or not out.is_file():
            raise Skip("unreadable Office file: LibreOffice could not convert it")
        return out.read_bytes()


def parse_office(name: str, data: bytes) -> Parsed:
    ext = parsers._ext(name)
    note = None
    if ext in LEGACY:
        data, ext = _convert_legacy(name, data, LEGACY[ext]), "." + LEGACY[ext]
        note = "converted with LibreOffice from an old Office format"
    _read["bytes"] = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            text = _READERS[ext](z)
        if len(text) > parsers.MAX_OFFICE_CHARS:
            raise _Bad("the document holds too much text")
    except _Bad as e:
        raise Skip(f"unreadable Office file: {e}") from None
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, RuntimeError, NotImplementedError,
            OSError, RecursionError, MemoryError, ValueError, TypeError) as e:
        raise Skip(f"unreadable Office file: {type(e).__name__}") from None
    return Parsed("text", text, note=note)


def _ocr_file(path: Path) -> str:
    r = _run(["tesseract", str(path), "stdout", "-l", "eng"], parsers.OCR_TIMEOUT, path.parent,
             env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "OMP_THREAD_LIMIT": "1", "LANG": "C.UTF-8"})
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""


def parse_image(name: str, data: bytes) -> Parsed:
    if not config.ocr_enabled():
        raise Skip("image not read (OCR is off)")
    if shutil.which("tesseract") is None:
        raise Skip("image not read (tesseract is not installed)")
    if len(data) < parsers.MIN_OCR_IMAGE_BYTES:
        raise Skip("image not read (too small to hold a page of text)")
    if len(data) > parsers.MAX_OCR_IMAGE_BYTES:
        raise Skip(f"image not read (over {parsers.MAX_OCR_IMAGE_BYTES} bytes)")
    with _scratch() as tmp:
        f = Path(tmp) / ("image" + parsers._ext(name))
        f.write_bytes(data)
        try:
            text = _ocr_file(f)
        except subprocess.TimeoutExpired:
            raise Skip("image not read (OCR timed out)") from None
    if not text.strip():
        raise Skip("no text found in the image (OCR)")
    return Parsed("text", text, note="text read by OCR; it may contain errors")


def ocr_pdf_pages(data: bytes, pages: list[str], blank: list[int]) -> tuple[list[str], str]:
    """Fill the pages without a text layer by OCR. Returns (pages, why some may still be empty)."""
    if not config.ocr_enabled():
        return pages, "OCR is off"
    if shutil.which("tesseract") is None or shutil.which("pdftoppm") is None:
        return pages, "tesseract or pdftoppm is not installed"
    pages = list(pages)
    why = "OCR found none"
    with _scratch() as tmp:
        pdf = Path(tmp) / "doc.pdf"
        pdf.write_bytes(data)
        began = time.monotonic()
        for i in blank[:parsers.MAX_OCR_PAGES]:
            if time.monotonic() - began > parsers.OCR_DEADLINE:
                why = "OCR stopped at its time limit for one document"
                break
            try:
                r = _run(["pdftoppm", "-r", "150", "-scale-to", "2200", "-png", "-singlefile", "-f", str(i + 1), "-l", str(i + 1),
                          str(pdf), str(Path(tmp) / f"p{i}")], parsers.OCR_TIMEOUT, Path(tmp))
                png = Path(tmp) / f"p{i}.png"
                if r.returncode == 0 and png.is_file():
                    pages[i] = parsers.clean(_ocr_file(png))
                    png.unlink(missing_ok=True)
            except subprocess.TimeoutExpired:
                why = "OCR timed out on a page"
        if len(blank) > parsers.MAX_OCR_PAGES:
            why = f"OCR reads at most {parsers.MAX_OCR_PAGES} pages"
    return pages, why
