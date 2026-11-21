"""A picture is looked at before a model gets it: what cannot be decoded is not sent, and what a program that
runs a model may not read (WebP, GIF, an animation, a JPEG of four channels) goes as a PNG of the same picture."""
import base64
import io
import threading

import pytest
from PIL import Image

from hunterai import pictures


def picture(kind="PNG", size=(320, 200), mode="RGB", colour=(200, 30, 30), **save):
    im = Image.new(mode, size, colour)
    out = io.BytesIO()
    im.save(out, kind, **save)
    return out.getvalue()


def animation(kind):
    frames = [Image.new("RGB", (64, 48), c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255))]
    out = io.BytesIO()
    frames[0].save(out, kind, save_all=True, append_images=frames[1:], duration=100)
    return out.getvalue()


def shown(data):
    """What a decoder makes of bytes: (kind, size, the colour of the first pixel)."""
    im = Image.open(io.BytesIO(data))
    return im.format, im.size, im.convert("RGB").getpixel((0, 0))


def refused(data):
    """Why bytes are no picture that is sent."""
    with pytest.raises(pictures.Unreadable) as e:
        pictures.read(data)
    return str(e.value)


@pytest.mark.parametrize("data", [picture("PNG"), picture("PNG", mode="RGBA", colour=(1, 2, 3, 128)), picture("PNG", mode="L", colour=90),
                                  picture("JPEG", quality=90), picture("JPEG", quality=90, progressive=True),
                                  picture("JPEG", mode="L", colour=90)])
def test_a_png_and_a_plain_jpeg_go_to_a_model_as_they_are(data):
    seen = pictures.read(data)
    assert seen.data is data and seen.made is False and seen.size == (320, 200)
    assert seen.mime == ("image/png" if data.startswith(b"\x89PNG") else "image/jpeg")


@pytest.mark.parametrize("data", [picture("WEBP", quality=95), picture("WEBP", lossless=True), picture("GIF")])
def test_a_webp_and_a_gif_go_as_a_png_of_the_same_picture(data):
    seen = pictures.read(data)
    kind, size, first = shown(seen.data)
    assert seen.made and seen.mime == "image/png" and kind == "PNG" and size == (320, 200) == seen.size
    assert all(abs(a - b) <= 6 for a, b in zip(first, (200, 30, 30)))       # the same picture: a lossy WebP is off by little


@pytest.mark.parametrize("kind", ["GIF", "WEBP"])
def test_of_an_animation_the_first_picture_goes(kind):
    seen = pictures.read(animation(kind))
    _, size, first = shown(seen.data)
    assert seen.made and seen.mime == "image/png" and seen.frames == 3
    assert size == (64, 48) and first[0] > 240 and first[1] < 20 and first[2] < 20      # the red one, which is the first


def test_a_jpeg_of_four_channels_goes_as_one_every_reader_takes():
    seen = pictures.read(picture("JPEG", mode="CMYK", colour=(0, 200, 200, 0), quality=95))
    kind, size, first = shown(seen.data)
    assert seen.made and kind == "PNG" and size == (320, 200) and first[0] > 200 and first[1] < 90


def test_transparency_is_kept_in_what_is_made():
    seen = pictures.read(picture("WEBP", mode="RGBA", colour=(10, 20, 30, 0), lossless=True))
    assert Image.open(io.BytesIO(seen.data)).convert("RGBA").getpixel((0, 0))[3] == 0
    drawn = Image.new("P", (32, 24), 1)            # a GIF says it with one place of its palette: here the first white, not the second
    drawn.putpalette([255, 255, 255, 200, 30, 30, 255, 255, 255])
    drawn.putpixel((5, 5), 0)
    drawn.putpixel((6, 6), 2)
    out = io.BytesIO()
    drawn.save(out, "GIF", transparency=0)
    made = Image.open(io.BytesIO(pictures.read(out.getvalue()).data)).convert("RGBA")
    assert made.getpixel((5, 5))[3] == 0 and made.getpixel((6, 6)) == (255, 255, 255, 255) and made.getpixel((0, 0)) == (200, 30, 30, 255)


@pytest.mark.parametrize("name, data", [
    ("a PNG cut in half", picture("PNG", size=(800, 600))[:400]),
    ("a PNG with its beginning only", picture("PNG")[:40]),
    ("a PNG whose inside is noise", picture("PNG")[:33] + bytes(range(256)) * 20),
    ("a JPEG cut in half", picture("JPEG", size=(800, 600), quality=95)[:3000]),
    ("a WebP cut in half", picture("WEBP", size=(800, 600), quality=95)[:600]),
    ("a GIF with its beginning only", picture("GIF")[:20]),
    ("no picture", b"GET / HTTP/1.1\r\nHost: acme.test\r\n\r\n"),
    ("nothing", b""),
])
def test_a_file_that_cannot_be_decoded_is_not_a_picture(name, data):
    assert refused(data), name


