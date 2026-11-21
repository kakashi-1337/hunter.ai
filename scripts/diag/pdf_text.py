"""How much of the text taken out of the PDFs of a collection is garbled.

A PDF holds letters at positions, not words. Taken out of some PDFs, the words come glued together or cut apart
("blockisthe con dit ion,post-statement"), and a passage of that is found by neither half of the search: its words
are not the words anybody asks with. This counts how many passages are like that. Nothing is changed.

Two signs are counted in each passage of 50 words or more:
- glued: the share of its words that are 15 letters or longer and all lower case behind their first letter
  ("andbodyoftheforstatement"; a long name in source code has capitals or underscores inside it);
- cut: the share of its words that are 1 to 3 letters long and none of the common short words of English.
A passage counts as garbled when the first is GLUED or more, or when both are there (BOTH). The second alone is no
proof, because a listing of source code looks like that too: such passages are counted apart, as "perhaps cut
apart". The same is counted for pages and Markdown files, every 50th passage: what it calls garbled there is what
the measure gets wrong.

From the backend folder:   uv run python - < pdf_text.py        (WHATIF_COLLECTION: another collection than "RAG")

A diagnostic, not a part of HunterAI. Checked on 1,555 passages of real English text and source code: one counts
as garbled. With words glued and cut on purpose (60% of the gaps closed, 40% of the longer words cut) 104 of 115
passages of prose count as garbled; with 35% and 25%, 27 of 116; with 15% and 10%, none. So what it counts is
heavy damage, and the least there is. A line of a PDF that is garbled has 14% glued and 36% cut words."""
import asyncio
import os
import re
import sqlite3
import sys

from hunterai import config, db
from hunterai.kb import ragtest
from hunterai.repo import kb

NAME = os.environ.get("WHATIF_COLLECTION", "RAG")
GLUED = 0.04                        # the share of glued words from which a passage counts as garbled
BOTH = (0.015, 0.15)                # or this share of glued words together with this share of cut words
CUT = 0.25                          # the share of cut words from which one that is not glued is "perhaps cut apart"
MIN_WORDS = 50
MOSTLY = 0.3                        # a document counts as garbled when this share of its passages is
SHOWN = 25
_WORD = re.compile(r"[A-Za-z]+")
SHORT = frozenset(
    ["a", "i", "an", "as", "at", "be", "by", "do", "go", "he", "if", "in", "is", "it", "me", "my", "no", "of", "on", "or", "so", "to", "up", "us", "we", "am", "ad", "vs", "id", "eg", "ie", "ok", "tv", "pc", "os", "ui", "io", "the", "and", "for", "are", "but", "not", "you", "all", "can", "had", "her", "was", "one", "our", "out", "day", "get", "has", "him", "his", "how", "man", "new", "now", "old", "see", "two", "way", "who", "did", "its", "let", "put", "say", "she", "too", "use", "any", "may", "per", "set", "yet", "via", "own", "few", "off", "due", "add", "etc", "pre", "non", "sub", "run", "try", "end", "top", "key", "why", "yes", "big", "low", "web", "app", "api", "url", "sql", "css", "xml", "dns", "ssh", "ssl", "tls", "tcp", "udp", "cpu", "ram", "git", "log", "bug", "fix", "job", "map", "tag", "bit", "box", "var", "int", "str", "len", "max", "min", "sum", "def", "nil", "err", "out", "src", "lib", "bin", "tmp", "dev", "doc", "ids", "row", "col", "ref", "arg", "env", "raw", "hex", "pdf", "csv", "yml", "ini", "cfg", "php", "asp", "jsp", "aws", "gcp", "vpn", "lan", "wan", "nat", "mac", "ip", "vs", "ex", "mr", "ms", "dr", "st", "th", "nd", "rd", "em", "en", "fr", "de", "es", "it", "pt", "ru", "zh", "ja", "six", "ten", "age", "ago", "air", "art", "bad", "car", "cut", "far", "got", "hit", "hot", "law", "led", "lot", "met", "mix", "net", "nor", "odd", "oil", "pay", "ran", "red", "sat", "saw", "sea", "sit", "ski", "sky", "son", "sun", "tax", "tea", "ten", "tie", "tip", "ton", "toy", "war", "wet", "win", "won", "yes", "ago", "aim", "ask", "bar", "bed", "bet", "bid", "buy", "cap", "cat", "cry", "die", "dog", "dry", "due", "ear", "eat", "egg", "era", "eye", "fan", "fat", "fee", "fit", "fly", "fun", "gap", "gas", "god", "guy", "gym", "hat", "ice", "ill", "inn", "jet", "joy", "kid", "lab", "lap", "lay", "leg", "lie", "lip", "mad", "mom", "mud", "nut", "owe", "pan", "pen", "pet", "pie", "pin", "pit", "pop", "pot", "rat", "raw", "rid", "rob", "rod", "row", "rub", "sad", "shy", "sir", "tap", "tar", "tea", "toe", "van", "vet", "vow", "wax", "web", "wit", "yet", "zip", "zoo"])


