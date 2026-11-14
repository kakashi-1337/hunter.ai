"""Builders for knowledge-base test inputs."""
from __future__ import annotations


def make_pdf(pages: list[str]) -> bytes:
    """A valid PDF built by hand: one Helvetica font, one content stream per page."""
    objs: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, text in enumerate(pages):
        esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({esc}) Tj ET".encode() if text else b""
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {5 + 2 * i} 0 R "
                    f"/Resources << /Font << /F1 3 0 R >> >> >>".encode())
        objs.append(f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


import io  # noqa: E402
import os  # noqa: E402
import tarfile  # noqa: E402
import time  # noqa: E402
import zipfile  # noqa: E402
from pathlib import Path  # noqa: E402

HANG = '#!/bin/sh\nsleep 60 &\necho $! > "$FAKE_OUT/child.pid"\necho $$ > "$FAKE_OUT/main.pid"\nwait\n'
# Stands in for 7z: lists one regular file, and on "x" grows it for ever.
GROW_7Z = r'''#!/bin/sh
echo $$ > "$FAKE_OUT/main.pid"
if [ "$1" = "l" ]; then
  printf 'Path = a.txt\nSize = 10\nPacked Size = 10\nAttributes = A -rw-rw-r--\nEncrypted = -\n\n'
  exit 0
fi
for a in "$@"; do case "$a" in -o*) dir="${a#-o}";; esac; done
while true; do head -c 200000 /dev/zero >> "$dir/a.txt"; sleep 0.05; done
'''


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(zipfile.ZipInfo(name), data, zipfile.ZIP_DEFLATED)
    return buf.getvalue()


def tar_bytes(members: list[tuple[tarfile.TarInfo, bytes | None]], mode: str = "w") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as t:
        for info, data in members:
            if data is not None:
                info.size = len(data)
            t.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def put(directory, name: str, data: bytes) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / name
    p.write_bytes(data)
    return p


def assert_dead(pid: int) -> None:
    for _ in range(200):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        # a killed child that nobody has reaped yet still answers kill(0)
        try:
            if Path(f"/proc/{pid}/stat").read_text().split(") ")[1].startswith("Z"):
                return
        except (OSError, IndexError):
            return
        time.sleep(0.01)
    raise AssertionError(f"process {pid} is still alive")


import json  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402


def tree(root, files: dict[str, bytes]) -> None:
    for rel, data in files.items():
        put((Path(root) / rel).parent, Path(rel).name, data)


def reader(data: bytes):
    buf = io.BytesIO(data)

    async def read(n: int) -> bytes:
        return buf.read(n)
    return read


async def collect(payload: dict):
    from hunterai.kb import sources
    from hunterai.kb.archives import Found
    found, problems = [], []
    async for x in sources.discover(payload):
        (found if isinstance(x, Found) else problems).append(x)
    return found, problems


def folder_payload(root, collection_id: str = "col1") -> dict:
    from hunterai.kb import sources
    return {"collection_id": collection_id, "staging_id": sources.new_staging(collection_id),
            "source": {"kind": "folder", "path": str(root)}}


def age_staging(staging_id: str, seconds: float) -> None:
    from hunterai.kb import sources
    f = sources.staging_path(staging_id) / "meta.json"
    meta = json.loads(f.read_text())
    for key in ("created_at", "committed_at"):
        if meta.get(key):
            meta[key] = (datetime.fromisoformat(meta[key]) - timedelta(seconds=seconds)).isoformat()
    f.write_text(json.dumps(meta))


class Crash(BaseException):
    """Simulated process death: nothing may catch it."""


async def make_collection(conn, name: str = "notes", dim: int = 64):
    from hunterai.kb.vectors import get_store
    from hunterai.repo import kb
    col = await kb.create_collection(conn, name, f"fake:{dim}", dim)
    await get_store().ensure_collection(kb.vector_name(col.id), dim)
    return col


async def ingest_folder(conn, col, root, **run_kw):
    from hunterai.kb import pipeline, sources
    from hunterai.repo import jobs
    job = await jobs.enqueue(conn, "ingest", {"collection_id": col.id, "staging_id": sources.new_staging(col.id),
                                               "source": {"kind": "folder", "path": str(root)}})
    job = await jobs.claim(conn, ["ingest"])
    state = await pipeline.run_job(conn, job, **run_kw)
    return state, await jobs.get(conn, job.id)


class EmbedSpy:
    def __init__(self):
        self.batches: list[list[str]] = []
        self.fail: Exception | None = None
        self.fail_times: int | None = None
        self.on_call = None

    @property
    def texts(self) -> list[str]:
        return [t for b in self.batches for t in b]


class StubStore:
    """A vector store whose search returns a fixed list per collection name."""

    def __init__(self, results: dict[str, list[tuple[str, float]]]):
        self.results = results

    async def search(self, name, vector, limit):
        return self.results.get(name, [])[:limit]


async def ingest_texts(conn, col, tmp_path, files: dict[str, str]) -> dict:
    from hunterai.repo import kb
    root = tmp_path / f"texts-{col.id[:6]}-{len(files)}-{abs(hash(tuple(files))) % 10**6}"
    tree(root, {n: t.encode() for n, t in files.items()})
    state, _ = await ingest_folder(conn, col, root)
    assert state == "done"
    return {n: await kb.find_document(conn, col.id, f"{root}/{n}") for n in files}


def _zip(members: dict[str, str]) -> bytes:
    return zip_bytes({k: v.encode() for k, v in members.items()})


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
P = 'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def make_docx(paragraphs: list[str]) -> bytes:
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    return _zip({"[Content_Types].xml": "<Types/>", "word/document.xml": f"<w:document {W}><w:body>{body}</w:body></w:document>"})


def make_xlsx(sheets: dict[str, list[list[str]]]) -> bytes:
    strings: list[str] = []
    files = {"[Content_Types].xml": "<Types/>"}
    names = ""
    for n, (name, rows) in enumerate(sheets.items(), 1):
        names += f'<sheet name="{name}" sheetId="{n}"/>'
        xml = ""
        for row in rows:
            cells = ""
            for v in row:
                if v.isdigit():
                    cells += f"<c><v>{v}</v></c>"
                else:
                    strings.append(v)
                    cells += f'<c t="s"><v>{len(strings) - 1}</v></c>'
            xml += f"<row>{cells}</row>"
        files[f"xl/worksheets/sheet{n}.xml"] = f"<worksheet {S}><sheetData>{xml}</sheetData></worksheet>"
    files["xl/workbook.xml"] = f"<workbook {S}><sheets>{names}</sheets></workbook>"
    files["xl/sharedStrings.xml"] = f"<sst {S}>" + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>"
    return _zip(files)


def make_pptx(slides: list[list[str]]) -> bytes:
    files = {"[Content_Types].xml": "<Types/>"}
    for n, lines in enumerate(slides, 1):
        body = "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in lines)
        files[f"ppt/slides/slide{n}.xml"] = f"<p:sld {P} {A}><p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:sld>"
    return _zip(files)


def make_odt(paragraphs: list[str]) -> bytes:
    ns = 'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"'
    body = "".join(f"<text:p>{p}</text:p>" for p in paragraphs)
    return _zip({"mimetype": "application/vnd.oasis.opendocument.text",
                 "content.xml": f"<office:document-content {ns}><office:body><office:text>{body}</office:text></office:body></office:document-content>"})
