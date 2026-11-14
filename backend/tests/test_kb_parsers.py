import pytest

from hunterai.kb import parsers
from tests.kb_samples import make_pdf


@pytest.mark.parametrize("name,kind", [("a.md", "markdown"), ("A.PY", "code"), ("x/Makefile", "code"), ("a.csv", "csv"), ("a.sql", "sql"), ("a.htm", "html"),
    ("a.pdf", "pdf"), ("a.log", "text"), ("a.tar.gz", "archive"), ("a.txt.gz", "archive"), ("a.7z", "archive"), ("a.bin", None), ("noext", None), ("a.rar", None)])
def test_kind_for(name, kind):
    assert parsers.kind_for(name) == kind


def test_decode_fallbacks():
    assert parsers.decode_text("héllo".encode()) == ("héllo", "utf-8") and parsers.decode_text(b"\xff\xfeh\x00i\x00") == ("hi", "utf-16")
    assert parsers.decode_text("café".encode("cp1252")) == ("café", "cp1252")
    assert parsers.decode_text(b"\x00\x01\x02\x03" * 100) is None and parsers.decode_text(bytes(range(128, 160)) * 50) is None


def test_html_strips_markup_and_keeps_title():
    p = parsers.parse("a.html", b"<html><head><title>T &amp; U</title><style>body{color:red}</style></head>"
                                b"<body><script>evil()</script><h1>Head</h1><p>one<br>two</p></body></html>")
    assert p.kind == "html" and p.title == "T & U" and p.text.split("\n")[0] == "T & U"
    assert "evil" not in p.text and "color" not in p.text and "one\ntwo" in p.text and "Head" in p.text


def test_pdf_text_and_pages_without_text():
    p = parsers.parse("a.pdf", make_pdf(["hello pdf", "", "third page"]))
    assert "hello pdf" in p.text and "third page" in p.text and p.note == "1 of 3 pages have no text layer (OCR is off)"
    with pytest.raises(parsers.Skip) as e:
        parsers.parse("c.pdf", make_pdf(["", ""]))
    assert e.value.reason == "PDF has no text layer (OCR is off)"


def test_broken_pdf_is_a_skip_not_a_crash():
    with pytest.raises(parsers.Skip) as e:
        parsers.parse("b.pdf", b"%PDF-1.4 this is not a pdf")
    assert e.value.reason.startswith("unreadable PDF: ")


@pytest.mark.parametrize("name,data,reason", [
    ("e.txt", b"", "empty file"), ("w.md", b" \n\t\n", "no text content"), ("x.bin", bytes(range(256)) * 20, "unknown binary type (.bin)"),
    ("blob", b"\x00" * 64, "unknown binary type (no extension)"), ("fake.py", b"\x00\x01\x02" * 50, "binary or undecodable content in a .py file")])
def test_skip_reasons(name, data, reason):
    with pytest.raises(parsers.Skip) as e:
        parsers.parse(name, data)
    assert e.value.reason == reason


def test_unknown_extension_with_text_is_text():
    assert parsers.parse("scan.nmap", b"Nmap scan report\n").kind == "text"


def test_sniffs_pdf_and_html_without_an_extension():
    assert parsers.parse("report", make_pdf(["sniffed"])).kind == "pdf"
    assert parsers.parse("page", b"\xef\xbb\xbf \n<!DOCTYPE html><html><body><p>hi</p></body></html>").kind == "html"


def test_line_structure_is_preserved_for_code():
    assert parsers.parse("a.py", b"a\r\nb\rc\x07\n").text == "a\nb\nc\n"


def test_registry_covers_every_kind():
    assert set(parsers.PARSERS) == {"text", "markdown", "code", "csv", "sql", "html", "pdf"}
