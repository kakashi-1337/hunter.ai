import pytest

from hunterai import config, uploads
from hunterai.types import FileBlock, ImageBlock

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 16


def stored_files():
    return [p for p in config.uploads_dir().iterdir() if p.is_file()]


@pytest.mark.parametrize("data,mime", [(PNG, "image/png"), (JPG, "image/jpeg"),
                                       (GIF, "image/gif"), (WEBP, "image/webp")])
async def test_images_detected_by_content_not_name(conn, data, mime):
    u = await uploads.save(conn, None, "notes.txt", data)
    assert u.mime == mime
    assert await uploads.to_block(conn, u.id) == ImageBlock(upload_id=u.id, mime=mime)


async def test_text_file_becomes_file_block(conn):
    u = await uploads.save(conn, "c1", "req.http", "GET / HTTP/1.1\nHost: ünï.test\n".encode())
    b = await uploads.to_block(conn, u.id)
    assert isinstance(b, FileBlock) and b.filename == "req.http" and not b.truncated
    assert b.text == "GET / HTTP/1.1\nHost: ünï.test\n" and b.size == u.size
    assert u.mime.startswith("text/") and u.chat_id == "c1"


async def test_long_text_is_truncated_and_marked(conn):
    u = await uploads.save(conn, None, "big.log", ("a" * 250_000).encode())
    b = await uploads.to_block(conn, u.id)
    assert len(b.text) == 200_000 and b.truncated is True


async def test_binary_file_has_no_text(conn):
    data = bytes(range(256)) * 8
    u = await uploads.save(conn, None, "fw.bin", data)
    b = await uploads.to_block(conn, u.id)
    assert isinstance(b, FileBlock) and b.text is None and b.size == len(data)
    assert u.mime == "application/octet-stream"


async def test_known_binary_extension_keeps_its_mime(conn):
    u = await uploads.save(conn, None, "report.pdf", b"%PDF-1.7\n\x00\x01\x02\xff\xfe")
    assert u.mime == "application/pdf"
    assert (await uploads.to_block(conn, u.id)).text is None


async def test_hostile_filename_never_reaches_the_filesystem(conn):
    u = await uploads.save(conn, None, "../../etc/passwd", b"hello")
    files = stored_files()
    assert len(files) == 1 and files[0].parent == config.uploads_dir()
    assert files[0].name == u.sha256 and len(u.sha256) == 64
    assert u.filename == "passwd"
    assert await uploads.load_bytes(conn, u.id) == b"hello"


@pytest.mark.parametrize("name,expected", [("", "upload"), ("   ", "upload"), ("a/b\\c.txt", "c.txt"),
                                           ("x\x00y.txt", "xy.txt"), ("n" * 400 + ".txt", None)])
async def test_filename_cleaned(conn, name, expected):
    u = await uploads.save(conn, None, name, b"x")
    assert len(u.filename) <= 255 and "/" not in u.filename and "\x00" not in u.filename
    if expected:
        assert u.filename == expected


async def test_identical_content_stored_once(conn):
    a = await uploads.save(conn, None, "a.txt", b"same")
    b = await uploads.save(conn, None, "b.txt", b"same")
    assert a.id != b.id and a.sha256 == b.sha256 and len(stored_files()) == 1


async def test_too_large_rejected(conn, monkeypatch):
    monkeypatch.setattr(uploads, "MAX_BYTES", 10)
    with pytest.raises(ValueError, match="file too large"):
        await uploads.save(conn, None, "big", b"x" * 11)
    assert stored_files() == []


def test_default_limit_is_50_mb():
    assert uploads.MAX_BYTES == 50 * 1024 * 1024


async def test_empty_file_allowed(conn):
    u = await uploads.save(conn, None, "empty.txt", b"")
    b = await uploads.to_block(conn, u.id)
    assert b.text == "" and b.size == 0


async def test_unknown_id(conn):
    assert await uploads.get(conn, "nope") is None
    with pytest.raises(KeyError):
        await uploads.load_bytes(conn, "nope")
    with pytest.raises(KeyError):
        await uploads.to_block(conn, "nope")
