"""What this machine has to start a llama-server with: the program, the graphics cards it sees, and the model files
Ollama keeps. From these comes the command that starts llama-server with a model Ollama has, for the owner to run.

Nothing is started here and nothing is installed. The one program that is run is a llama-server that was found,
asked for its list of cards (--list-devices), which loads no model.

What was seen, on 26 November 2026:

- llama-server (built from the source of llama.cpp at 074f1f3) answers --list-devices on its standard output with a
  line "Available devices:" and then either "  (none)" or one line for each card: its name, what it is, and its memory
  in MiB, whole and free. A build for the processor alone says "(none)", and so did the one Unsloth Studio brought to
  the owner's laptop, where a 9B model then wrote 3.6 tokens a second. A flag it does not know ends it with a status
  of 1. Its help names every flag used here; -ngl is "auto" by itself, so none is given. It loads a model file whose
  name does not end in .gguf, which is how Ollama names its files.
- Ollama 0.40.18 on the owner's laptop keeps a llama-server of its own in lib/ollama beside the folder of its program
  (/usr/local/lib/ollama), with the libraries it needs, and a folder for each part for a graphics card: cuda_v12,
  cuda_v13, vulkan. Started with LD_LIBRARY_PATH (lib/ollama and one such folder) and GGML_BACKEND_PATH (the
  libggml-*.so of that folder), which is what Ollama's source gives it (llm/llama_server.go), it listed "CUDA0: NVIDIA
  GeForce RTX 5080 Laptop GPU (15839 MiB, 6015 MiB free)", loaded a 9B model, and the card then held 7,578 MiB for it.
- Ollama (built from its source at 42ac2c0) answers POST /api/show with the Modelfile text of a model. After three
  comment lines its first lines name the files: FROM the model, and for a model that reads pictures a second FROM, the
  projector. Its source (server/images.go) writes ADAPTER for each adapter and DRAFT for a draft model between the
  two. Then come the template and the system text, which may hold any line: the system text of a model made for
  these tests has one that begins with FROM. A path stands there without quotes. That Ollama makes no model with an
  adapter any more and still runs one an older Ollama made, so an ADAPTER line was read in its source and not seen.
- The same answer has the sampling values of the model ("parameters": a name, spaces, the value, one to a line, in an
  order that changes), what the model reads ("capabilities"), the engine Ollama runs it with ("details.runner":
  llamacpp for a file it hands to a llama-server of its own, ggml for one in the form of its own engine), and the
  length the model was trained with (model_info: "<architecture>.context_length").
- Ollama starts its own llama-server with the model file, the first projector (--mmproj), each adapter (--lora) and
  the context, and leaves the rest to llama-server (llm/llama_server.go). The command here does the same.
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import config
from .providers import ollama

NAME = "llama-server"
USUAL = "http://127.0.0.1:9931"         # where a llama-server answers that was started with neither --host nor --port
HERE = ("127.0.0.1", "localhost")       # the hosts under which a llama-server started without --host is reached
# Where a llama-server stands that is not on the PATH, under the home folder: the one Unsloth Studio brings, one it
# built from source, and a build in a copy of llama.cpp's source (its build puts the programs into build/bin).
PLACES = (".unsloth/llama.cpp/llama-server", ".unsloth/llama.cpp/build/bin/llama-server", "llama.cpp/build/bin/llama-server")
ASK = httpx.Timeout(5.0, connect=2.0)   # for what Ollama shows for a model
LIST_WAIT = 20.0                        # seconds a llama-server has to list its cards: a card that sleeps is woken first
CARDS = ("cuda", "rocm", "vulkan")      # the parts for a graphics card Ollama brings, the one to try first in front
# The sampling values a model has in Ollama, and the flag that gives each to llama-server, in the order of the command.
FLAGS = {"temperature": "--temp", "top_k": "--top-k", "top_p": "--top-p", "min_p": "--min-p", "repeat_penalty": "--repeat-penalty",
         "repeat_last_n": "--repeat-last-n", "presence_penalty": "--presence-penalty", "frequency_penalty": "--frequency-penalty",
         "seed": "--seed"}
OWN_ENGINE = ("Ollama keeps this model in the form of its own engine, not in the one of llama.cpp, so llama-server may not load its "
              "file; it says why in its terminal.")
NO_PROJECTOR = ("Ollama reads pictures with this model. Started this way it reads text only: Ollama keeps no separate projector "
                "file for it.")

_NUMBER = re.compile(r"-?\d+(\.\d+)?([eE][-+]?\d+)?")
_CARD = re.compile(r"\s+(\S+): (.+) \((\d+) MiB, (\d+) MiB free\)\s*")


@dataclass(frozen=True)
class Way:
    """One way to start a llama-server: the program, and what has to stand in its environment for it to find the
    libraries it needs (nothing for a build that finds them by itself). ollamas: it is the llama-server Ollama brings."""
    program: str
    env: tuple[tuple[str, str], ...] = ()
    ollamas: bool = False


def own_place() -> Path:
    """Where a llama-server stands that the owner put into HunterAI's own folder."""
    return config.home() / "llama.cpp" / NAME