def signs(text: str):
    """(the share of glued words, the share of cut words) of a passage, or None when it has too few words to say."""
    words = _WORD.findall(text)
    if len(words) < MIN_WORDS:
        return None
    glued = sum(1 for w in words if len(w) >= 15 and w[1:].islower()) / len(words)
    cut = sum(1 for w in words if len(w) <= 3 and w.lower() not in SHORT) / len(words)
    return glued, cut


def is_garbled(found) -> bool:
    return found[0] >= GLUED or (found[0] >= BOTH[0] and found[1] >= BOTH[1])


def garbled(text: str):
    found = signs(text)
    return None if found is None else is_garbled(found)


async def main() -> int:
    conn = await db.connect()
    try:
        col = next((c for c in await kb.list_collections(conn) if c.name == NAME), None)
    finally:
        await conn.close()
    if col is None:
        print(f'There is no collection named "{NAME}".')
        return 1
    src = sqlite3.connect(f"file:{config.db_path()}?mode=ro", uri=True)
    docs = {row[0]: row[1] for row in src.execute(
        "select id, source_uri from kb_documents where collection_id=? and status='ok'", (col.id,))}
    pdfs = {did for did, uri in docs.items() if uri.lower().split("!/")[-1].endswith(".pdf")}
    prose = {did for did, uri in docs.items() if did not in pdfs and ragtest.readable(uri)}
    per: dict[str, list] = {}           # document -> [passages judged, garbled, sum of glued, sum of cut, a sample, perhaps cut]
    base = [0, 0, 0]
    seen = 0
    for n, did, text in src.execute("select n, document_id, text from kb_chunks where collection_id=? order by n", (col.id,)):
        seen += 1
        if seen % 100000 == 0:
            print(f"  read {seen:,} passages", file=sys.stderr, flush=True)
        if did in pdfs:
            found = signs(text)
            if found is None:
                continue
            bad = is_garbled(found)
            row = per.setdefault(did, [0, 0, 0.0, 0.0, "", 0])
            row[0] += 1
            row[1] += int(bad)
            row[2] += found[0]
            row[3] += found[1]
            row[5] += int(not bad and found[1] >= CUT)
            if bad and not row[4]:
                row[4] = " ".join(text.split())[:110]
        elif did in prose and n % 50 == 0:
            found = signs(text)
            if found is not None:
                base[0] += 1
                base[1] += int(is_garbled(found))
                base[2] += int(not is_garbled(found) and found[1] >= CUT)
    src.close()
    judged = sum(r[0] for r in per.values())
    bad = sum(r[1] for r in per.values())
    mostly = {did: r for did, r in per.items() if r[0] and r[1] / r[0] >= MOSTLY}
    print(f'Collection "{col.name}": {len(pdfs):,} PDFs among its {len(docs):,} documents, {judged:,} passages of '
          f"{MIN_WORDS} words or more in them.")
    if not judged:
        return 0
    perhaps = sum(r[5] for r in per.values())
    print(f"Garbled by this measure: {bad:,} of those passages ({bad / judged:.1%}). {len(mostly):,} PDFs are garbled "
          f"for {MOSTLY:.0%} of their passages or more, and hold {sum(r[1] for r in mostly.values()):,} of them.")
    print(f"Perhaps cut apart, and not glued: {perhaps:,} more ({perhaps / judged:.1%}). A listing of source code looks "
          "like that too.")
    if base[0]:
        print(f"For comparison, pages and Markdown files (every 50th passage, {base[0]:,} of them): {base[1]:,} count "
              f"as garbled ({base[1] / base[0]:.1%}) and {base[2]:,} as perhaps cut apart ({base[2] / base[0]:.1%}). "
              "That is what the measure says of text that is whole.")
    worst = sorted(mostly.items(), key=lambda item: -item[1][1])[:SHOWN]
    if worst:
        print(f"\nThe {len(worst)} PDFs with the most garbled passages (glued, cut: the average share of such words):")
        print(f"  {'passages':>8} {'garbled':>8} {'glued':>6} {'cut':>6}   PDF")
        for did, (count, wrong, glued, cut, sample, _) in worst:
            print(f"  {count:>8,} {wrong:>8,} {glued / count:>6.1%} {cut / count:>6.1%}   {ragtest.named(docs[did])}\n"
                  f"  {'':>31}   \"{sample}\"")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