def test_a_kind_that_is_not_one_of_the_four_is_not_read(tmp_path):
    """Only what HunterAI takes for a picture is decoded: the library reads dozens of kinds, some of them from
    programs that were never meant for files off the network."""
    for kind in ("BMP", "TIFF", "PPM"):
        assert "not a PNG, JPEG, GIF or WebP" in refused(picture(kind)), kind


def test_a_picture_of_more_pixels_than_the_limit_is_not_decoded(monkeypatch):
    data = picture("PNG", size=(400, 300))
    assert pictures.read(data).size == (400, 300)
    monkeypatch.setattr(pictures, "MAX_PIXELS", 400 * 300 - 1)
    assert "120,000 pixels" in refused(data) and "400x300" in refused(data)


def test_a_picture_the_library_itself_calls_too_large_is_not_decoded(monkeypatch):
    """The library warns from its own limit on and refuses at twice that. This one is under both, so that the words
    said here are the ones a reader gets."""
    assert pictures.MAX_PIXELS < Image.MAX_IMAGE_PIXELS
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    assert "more pixels" in refused(picture("PNG", size=(400, 300)))


def test_what_is_wrong_with_a_file_is_said_in_words(monkeypatch):
    assert pictures.why_not(picture("PNG")) == ""
    assert "damaged or cut short" in pictures.why_not(picture("PNG", size=(800, 600))[:400])
    assert "damaged or cut short" in pictures.why_not(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)


def test_a_large_picture_made_here_is_a_jpeg_unless_it_has_transparency(monkeypatch):
    """A PNG of a photograph is many times its JPEG, and a request carries every picture of the conversation."""
    noisy = Image.effect_noise((600, 400), 80).convert("RGB")
    out = io.BytesIO()
    noisy.save(out, "WEBP", lossless=True)
    monkeypatch.setattr(pictures, "PNG_AT_MOST", 20_000)
    seen = pictures.read(out.getvalue())
    assert seen.made and seen.mime == "image/jpeg" and shown(seen.data)[:2] == ("JPEG", (600, 400))
    part = noisy.convert("RGBA")
    part.putpixel((0, 0), (0, 0, 0, 0))          # one pixel that is see-through: a WebP that has none is stored without the channel
    clear = io.BytesIO()
    part.save(clear, "WEBP", lossless=True)
    kept = pictures.read(clear.getvalue())
    assert kept.mime == "image/png" and Image.open(io.BytesIO(kept.data)).getpixel((0, 0))[3] == 0


async def test_it_is_looked_at_off_the_event_loop(monkeypatch):
    seen = await pictures.for_model(picture("WEBP"))
    assert seen.made and seen.mime == "image/png"
    with pytest.raises(pictures.Unreadable, match="damaged or cut short"):
        await pictures.for_model(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    where = []
    monkeypatch.setattr(pictures, "read", lambda data: where.append(threading.current_thread()))
    await pictures.for_model(b"x")
    assert where and where[0] is not threading.main_thread()


def test_a_photograph_that_holds_more_than_one_picture_goes_as_it_is():
    """A telephone stores a second picture behind the first (a gain map, a depth map). Every reader stops at the
    end of the first, so the file is an ordinary JPEG to them."""
    first, second = Image.new("RGB", (320, 200), (200, 30, 30)), Image.new("RGB", (160, 100), (0, 0, 0))
    out = io.BytesIO()
    first.save(out, "MPO", save_all=True, append_images=[second])
    data = out.getvalue()
    assert Image.open(io.BytesIO(data)).format == "MPO"
    seen = pictures.read(data)
    assert seen.data is data and not seen.made and seen.mime == "image/jpeg" and seen.frames == 1


def test_what_a_model_is_told_in_the_place_of_a_picture():
    said = pictures.omitted("shot.png", pictures.DAMAGED)
    assert said == '[attached picture "shot.png" omitted: it cannot be decoded (the file is damaged or cut short)]'
    assert pictures.omitted(None, pictures.DAMAGED).startswith("[attached picture omitted: ")
    assert len(pictures.omitted("a" * 500 + "]\n[system", pictures.DAMAGED)) < 220 and "\n" not in pictures.omitted("a\nb", "x")


# A JPEG in the lossless coding (its frame is marked C3), 32x24 of one red, as another library wrote it: the one
# used here does not write this coding, and decodes it.
LOSSLESS = base64.b64decode(
    "/9j/7gAOQWRvYmUAZAAAAAAA/8MAEQgAGAAgA1IRAEcRAEIRAP/EABUAAQEAAAAAAAAAAAAAAAAAAAAH/9oADANSAEcAQgABAACkR2Og"
    + "A" * 382 + "D/2Q==")


def test_a_jpeg_in_a_coding_few_readers_have_goes_as_a_png():
    """Lossless and arithmetic coding: the decoder of llama.cpp has neither."""
    assert Image.open(io.BytesIO(LOSSLESS)).format == "JPEG" and pictures._coding(LOSSLESS) == (0xC3, 8)
    seen = pictures.read(LOSSLESS)
    assert seen.made and shown(seen.data) == ("PNG", (32, 24), (200, 30, 30))