def _runs(path: str | Path) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _alone() -> list[Way]:
    """The llama-server programs that stand alone, each once: the one in HunterAI's own folder, the one on the PATH,
    then the usual places under the home folder."""
    found = [str(own_place()), shutil.which(NAME), *(str(Path.home() / place) for place in PLACES)]
    out: list[Way] = []
    seen: set[str] = set()
    for path in found:
        if path and _runs(path) and os.path.realpath(path) not in seen:
            seen.add(os.path.realpath(path))
            out.append(Way(path))
    return out


def _card_order(folder: Path) -> tuple:
    """Sorts Ollama's folders with a part for a graphics card: CARDS in their order, of one kind the newest first."""
    kind = next((i for i, card in enumerate(CARDS) if folder.name.startswith(card)), len(CARDS))
    return kind, [-int(n) for n in re.findall(r"\d+", folder.name)], folder.name


def _of_ollama() -> list[Way]:
    """The llama-server Ollama brings: once with each of its parts for a graphics card, and last for the processor
    alone. It stands in lib/ollama beside the folder of the ollama program, and is given what Ollama gives it: its
    own folder and that of the part to find libraries in, and the part itself."""
    ollama_program = shutil.which("ollama")
    if not ollama_program:
        return []
    root = Path(os.path.realpath(ollama_program)).parent.parent / "lib" / "ollama"
    program = root / NAME
    if not _runs(program):
        return []
    try:
        folders = sorted((d for d in root.iterdir() if d.is_dir()), key=_card_order)
    except OSError:
        folders = []
    out = []
    for folder in folders:
        parts = sorted(folder.glob("libggml-*.so"))
        if parts:
            out.append(Way(str(program), (("LD_LIBRARY_PATH", f"{root}:{folder}"), ("GGML_BACKEND_PATH", str(parts[0]))), True))
    return [*out, Way(str(program), (("LD_LIBRARY_PATH", str(root)),), True)]


def ways() -> list[Way]:
    """Every way to start a llama-server on this machine: the programs that stand alone, then the one Ollama brings."""
    return [*_alone(), *_of_ollama()]


