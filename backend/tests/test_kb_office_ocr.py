import shutil
import subprocess

import pytest

from hunterai.kb import parsers
from tests.kb_samples import HANG, make_docx, make_odt, make_pdf, make_pptx, make_xlsx, zip_bytes

HAVE_OCR = shutil.which("tesseract") is not None and shutil.which("pdftoppm") is not None


def test_docx_paragraphs_become_lines():
    p = parsers.parse("report.docx", make_docx(["IDOR in invoices", "Steps to reproduce"]))
    assert p.kind == "text" and p.text.split("\n") == ["IDOR in invoices", "Steps to reproduce"]


def test_xlsx_sheets_rows_and_shared_strings():
    p = parsers.parse("hosts.xlsx", make_xlsx({"Hosts": [["host", "port"], ["acme.test", "443"]], "Notes": [["todo"]]}))
    assert p.text == "# Hosts\nhost\tport\nacme.test\t443\n\n# Notes\ntodo"


def test_pptx_slides_in_order():
    p = parsers.parse("deck.pptx", make_pptx([["Title", "sub"], ["Second slide"]] + [[f"s{i}"] for i in range(3, 12)]))
    lines = p.text.split("\n")
    assert lines[:3] == ["## Slide 1", "Title", "sub"] and "## Slide 2\nSecond slide" in p.text
    assert p.text.index("## Slide 2") < p.text.index("## Slide 10") < p.text.index("## Slide 11")


def test_odt_paragraphs():
    assert parsers.parse("notes.odt", make_odt(["one", "two"])).text == "one\ntwo"


@pytest.mark.parametrize("name,data,reason", [
    ("bad.docx", b"PK\x03\x04garbage", "unreadable Office file: BadZipFile"),
    ("empty.docx", make_docx([]), "no text content"),
    ("other.docx", zip_bytes({"hello.txt": b"x"}), "unreadable Office file: not a docx document"),
    ("ent.docx", zip_bytes({"word/document.xml": b'<!DOCTYPE x [<!ENTITY a "aaaa">]><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">&a;</w:document>'}),
     "unreadable Office file: XML with a document type is not accepted"),
])
def test_bad_office_files_are_skips(name, data, reason):
    with pytest.raises(parsers.Skip) as e:
        parsers.parse(name, data)
    assert e.value.reason == reason


def test_office_member_size_is_capped(monkeypatch):
    monkeypatch.setattr(parsers, "MAX_MEMBER_BYTES", 1000)
    with pytest.raises(parsers.Skip) as e:
        parsers.parse("big.docx", make_docx(["x" * 5000]))
    assert e.value.reason == "unreadable Office file: a part of the document is too large"


def test_kinds_for_new_formats():
    for name in ("a.docx", "a.xlsx", "a.pptx", "a.odt", "a.ods", "a.odp", "a.doc", "a.xls", "a.ppt", "a.rtf"):
        assert parsers.kind_for(name) == "office", name
    for name in ("a.png", "a.JPG", "a.jpeg", "a.webp", "a.tiff", "a.bmp", "a.gif"):
        assert parsers.kind_for(name) == "image", name


def test_images_are_skipped_with_a_reason_when_ocr_is_off():
    with pytest.raises(parsers.Skip) as e:
        parsers.parse("shot.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 64)
    assert e.value.reason == "image not read (OCR is off)"


def test_pdf_without_text_names_ocr_being_off():
    with pytest.raises(parsers.Skip) as e:
        parsers.parse("scan.pdf", make_pdf(["", ""]))
    assert e.value.reason == "PDF has no text layer (OCR is off)"


def page_png(tmp_path, text):
    (tmp_path / "t.pdf").write_bytes(make_pdf([text]))
    subprocess.run(["pdftoppm", "-r", "150", "-png", "-singlefile", str(tmp_path / "t.pdf"), str(tmp_path / "page")], check=True)
    return (tmp_path / "page.png").read_bytes()


@pytest.mark.skipif(not HAVE_OCR, reason="tesseract or pdftoppm not installed")
def test_ocr_reads_an_image_and_a_scanned_pdf_page(tmp_path, monkeypatch):
    monkeypatch.setenv("HUNTERAI_OCR", "on")
    png = page_png(tmp_path, "Invoice 1338 belongs to user B")
    p = parsers.parse("shot.png", png)
    assert "Invoice 1338" in p.text and p.note == "text read by OCR; it may contain errors"
    blank = parsers.parse("mixed.pdf", make_pdf(["typed page", ""]))
    assert "typed page" in blank.text and blank.note == "1 of 2 pages have no text (OCR found none)"


def test_a_hanging_ocr_tool_is_killed_and_the_image_is_skipped(fake_bin, tmp_path, monkeypatch):
    from tests.kb_samples import assert_dead
    out = fake_bin("tesseract", HANG.replace("$FAKE_OUT", str(tmp_path)))      # OCR tools get a bare environment
    monkeypatch.setenv("HUNTERAI_OCR", "on"); monkeypatch.setattr(parsers, "OCR_TIMEOUT", 0.3)
    with pytest.raises(parsers.Skip) as e:
        parsers.parse("shot.png", b"\x89PNG\r\n\x1a\n" + b"\0" * 20_000)
    assert e.value.reason == "image not read (OCR timed out)"
    for name in ("main.pid", "child.pid"):
        assert_dead(int((out / name).read_text()))


# ---- review fixes

def test_office_totals_are_bounded(monkeypatch):
    monkeypatch.setattr(parsers, "MAX_OFFICE_CHARS", 500)
    with pytest.raises(parsers.Skip) as e:
        parsers.parse("big.xlsx", make_xlsx({"S": [["x" * 80] * 3 for _ in range(10)]}))
    assert e.value.reason == "unreadable Office file: the document holds too much text"
    monkeypatch.setattr(parsers, "MAX_OFFICE_CHARS", 10_000_000); monkeypatch.setattr(parsers, "MAX_OFFICE_BYTES", 300)
    with pytest.raises(parsers.Skip) as e:
        parsers.parse("deck.pptx", make_pptx([["slide text"] for _ in range(10)]))
    assert e.value.reason == "unreadable Office file: a part of the document is too large"


def test_utf16_xml_is_refused():
    xml = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY a "b">]><d/>'.encode("utf-16")
    with pytest.raises(parsers.Skip) as e:
        parsers.parse("u.docx", zip_bytes({"word/document.xml": xml}))
    assert e.value.reason == "unreadable Office file: XML that is not UTF-8 is not accepted"


def test_old_office_formats_are_not_converted_unless_asked():
    with pytest.raises(parsers.Skip) as e:
        parsers.parse("old.doc", b"\xd0\xcf\x11\xe0" + b"\0" * 100)
    assert "HUNTERAI_LEGACY_OFFICE=on" in e.value.reason and "(.doc)" in e.value.reason


def test_tiny_and_huge_images_are_not_sent_to_ocr(monkeypatch, fake_bin):
    fake_bin("tesseract", "#!/bin/sh\necho should-not-run\n")
    monkeypatch.setenv("HUNTERAI_OCR", "on")
    with pytest.raises(parsers.Skip) as e:
        parsers.parse("icon.png", b"\x89PNG" + b"\0" * 100)
    assert e.value.reason == "image not read (too small to hold a page of text)"
    monkeypatch.setattr(parsers, "MAX_OCR_IMAGE_BYTES", 50_000)
    with pytest.raises(parsers.Skip) as e:
        parsers.parse("huge.png", b"\x89PNG" + b"\0" * 60_000)
    assert e.value.reason == "image not read (over 50000 bytes)"
