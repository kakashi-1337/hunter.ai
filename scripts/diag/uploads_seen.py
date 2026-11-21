"""What the pictures attached to the last chat turn that failed are made of.

A turn with a picture can fail before the model has read a word: the program that runs the model could not
decode one of the pictures. HunterAI takes a file for a picture by its first bytes (PNG, JPEG, GIF, WebP) and
sends it to the model as it is, with every other picture of the same conversation. This lists those pictures for
the newest turn that ended in an error, and says of each what kind of file it really is: the kind, the size in
pixels, whether it is animated, progressive, of an unusual depth, or cut short.

Nothing is changed and no model is loaded or asked.

From the backend folder:   uv run python - < uploads_seen.py
CHAT: the id of another chat than the one whose turn failed last.

A diagnostic for one question, not a part of HunterAI."""
import asyncio
import os
import sys

import httpx

from hunterai import config, db
from hunterai.providers import ollama
from hunterai.repo import messages

SHOWN = 30


def png(data: bytes) -> str:
    said = []
    if data[12:16] == b"IHDR" and len(data) >= 29:
        depth, colour, interlace = data[24], data[25], data[28]
        said.append(f"{int.from_bytes(data[16:20], 'big')}x{int.from_bytes(data[20:24], 'big')} px")
        said.append(f"{depth} bits a channel" + ("" if depth == 8 else " (unusual)"))
        said.append({0: "grey", 2: "colour", 3: "a palette", 4: "grey with transparency", 6: "colour with transparency"}.get(colour, f"colour type {colour}"))
        if interlace:
            said.append("interlaced")
    if b"acTL" in data[:4096]:
        said.append("ANIMATED (APNG)")
    if not data.rstrip(b"\x00").endswith(b"IEND\xaeB`\x82"):
        said.append("CUT SHORT: the end of the file is missing")
    return "PNG, " + ", ".join(said)


def jpeg(data: bytes) -> str:
    said, i = [], 2
    while i + 9 <= len(data) and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if 0xD0 <= marker <= 0xD9 or marker == 0x01:
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            said.append(f"{int.from_bytes(data[i + 7:i + 9], 'big')}x{int.from_bytes(data[i + 5:i + 7], 'big')} px")
            said.append({0xC0: "baseline", 0xC1: "extended", 0xC2: "progressive", 0xC3: "LOSSLESS (few programs read it)"}.get(
                marker, f"ARITHMETIC or another rare coding (marker {marker:#x}; few programs read it)"))
            if data[i + 4] != 8:
                said.append(f"{data[i + 4]} bits a channel (unusual)")
            said.append({1: "grey", 3: "colour", 4: "four channels (CMYK)"}.get(data[i + 9], f"{data[i + 9]} channels"))
            break
        if length < 2:
            break
        i += 2 + length
    if not data.rstrip(b"\x00").endswith(b"\xff\xd9"):
        said.append("CUT SHORT or with something behind its end")
    return "JPEG, " + ", ".join(said)


def gif(data: bytes) -> str:
    frames, i = 0, 13
    try:
        if data[10] & 0x80:
            i += 3 * (2 << (data[10] & 7))
        while i < len(data):
            block = data[i]
            if block == 0x3B:
                break
            if block == 0x21:
                i += 2
            elif block == 0x2C:
                frames += 1
                flags = data[i + 9]
                i += 10 + (3 * (2 << (flags & 7)) if flags & 0x80 else 0) + 1
            else:
                return f"GIF, {int.from_bytes(data[6:8], 'little')}x{int.from_bytes(data[8:10], 'little')} px, DAMAGED inside"
            while data[i]:
                i += data[i] + 1
            i += 1
    except IndexError:
        return f"GIF, {int.from_bytes(data[6:8], 'little')}x{int.from_bytes(data[8:10], 'little')} px, CUT SHORT"
    return (f"GIF, {int.from_bytes(data[6:8], 'little')}x{int.from_bytes(data[8:10], 'little')} px, "
            + ("one picture" if frames <= 1 else f"ANIMATED, {frames} pictures"))