async def devices(way: Way) -> list[dict] | None:
    """The graphics cards a llama-server started this way can put a model on, as it lists them itself: the name it
    gives each, what the card is, and its memory in MiB. Empty for a build that has no part for a card, or finds no
    driver. None when it does not say: it cannot be run, it does not end in time, or it is a build from before it had
    that list."""
    try:
        asked = await asyncio.create_subprocess_exec(
            way.program, "--list-devices", env={**os.environ, **dict(way.env)}, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except OSError:
        return None
    try:
        said, _ = await asyncio.wait_for(asked.communicate(), LIST_WAIT)
    except TimeoutError:
        asked.kill()
        await asked.wait()
        return None
    _, listed, lines = said.decode("utf-8", "replace").partition("Available devices:")
    if not listed:
        return None
    cards = (_CARD.fullmatch(line) for line in lines.splitlines())
    return [{"name": c[1], "card": c[2], "total_mb": int(c[3]), "free_mb": int(c[4])} for c in cards if c]


async def choose(found: list[Way]) -> tuple[Way | None, list[dict] | None]:
    """(the way to start llama-server, the cards it sees): the first of `found` that sees a graphics card, each asked
    in turn until one does, and else the first of them. (None, None) when there is none."""
    first: tuple[Way | None, list[dict] | None] = (None, None)
    for way in found:
        cards = await devices(way)
        if cards:
            return way, cards
        if first[0] is None:
            first = (way, cards)
    return first


def start_at(saved: str | None) -> str | None:
    """The address a llama-server started on this machine answers at: the usual one while no address is saved, and the
    saved one when it is of that plain kind (http, this machine by its usual names, a port, nothing after it). None for
    a saved address of another kind: a server elsewhere, or one that a start with a port alone does not answer at."""
    if saved is None:
        return USUAL
    try:
        parts = urlsplit(saved)
        port = parts.port
    except ValueError:
        return None
    plain = parts.scheme == "http" and parts.hostname in HERE and port and parts.path in ("", "/")
    return f"http://{parts.hostname}:{port}" if plain else None


async def shown(name: str) -> dict | None:
    """What Ollama shows for one of its models (/api/show). None when it shows nothing for it: the model is gone since
    it was listed, or Ollama does not answer."""
    try:
        async with httpx.AsyncClient(timeout=ASK, trust_env=False) as client:
            r = await client.post(f"{ollama.host()}/api/show", json={"model": name})
        said = r.json() if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    return said if isinstance(said, dict) else None


def files(modelfile: str) -> tuple[list[str], list[str]]:
    """(the files after FROM, the files after ADAPTER) in the Modelfile text Ollama shows for a model, in its order.
    Only the lines before the template are read: they are the ones that name files."""
    models: list[str] = []
    adapters: list[str] = []
    for line in modelfile.splitlines():
        word, _, rest = line.strip().partition(" ")
        if not word or word.startswith("#"):
            continue
        if word == "FROM":
            models.append(rest.strip())
        elif word == "ADAPTER":
            adapters.append(rest.strip())
        elif word != "DRAFT":
            break
    return models, adapters


def unusable(path: str) -> str:
    """Why llama-server cannot be given this file, in words that follow the name of the model; "" when it can: the
    file is on this machine, may be read, and begins as a GGUF file does."""
    if not os.path.isabs(path):
        return f"Ollama names no file on this machine for it (FROM {path})"
    if not os.path.isfile(path):
        return (f"its file is not on this machine where Ollama names it ({path}): an Ollama in a container or on another "
                "machine keeps its files there")
    try:
        with open(path, "rb") as f:
            begins = f.read(4)
    except OSError:
        return f"its file may not be read by the user HunterAI runs as ({path})"
    return "" if begins == b"GGUF" else f"its file is not a GGUF file ({path})"


def alias(name: str) -> str:
    """The name llama-server gives a model of Ollama (--alias): the last part of Ollama's name, without the tag
    "latest", and with a "-" for whatever a command or a list of names divided by commas would trip over."""
    tail = name.rsplit("/", 1)[-1].removesuffix(":latest")
    return re.sub(r"[^A-Za-z0-9._-]+", "-", tail).strip("-") or "model"


def sampling(parameters: str | None) -> list[str]:
    """The flags that give llama-server the sampling values a model has in Ollama, from the "parameters" Ollama shows.
    What a chat sets itself still goes with each request and comes first. A value that is no number is left out."""
    has = {name: value.strip() for name, _, value in (line.partition(" ") for line in (parameters or "").splitlines())}
    return [word for name, flag in FLAGS.items() if _NUMBER.fullmatch(has.get(name, "")) for word in (flag, has[name])]


def command(way: Way, at: str, context: int, model: str, name: str | None = None, projector: str | None = None,
            adapters: list[str] | tuple[str, ...] = (), more: list[str] | tuple[str, ...] = ()) -> str:
    """The command that starts llama-server with a model so that it answers at `at`, written for a shell: what the way
    needs in the environment, then the program and its flags. The port is always named: a build from before October
    2026 listens on another one by itself."""
    words = [way.program, "-m", model, *(["--mmproj", projector] if projector else []), *(w for a in adapters for w in ("--lora", a)),
             "-c", str(context), *(["--alias", name] if name else []), "--port", str(urlsplit(at).port), *more]
    return " ".join([*(f"{name}={shlex.quote(value)}" for name, value in way.env), shlex.join(words)])


def offered(name: str, shown: dict, way: Way, at: str, context: int) -> dict:
    """One model of Ollama as the page offers it: the command that starts llama-server with its files, or why there is
    none. `shown` is what Ollama answers on /api/show for it; `context` the one HunterAI wants, which is lowered to the
    length the model was trained with. images: llama-server will read pictures with it. note: what the owner should
    know before he starts it."""
    def part(key: str, kind: type):
        return shown[key] if isinstance(shown.get(key), kind) else kind()
    froms, adapters = files(part("modelfile", str))
    info, reads = part("model_info", dict), part("capabilities", list)
    out = {"name": name, "alias": alias(name), "images": False, "command": None, "why_not": "", "note": ""}
    if not froms:
        return out | {"why_not": "Ollama names no file for it"}
    projector = froms[1] if len(froms) > 1 else None
    why = next((w for w in map(unusable, [froms[0], *froms[1:2], *adapters]) if w), "")
    if why:
        return out | {"why_not": why}
    trained = info.get(f"{info.get('general.architecture')}.context_length")
    if isinstance(trained, int) and not isinstance(trained, bool) and trained > 0:
        context = min(context, trained)
    notes = [OWN_ENGINE] if part("details", dict).get("runner") == "ggml" else []
    if "vision" in reads and not projector:
        notes.append(NO_PROJECTOR)
    parameters = shown.get("parameters")
    return out | {"images": projector is not None, "note": " ".join(notes),
                  "command": command(way, at, context, froms[0], out["alias"], projector, adapters,
                                     sampling(parameters if isinstance(parameters, str) else None))}
