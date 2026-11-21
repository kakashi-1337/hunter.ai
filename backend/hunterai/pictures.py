"""A picture, looked at before a model gets it.

HunterAI takes a file for a picture by its first bytes. That says what the file claims to be, not that it can be
decoded: a download that was cut short begins like any other PNG. The program that runs a local model refuses a
request with such a file in it ("Failed to load image or audio file"), and because every picture of a conversation
goes with every turn, one such file failed every turn after it.

So a picture is decoded here first. What cannot be decoded is not sent, and the model is told so in words. What can
is sent as it is when it is a PNG or a plain JPEG, which every reader takes; a WebP, a GIF, an animation or a JPEG of
four channels goes as a PNG of its first picture, because the readers differ on those (llama.cpp's has no WebP,
Ollama's refuses an animated one, and not every cloud API takes a GIF).

Only the four kinds are opened: the library reads dozens, some of them through decoders that were not written for
files off the network."""
from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass

from PIL import Image

KINDS = ("PNG", "JPEG", "GIF", "WEBP")
MAX_PIXELS = 50_000_000         # more than this is not decoded (a 4K screen is 8 million): decoded, a pixel takes 4 bytes
PNG_AT_MOST = 4 * 1024**2       # a PNG made here that is larger goes as a JPEG instead, unless it has transparency
JPEG_QUALITY = 92
NAME_CHARS = 80
DAMAGED = "it cannot be decoded (the file is damaged or cut short)"
NOT_A_PICTURE = "it is not a PNG, JPEG, GIF or WebP picture"
_BEGINS = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")
_PLAIN = frozenset({0xC0, 0xC1, 0xC2})      # baseline, extended and progressive JPEG with Huffman coding: what every reader has
_MOVES = frozenset({"PNG", "GIF", "WEBP"})  # the kinds whose further pictures are an animation


class Unreadable(ValueError):
    """These bytes are no picture that is sent to a model. The message says why, as the rest of a sentence that
    begins with the file ("it cannot be decoded ...")."""


@dataclass(frozen=True)
class Seen:
    """A picture that can be decoded, and the bytes of it that go to a model: the file's own, or made here."""
    data: bytes
    mime: str
    size: tuple[int, int]
    frames: int             # more than one: an animation, of which the first picture is what a model gets
    made: bool


def _claims(data: bytes) -> bool:
    """Whether the first bytes are those of one of the four kinds."""
    return data.startswith(_BEGINS) or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


def _coding(data: bytes) -> tuple[int, int] | None:
    """(the marker of a JPEG's frame, its bits a channel), or None when no frame is found."""
    i = 2
    while i + 9 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xFF:                                      # a fill byte
            i += 1
        elif 0xD0 <= marker <= 0xD9 or marker == 0x01:          # markers without a length
            i += 2
        elif 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return marker, data[i + 4]
        else:
            length = int.from_bytes(data[i + 2:i + 4], "big")
            if length < 2:
                return None
            i += 2 + length
    return None


def _as_it_is(im: Image.Image, data: bytes) -> str | None:
    """The media type under which every reader takes this file, or None when it has to be made anew. A PNG; a
    JPEG of one or three channels in a common coding, and one that holds further pictures behind the first (MPO),
    which a reader does not get to."""
    if im.format == "PNG":
        return "image/png"
    if im.format in ("JPEG", "MPO") and im.mode in ("L", "RGB") and _coding(data) in {(m, 8) for m in _PLAIN}:
        return "image/jpeg"
    return None


def _made(im: Image.Image) -> tuple[bytes, str]:
    """The first picture of a file as a PNG, or as a JPEG when the PNG is large and nothing in it is transparent."""
    clear = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
    frame = im.convert("RGBA" if clear else "RGB")
    out = io.BytesIO()
    frame.save(out, "PNG")
    if out.tell() <= PNG_AT_MOST or clear:
        return out.getvalue(), "image/png"
    out = io.BytesIO()
    frame.save(out, "JPEG", quality=JPEG_QUALITY, subsampling=0)        # a colour for every pixel: small text stays sharp
    return out.getvalue(), "image/jpeg"


def read(data: bytes) -> Seen:
    """The picture as it goes to a model. Raises Unreadable, with the reason, when the bytes are none that is sent."""
    too_many = f"it has more pixels than the {MAX_PIXELS:,} that are decoded here"
    try:
        im = Image.open(io.BytesIO(data), formats=KINDS)
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):      # the library's own limit, far above this one
        raise Unreadable(too_many) from None
    except Exception:  # noqa: BLE001 - whatever a decoder says of bytes it cannot read, the answer is the same
        raise Unreadable(DAMAGED if _claims(data) else NOT_A_PICTURE) from None
    try:
        with im:
            width, height = im.size
            if width * height > MAX_PIXELS:
                raise Unreadable(f"it has {width * height:,} pixels ({width}x{height}), more than the {MAX_PIXELS:,} "
                                 "that are decoded here")
            frames = int(getattr(im, "n_frames", 1)) if im.format in _MOVES else 1
            im.load()                                   # the whole first picture: a file cut short fails here
            mime = _as_it_is(im, data)
            if mime:
                return Seen(data, mime, (width, height), frames, False)
            made, mime = _made(im)
            return Seen(made, mime, (width, height), frames, True)
    except Unreadable:
        raise
    except (Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise Unreadable(too_many) from None
    except Exception:  # noqa: BLE001
        raise Unreadable(DAMAGED) from None


def why_not(data: bytes) -> str:
    """In words, why these bytes are no picture that is sent; empty when they are one."""
    try:
        read(data)
    except Unreadable as e:
        return str(e)
    return ""


async def for_model(data: bytes) -> Seen:
    """`read`, off the event loop: decoding a large picture takes a tenth of a second and more."""
    return await asyncio.to_thread(read, data)


def omitted(name: str | None, why: str) -> str:
    """What a model is told in the place of a picture that was not sent to it."""
    name = " ".join((name or "").split())[:NAME_CHARS]
    return f'[attached picture "{name}" omitted: {why}]' if name else f"[attached picture omitted: {why}]"