def webp(data: bytes) -> str:
    said, kind = [], data[12:16]
    if kind == b"VP8X" and len(data) >= 30:
        flags = data[20]
        said.append(f"{int.from_bytes(data[24:27], 'little') + 1}x{int.from_bytes(data[27:30], 'little') + 1} px")
        said.append("extended")
        if flags & 0x02:
            said.append(f"ANIMATED, {data.count(b'ANMF')} pictures")
        if flags & 0x10:
            said.append("with transparency")
    elif kind == b"VP8 ":
        said.append(f"{int.from_bytes(data[26:28], 'little') & 0x3FFF}x{int.from_bytes(data[28:30], 'little') & 0x3FFF} px, lossy")
    elif kind == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        said.append(f"{(bits & 0x3FFF) + 1}x{((bits >> 14) & 0x3FFF) + 1} px, lossless")
    if int.from_bytes(data[4:8], "little") + 8 > len(data):
        said.append("CUT SHORT: the file says it is longer than it is")
    return "WebP, " + ", ".join(said)


def made_of(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return png(data)
    if data.startswith(b"\xff\xd8\xff"):
        return jpeg(data)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return gif(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return webp(data)
    return f"none of PNG, JPEG, GIF, WebP (it begins with {data[:12]!r})"


async def main() -> int:
    conn = await db.connect()
    try:
        wanted = os.environ.get("CHAT", "").strip()
        if wanted:
            turn = await (await conn.execute("select id, chat_id, parent_id, model, provider, status, created_at from messages "
                                             "where chat_id=? and role='assistant' order by created_at desc limit 1", (wanted,))).fetchone()
        else:
            turn = await (await conn.execute("select id, chat_id, parent_id, model, provider, status, created_at from messages "
                                             "where role='assistant' and status='error' order by created_at desc limit 1")).fetchone()
        if turn is None:
            print("No turn of any chat has ended in an error." if not wanted else f"There is no chat {wanted}, or it has no answer yet.")
            return 1
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5, connect=2)) as client:
                version = (await client.get(f"{ollama.host()}/api/version")).json().get("version")
        except (httpx.HTTPError, ValueError):
            version = "not answering"
        print(f"The turn: {turn['created_at']} (UTC), {turn['provider']}/{turn['model']}, ended as: {turn['status']}. Ollama {version}.")
        # The messages the model was sent for that turn: the line from the first message of the chat down to this one.
        asked = [m for m in await messages.path(conn, turn["chat_id"], turn["parent_id"]) if m.role == "user"]
        seen = 0
        for n, message in enumerate(asked, 1):
            for block in message.blocks:
                if block.type != "image":
                    continue
                seen += 1
                if seen > SHOWN:
                    continue
                u = await (await conn.execute("select filename, mime, size, sha256 from uploads where id=?", (block.upload_id,))).fetchone()
                if u is None:
                    print(f"  your message {n}: a picture whose upload HunterAI no longer knows (it is not sent)")
                    continue
                path = config.uploads_dir() / u["sha256"]
                try:
                    data = path.read_bytes()
                except OSError as e:
                    print(f"  your message {n}: {u['filename']!r}, kept as {u['mime']}: THE FILE IS NOT ON DISK ({type(e).__name__}); it is not sent")
                    continue
                size = f"{len(data):,} bytes" + ("" if len(data) == u["size"] else f" (HunterAI noted {u['size']:,})")
                print(f"  your message {n}: {u['filename']!r}, kept as {u['mime']}, {size}\n      {made_of(data)}")
        if not seen:
            print("  No message of that chat has a picture attached: the error has another cause.")
        elif seen > SHOWN:
            print(f"  ... and {seen - SHOWN} more pictures")
        if seen:
            print(f"\nThat turn sent every picture above that is still on disk, whatever message it came with: {seen} at most.")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
