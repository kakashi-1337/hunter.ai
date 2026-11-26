"""llamafind: where the llama-server program is on this machine, which files Ollama keeps for a model, and the command
that starts llama-server with them. What Ollama says here is what a real one said (tests/ollama_show_wire.py)."""
import copy
import json
import re
import shlex
import time
from pathlib import Path

import httpx
import pytest
import respx

from hunterai import config, engine, llamafind
from hunterai.providers import llamacpp
from tests import llamacpp_wire as llama_wire
from tests import ollama_show_wire as wire
from tests.test_api import client  # noqa: F401 (client is a fixture)
from tests.test_llamacpp_app import fixed_overhead

GGUF = b"GGUF\x03\x00\x00\x00" + bytes(16)
PROGRAM = "/home/dave/.unsloth/llama.cpp/llama-server"
WAY = llamafind.Way(PROGRAM)
AT = "http://127.0.0.1:9931"
TINY_FILE = "sha256-270cba1bd5109f42d03350f60406024560464db173c0e387d91f0426d3bd256d"
VISION_FILES = ["sha256-7566ae7219c93ea2ecc692a931ee122d30c55261d0e2c3347acb8b939d2e9abd",
                "sha256-93c2ba8c34574dd8f2dfda64931fc20943de2f941bfe03e6e9eca68951b80604"]
QWEN_FILES = ["sha256-bd258782e35f7f458f8aced1adc053e6e92e89bc735ba3be89d38a06121dc517",
              "sha256-56e4c6cfe73b0c82e3e82bc518d7591997e61d81f723fc41a586f4fa69ea2453"]
QWEN_NAME = "hf.co/unsloth/Qwen3.5-0.8B-GGUF:Q4_K_M"


def program_at(path: Path, mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(mode)
    return path


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    """A home folder and a PATH of the test's own, with no llama-server in either. The PATH is the folder bin beside it."""
    where = tmp_path / "userhome"
    where.mkdir()
    (tmp_path / "bin").mkdir()
    monkeypatch.setenv("HOME", str(where))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    return where


def blob(folder: Path, name: str, content: bytes = GGUF) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(content)
    return str(folder / name)


def shown(answer: dict, folder: Path, **more) -> dict:
    """What Ollama shows for a model, with its files in `folder`, where they are made as GGUF files."""
    said = copy.deepcopy(answer)
    for name in re.findall(r"^(?:FROM|ADAPTER) /BLOBS/(\S+)$", said["modelfile"], re.MULTILINE):
        blob(folder, name)
    said["modelfile"] = said["modelfile"].replace("/BLOBS", str(folder))
    said.update(more)
    return said


# ---- the ways to start it

def says(path: Path, text: str, code: int = 0) -> str:
    """A program in place of llama-server that writes `text` and ends. It needs nothing from the PATH."""
    lines = "".join(f"echo '{line}'\n" for line in text.splitlines())
    program_at(path).write_text(f"#!/bin/sh\n{lines}exit {code}\n")
    return str(path)


def ollama_at(root: Path, *parts: str) -> Path:
    """An Ollama as its Linux package lays it out under `root`: bin/ollama, and lib/ollama with a llama-server of its
    own and a folder for each of `parts` that holds a part for a graphics card. Returns lib/ollama. The llama-server
    lists a card when it was given the part of cuda_v13, as the one on the owner's laptop did."""
    program_at(root / "bin" / "ollama")
    lib = root / "lib" / "ollama"
    program_at(lib / "llama-server").write_text(
        "#!/bin/sh\necho 'Available devices:'\n"
        'case "$GGML_BACKEND_PATH" in\n'
        "  */cuda_v13/*) echo '  CUDA0: NVIDIA GeForce RTX 5080 Laptop GPU (15839 MiB, 6015 MiB free)';;\n"
        "  *) echo '  (none)';;\nesac\n")
    for part in parts:
        (lib / part).mkdir(parents=True)
        kind = re.match("[a-z]+", part)[0]
        (lib / part / f"libggml-{kind}.so").write_bytes(b"")
    return lib


def test_the_usual_places_are_those_of_unsloth_studio_and_of_a_build_in_the_home_folder():
    assert llamafind.PLACES == (".unsloth/llama.cpp/llama-server", ".unsloth/llama.cpp/build/bin/llama-server",
                                "llama.cpp/build/bin/llama-server")


def test_the_program_on_the_path_is_found_and_needs_nothing_more(home, tmp_path):
    here = program_at(tmp_path / "bin" / "llama-server")
    assert llamafind.ways() == [llamafind.Way(str(here))]
    assert llamafind.ways()[0].env == () and llamafind.ways()[0].ollamas is False


def test_the_program_unsloth_studio_brought_is_found_though_it_is_not_on_the_path(home):
    here = program_at(home / ".unsloth" / "llama.cpp" / "llama-server")
    assert llamafind.ways() == [llamafind.Way(str(here))]


def test_hunterais_own_folder_comes_first_then_the_path_then_the_usual_places(home, tmp_path):
    places = [program_at(home / place) for place in llamafind.PLACES]
    on_path = program_at(tmp_path / "bin" / "llama-server")
    own = program_at(config.home() / "llama.cpp" / "llama-server")
    assert [w.program for w in llamafind.ways()] == [str(own), str(on_path), *(str(place) for place in places)]


def test_a_program_that_is_found_twice_is_named_once(home, tmp_path):
    here = program_at(home / ".unsloth" / "llama.cpp" / "llama-server")
    (tmp_path / "bin" / "llama-server").symlink_to(here)
    assert [w.program for w in llamafind.ways()] == [str(tmp_path / "bin" / "llama-server")]


def test_a_file_that_may_not_be_run_and_a_folder_of_that_name_are_not_the_program(home):
    program_at(home / ".unsloth" / "llama.cpp" / "llama-server", 0o644)
    (home / "llama.cpp" / "build" / "bin" / "llama-server").mkdir(parents=True)
    program_at(config.home() / "llama.cpp" / "llama-server", 0o600)
    assert llamafind.ways() == []


def test_the_llama_server_ollama_brings_is_a_way_for_each_of_its_parts_for_a_card_and_last_for_the_processor(home, tmp_path, monkeypatch):
    """As on the owner's laptop on 26 November 2026: Ollama 0.40.18 with cuda_v12, cuda_v13 and vulkan beside its
    llama-server. The two variables are those Ollama gives it (llm/llama_server.go of Ollama)."""
    lib = ollama_at(tmp_path / "usr" / "local", "vulkan", "cuda_v12", "cuda_v13", "rocm_v7_2", "rocm_v6_4")
    (lib / "mlx_cuda_v13").mkdir()                      # a folder without a part for llama.cpp
    (lib / "cuda_v9").mkdir()
    (lib / "libggml-base.so").write_bytes(b"")          # a library beside the program is no folder with a part
    monkeypatch.setenv("PATH", str(tmp_path / "usr" / "local" / "bin"))
    program = str(lib / "llama-server")

    def part(folder: str, kind: str) -> llamafind.Way:
        return llamafind.Way(program, (("LD_LIBRARY_PATH", f"{lib}:{lib / folder}"), ("GGML_BACKEND_PATH", str(lib / folder / f"libggml-{kind}.so"))),
                             True)
    assert llamafind.ways() == [part("cuda_v13", "cuda"), part("cuda_v12", "cuda"), part("rocm_v7_2", "rocm"), part("rocm_v6_4", "rocm"),
                                part("vulkan", "vulkan"), llamafind.Way(program, (("LD_LIBRARY_PATH", str(lib)),), True)]


def test_a_folder_is_a_way_for_the_part_it_holds_and_not_for_the_libraries_beside_it(home, tmp_path, monkeypatch):
    """As on the owner's laptop: cuda_v13 holds the libraries of the card maker beside the part for llama.cpp, and
    they come before it in the order of names. A folder with libraries and without such a part is no way."""
    lib = ollama_at(tmp_path / "usr", "cuda_v13")
    (lib / "cuda_v13" / "libcublas.so.13").write_bytes(b"")
    (lib / "cuda_v13" / "libcudart.so.13").write_bytes(b"")
    (lib / "mlx_cuda_v13").mkdir()
    (lib / "mlx_cuda_v13" / "libmlx.so").write_bytes(b"")
    monkeypatch.setenv("PATH", str(tmp_path / "usr" / "bin"))
    assert [dict(w.env) for w in llamafind.ways()] == [
        {"LD_LIBRARY_PATH": f"{lib}:{lib / 'cuda_v13'}", "GGML_BACKEND_PATH": str(lib / "cuda_v13" / "libggml-cuda.so")},
        {"LD_LIBRARY_PATH": str(lib)}]


def test_a_part_of_a_kind_this_does_not_know_comes_after_those_it_knows(home, tmp_path, monkeypatch):
    lib = ollama_at(tmp_path / "usr", "accel_v2", "vulkan", "cuda_v13")
    monkeypatch.setenv("PATH", str(tmp_path / "usr" / "bin"))
    assert [dict(w.env).get("GGML_BACKEND_PATH") for w in llamafind.ways()] == [
        str(lib / "cuda_v13" / "libggml-cuda.so"), str(lib / "vulkan" / "libggml-vulkan.so"), str(lib / "accel_v2" / "libggml-accel.so"), None]


def test_ollamas_llama_server_comes_after_one_that_stands_alone(home, tmp_path, monkeypatch):
    lib = ollama_at(tmp_path / "usr", "cuda_v13")
    monkeypatch.setenv("PATH", str(tmp_path / "usr" / "bin"))
    alone = program_at(home / ".unsloth" / "llama.cpp" / "llama-server")
    found = llamafind.ways()
    assert [(w.program, w.ollamas) for w in found] == [(str(alone), False), (str(lib / "llama-server"), True), (str(lib / "llama-server"), True)]


def test_an_ollama_program_that_is_a_link_is_followed_to_where_it_stands(home, tmp_path, monkeypatch):
    lib = ollama_at(tmp_path / "opt" / "ollama", "cuda_v13")
    (tmp_path / "bin" / "ollama").symlink_to(tmp_path / "opt" / "ollama" / "bin" / "ollama")
    assert [w.program for w in llamafind.ways()] == [str(lib / "llama-server")] * 2


def test_an_ollama_without_a_llama_server_of_its_own_is_no_way(home, tmp_path):
    program_at(tmp_path / "bin" / "ollama")              # an Ollama from before it brought one: nothing in ../lib/ollama
    assert llamafind.ways() == []
    (tmp_path / "lib" / "ollama" / "llama-server").mkdir(parents=True)
    assert llamafind.ways() == []
    (tmp_path / "lib" / "ollama" / "llama-server").rmdir()
    program_at(tmp_path / "lib" / "ollama" / "llama-server", 0o644)
    assert llamafind.ways() == []


# ---- the graphics cards a way sees

def way(path: Path, text: str, code: int = 0) -> llamafind.Way:
    return llamafind.Way(says(path, text, code))


async def test_the_cards_a_llama_server_sees_are_read_from_its_own_list(tmp_path):
    """The lines are those llama.cpp writes for --list-devices (common/arg.cpp at 074f1f3): a name, what it is, and its
    memory. What a build logs before the list is not part of it."""
    text = ("ggml_vulkan: Found 1 Vulkan devices:\n  ggml_vulkan: 0 = NVIDIA GeForce RTX 5080 Laptop GPU (16303 MiB, 9 MiB free)\n"
            "Available devices:\n  CUDA0: NVIDIA GeForce RTX 5080 Laptop GPU (15839 MiB, 6015 MiB free)\n"
            "  Vulkan1: Intel(R) Graphics (ARL) (15885 MiB, 15885 MiB free)\n  Odd9: a card (1 MiB, 1 MiB free) and more\n"
            "CUDA7: not set in (2 MiB, 2 MiB free)")
    assert await llamafind.devices(way(tmp_path / "llama-server", text)) == [
        {"name": "CUDA0", "card": "NVIDIA GeForce RTX 5080 Laptop GPU", "total_mb": 15839, "free_mb": 6015},
        {"name": "Vulkan1", "card": "Intel(R) Graphics (ARL)", "total_mb": 15885, "free_mb": 15885}]


async def test_a_build_without_a_part_for_a_card_sees_none(tmp_path):
    """What a real llama-server built for the processor alone wrote, and what the one Unsloth Studio brought to the
    owner's laptop wrote on 26 November 2026."""
    assert await llamafind.devices(way(tmp_path / "llama-server", "Available devices:\n  (none)")) == []


async def test_a_build_that_has_no_such_list_does_not_say(tmp_path):
    assert await llamafind.devices(way(tmp_path / "llama-server", "error: invalid argument: --list-devices", 1)) is None


async def test_a_program_that_cannot_be_run_does_not_say(tmp_path):
    assert await llamafind.devices(llamafind.Way(str(tmp_path / "not-there"))) is None
    assert await llamafind.devices(llamafind.Way(str(program_at(tmp_path / "llama-server", 0o644)))) is None


async def test_it_is_asked_with_that_one_flag_and_given_what_its_way_needs(tmp_path, monkeypatch):
    asked = tmp_path / "asked"
    program_at(tmp_path / "llama-server").write_text(
        f'#!/bin/sh\necho "$#:$*|$LD_LIBRARY_PATH|$GGML_BACKEND_PATH|$KEPT" > {asked}\necho "Available devices:"\necho "  (none)"\n')
    monkeypatch.setenv("KEPT", "the rest of the environment stays")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/the/owners/own")
    monkeypatch.delenv("GGML_BACKEND_PATH", raising=False)
    assert await llamafind.devices(llamafind.Way(str(tmp_path / "llama-server"))) == []
    assert asked.read_text() == "1:--list-devices|/the/owners/own||the rest of the environment stays\n"
    needs = (("LD_LIBRARY_PATH", "/lib/ollama:/lib/ollama/cuda_v13"), ("GGML_BACKEND_PATH", "/lib/ollama/cuda_v13/libggml-cuda.so"))
    assert await llamafind.devices(llamafind.Way(str(tmp_path / "llama-server"), needs, True)) == []
    assert asked.read_text() == ("1:--list-devices|/lib/ollama:/lib/ollama/cuda_v13|/lib/ollama/cuda_v13/libggml-cuda.so|"
                                 "the rest of the environment stays\n")


async def test_a_program_that_does_not_end_is_stopped_and_does_not_say(tmp_path, monkeypatch):
    monkeypatch.setattr(llamafind, "LIST_WAIT", 0.3)
    pid = tmp_path / "pid"
    program_at(tmp_path / "llama-server").write_text(f"#!/bin/sh\necho $$ > {pid}\necho 'Available devices:'\nexec /bin/sleep 30\n")
    began = time.monotonic()
    assert await llamafind.devices(llamafind.Way(str(tmp_path / "llama-server"))) is None
    assert time.monotonic() - began < 5
    assert not Path(f"/proc/{pid.read_text().strip()}").exists()        # it was stopped, and is not left behind


async def test_of_several_ways_the_first_that_sees_a_card_is_taken_and_the_ones_after_it_are_not_asked(tmp_path):
    card = "Available devices:\n  CUDA0: a card (16303 MiB, 15800 MiB free)"
    none = way(tmp_path / "a" / "llama-server", "Available devices:\n  (none)")
    old = way(tmp_path / "b" / "llama-server", "error: invalid argument: --list-devices", 1)
    sees = way(tmp_path / "c" / "llama-server", card)
    ran = tmp_path / "ran"
    after = llamafind.Way(str(program_at(tmp_path / "d" / "llama-server")))
    Path(after.program).write_text(f"#!/bin/sh\n: > {ran}\necho 'Available devices:'\necho '  CUDA1: another (1 MiB, 1 MiB free)'\n")
    assert await llamafind.choose([none, old, sees, after]) == (sees, [{"name": "CUDA0", "card": "a card", "total_mb": 16303, "free_mb": 15800}])
    assert not ran.exists()


async def test_where_no_way_sees_a_card_the_first_is_taken_with_what_it_said(tmp_path):
    none = way(tmp_path / "a" / "llama-server", "Available devices:\n  (none)")
    old = way(tmp_path / "b" / "llama-server", "error: invalid argument: --list-devices", 1)
    assert await llamafind.choose([none, old]) == (none, [])
    assert await llamafind.choose([old, none]) == (old, None)
    assert await llamafind.choose([]) == (None, None)


async def test_the_way_through_ollamas_part_for_the_card_is_taken_where_the_program_that_stands_alone_sees_none(home, tmp_path, monkeypatch):
    """The owner's laptop: the llama-server Unsloth Studio brought sees no card, the one of Ollama does with cuda_v13."""
    lib = ollama_at(tmp_path / "usr" / "local", "vulkan", "cuda_v12", "cuda_v13")
    monkeypatch.setenv("PATH", str(tmp_path / "usr" / "local" / "bin"))
    says(home / ".unsloth" / "llama.cpp" / "llama-server", "Available devices:\n  (none)")
    taken, cards = await llamafind.choose(llamafind.ways())
    assert taken == llamafind.Way(str(lib / "llama-server"), (("LD_LIBRARY_PATH", f"{lib}:{lib}/cuda_v13"),
                                                             ("GGML_BACKEND_PATH", f"{lib}/cuda_v13/libggml-cuda.so")), True)
    assert cards == [{"name": "CUDA0", "card": "NVIDIA GeForce RTX 5080 Laptop GPU", "total_mb": 15839, "free_mb": 6015}]


# ---- where it is started

def test_without_a_saved_address_llama_server_is_started_at_its_usual_port():
    assert llamafind.start_at(None) == "http://127.0.0.1:9931"


@pytest.mark.parametrize("saved", ["http://127.0.0.1:9931", "http://127.0.0.1:8080", "http://localhost:18084"])
def test_a_saved_address_on_this_machine_is_where_it_is_started(saved):
    assert llamafind.start_at(saved) == saved


@pytest.mark.parametrize("saved", ["http://192.168.1.20:9931", "http://box.lan:9931", "https://127.0.0.1:9931", "http://[::1]:9931",
                                   "http://127.0.0.1:9931/llama", "http://127.0.0.1", "http://127.0.0.1:99999", "nonsense"])
def test_an_address_elsewhere_or_one_a_plain_start_does_not_answer_at_is_not_started_here(saved):
    assert llamafind.start_at(saved) is None


# ---- the files of a model, from what Ollama shows

def test_the_model_file_is_read_from_what_ollama_shows_and_not_from_its_system_text():
    """The system text of this model has a line that begins with FROM and one that begins with ADAPTER."""
    assert llamafind.files(wire.TINY["modelfile"]) == ([f"/BLOBS/{TINY_FILE}"], [])


def test_a_projector_is_the_second_file():
    assert llamafind.files(wire.VISION["modelfile"]) == ([f"/BLOBS/{name}" for name in VISION_FILES], [])
    assert llamafind.files(wire.QWEN["modelfile"]) == ([f"/BLOBS/{name}" for name in QWEN_FILES], [])


def test_adapters_and_a_draft_model_are_read_as_ollama_writes_them():
    """Ollama of November 2026 makes no model with an adapter any more, and still runs one that an older Ollama made.
    Its lines are those its source writes (server/images.go): the model, each adapter, a draft model, each projector."""
    text = ('# Modelfile generated by "ollama show"\n# To build a new Modelfile based on this, replace FROM with:\n'
            "# FROM pakkun:latest\n\nFROM /m/blobs/sha256-aa\nADAPTER /m/blobs/sha256-bb\nADAPTER /m/blobs/sha256-cc\n"
            "DRAFT /m/blobs/sha256-dd\nFROM /m/blobs/sha256-ee\nTEMPLATE {{ .Prompt }}\nPARAMETER stop FROM\nFROM /m/blobs/sha256-ff\n")
    assert llamafind.files(text) == (["/m/blobs/sha256-aa", "/m/blobs/sha256-ee"], ["/m/blobs/sha256-bb", "/m/blobs/sha256-cc"])


def test_a_folder_with_a_space_in_its_name_is_kept_whole():
    assert llamafind.files("FROM /home/dave/My Models/blobs/sha256-aa\nTEMPLATE x\n") == (["/home/dave/My Models/blobs/sha256-aa"], [])


def test_a_text_that_names_no_file_before_its_template_gives_none():
    assert llamafind.files("") == ([], [])
    assert llamafind.files("# FROM tiny:latest\n\nTEMPLATE {{ .Prompt }}\nFROM /late\nADAPTER /late\n") == ([], [])


# ---- a file llama-server can read

def test_a_gguf_file_that_can_be_read_is_usable(tmp_path):
    assert llamafind.unusable(blob(tmp_path, "sha256-aa")) == ""


def test_a_name_in_place_of_a_file_is_said_as_that():
    assert llamafind.unusable("gpt-oss:120b-cloud") == "Ollama names no file on this machine for it (FROM gpt-oss:120b-cloud)"


def test_a_file_that_is_not_where_ollama_names_it_is_said_with_its_place(tmp_path):
    gone = str(tmp_path / "blobs" / "sha256-aa")
    why = (f"its file is not on this machine where Ollama names it ({gone}): an Ollama in a container or on another machine "
           "keeps its files there")
    assert llamafind.unusable(gone) == why
    Path(gone).mkdir(parents=True)          # a folder of that name is no file either
    assert llamafind.unusable(gone) == why


@pytest.mark.parametrize("content", [b"PK\x03\x04 a zip", b"GG", b"", b"gguf\x03\x00\x00\x00"])
def test_a_file_that_is_no_gguf_file_is_said_as_that(tmp_path, content):
    other = blob(tmp_path, "sha256-aa", content)
    assert llamafind.unusable(other) == f"its file is not a GGUF file ({other})"


def test_a_file_that_may_not_be_read_is_said_as_that(tmp_path, monkeypatch):
    kept = blob(tmp_path, "sha256-aa")

    def refuse(path, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(path))
    monkeypatch.setattr(llamafind, "open", refuse, raising=False)
    assert llamafind.unusable(kept) == f"its file may not be read by the user HunterAI runs as ({kept})"


# ---- the name llama-server gives the model

@pytest.mark.parametrize("name, short", [
    ("hf.co/huihui-ai/Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-GGUF:Q6_K", "Huihui-Qwythos-9B-Claude-Mythos-5-1M-abliterated-GGUF-Q6_K"),
    ("qwen3:8b", "qwen3-8b"), ("mythos:latest", "mythos"), ("mythos", "mythos"),
    ("maternion/mimo-v2.6-heretic:9b-thinking-q6_K", "mimo-v2.6-heretic-9b-thinking-q6_K"),
    ("a b,c:1", "a-b-c-1"), ("my model (v2):latest", "my-model-v2"), ("-x-:latest", "x"), (":latest", "model"), ("x/", "model")])
def test_the_name_is_the_last_part_of_ollamas_without_what_a_command_or_a_list_of_names_trips_over(name, short):
    assert llamafind.alias(name) == short


# ---- the sampling values a model has in Ollama

def test_the_sampling_values_a_model_has_in_ollama_become_flags_in_one_order():
    assert llamafind.sampling(wire.TINY["parameters"]) == [
        "--temp", "0.6", "--top-k", "20", "--top-p", "0.95", "--min-p", "0.02", "--repeat-penalty", "1.1", "--repeat-last-n", "128",
        "--presence-penalty", "1.5", "--frequency-penalty", "0.25", "--seed", "42"]


def test_the_order_ollama_lists_them_in_changes_nothing():
    lines = wire.TINY["parameters"].splitlines()
    assert llamafind.sampling("\n".join(reversed(lines))) == llamafind.sampling(wire.TINY["parameters"])


def test_a_model_without_sampling_values_gets_no_flag():
    assert llamafind.sampling(None) == [] and llamafind.sampling("") == []
    assert llamafind.sampling(wire.VISION["parameters"]) == []          # a stop word is all it has


def test_a_value_that_is_no_number_is_left_out():
    text = ('temperature                    "hot"\ntop_k                          nan\nseed                           -1\n'
            "min_p                          1e-05\ntop_p                          \nrepeat_penalty                 1.1 2\n"
            "presence_penalty               .5\nfrequency_penalty              0.5\nrepeat_last_n")
    assert llamafind.sampling(text) == ["--min-p", "1e-05", "--frequency-penalty", "0.5", "--seed", "-1"]


# ---- the command

def test_the_command_names_the_program_the_files_the_context_the_name_and_the_port():
    said = llamafind.command(WAY, AT, 32768, "/m/blobs/sha256-aa", "qwythos-9b", "/m/blobs/sha256-bb",
                             ["/m/blobs/sha256-cc", "/m/blobs/sha256-dd"], ["--temp", "0.6"])
    assert said == (f"{PROGRAM} -m /m/blobs/sha256-aa --mmproj /m/blobs/sha256-bb --lora /m/blobs/sha256-cc --lora /m/blobs/sha256-dd "
                    "-c 32768 --alias qwythos-9b --port 9931 --temp 0.6")


def test_the_command_for_a_file_alone_has_neither_a_projector_nor_a_name():
    assert llamafind.command(llamafind.Way("/usr/bin/llama-server"), "http://localhost:8080", 16384, "your-model.gguf") == (
        "/usr/bin/llama-server -m your-model.gguf -c 16384 --port 8080")


def test_what_a_way_needs_in_its_environment_stands_before_the_program():
    """The command the owner's laptop was given by hand on 26 November 2026, which listed his card."""
    lib = "/usr/local/lib/ollama"
    ollamas = llamafind.Way(f"{lib}/llama-server", (("LD_LIBRARY_PATH", f"{lib}:{lib}/cuda_v13"), ("GGML_BACKEND_PATH", f"{lib}/cuda_v13/libggml-cuda.so")),
                            True)
    assert llamafind.command(ollamas, AT, 32768, "/m/a.gguf", "a") == (
        f"LD_LIBRARY_PATH={lib}:{lib}/cuda_v13 GGML_BACKEND_PATH={lib}/cuda_v13/libggml-cuda.so {lib}/llama-server -m /m/a.gguf "
        "-c 32768 --alias a --port 9931")
    spaced = llamafind.Way("/opt/o/llama-server", (("LD_LIBRARY_PATH", "/opt/my libs:/opt/o"),))
    assert llamafind.command(spaced, AT, 4096, "a.gguf") == "LD_LIBRARY_PATH='/opt/my libs:/opt/o' /opt/o/llama-server -m a.gguf -c 4096 --port 9931"


def test_a_path_with_a_space_or_a_quote_is_written_for_the_shell():
    said = llamafind.command(llamafind.Way("/opt/my tools/llama-server"), AT, 4096, "/home/o'neil/My Models/a.gguf", "a")
    assert shlex.split(said) == ["/opt/my tools/llama-server", "-m", "/home/o'neil/My Models/a.gguf", "-c", "4096", "--alias", "a",
                                 "--port", "9931"]
    assert said.startswith("'/opt/my tools/llama-server' -m ")


# ---- one model of Ollama, as it is offered

def test_a_model_of_ollama_is_offered_with_the_command_that_starts_it(tmp_path):
    assert llamafind.offered(QWEN_NAME, shown(wire.QWEN, tmp_path), WAY, AT, 32768) == {
        "name": QWEN_NAME, "alias": "Qwen3.5-0.8B-GGUF-Q4_K_M", "images": True, "why_not": "", "note": "",
        "command": f"{PROGRAM} -m {tmp_path}/{QWEN_FILES[0]} --mmproj {tmp_path}/{QWEN_FILES[1]} -c 32768 "
                   "--alias Qwen3.5-0.8B-GGUF-Q4_K_M --port 9931"}


def test_the_context_is_never_more_than_the_model_takes_and_its_sampling_values_go_along(tmp_path):
    """The story model was trained with 2,048 tokens. Its num_ctx in Ollama is not taken: HunterAI names the context."""
    got = llamafind.offered("tiny:latest", shown(wire.TINY, tmp_path), WAY, "http://127.0.0.1:18084", 32768)
    assert got["command"] == (f"{PROGRAM} -m {tmp_path}/{TINY_FILE} -c 2048 --alias tiny --port 18084 --temp 0.6 --top-k 20 "
                              "--top-p 0.95 --min-p 0.02 --repeat-penalty 1.1 --repeat-last-n 128 --presence-penalty 1.5 "
                              "--frequency-penalty 0.25 --seed 42")
    assert got["images"] is False and got["why_not"] == "" and got["note"] == ""


def test_a_context_the_model_takes_is_left_as_it_is(tmp_path):
    got = llamafind.offered("tinyvision:latest", shown(wire.VISION, tmp_path), WAY, AT, 131072)
    assert " -c 131072 " in got["command"] and got["images"] is True


def test_a_model_whose_file_is_not_here_is_offered_without_a_command_and_with_the_reason(tmp_path):
    said = copy.deepcopy(wire.QWEN)
    said["modelfile"] = said["modelfile"].replace("/BLOBS", str(tmp_path / "gone"))
    got = llamafind.offered(QWEN_NAME, said, WAY, AT, 32768)
    assert got["command"] is None and got["images"] is False
    assert got["why_not"] == (f"its file is not on this machine where Ollama names it ({tmp_path}/gone/{QWEN_FILES[0]}): an Ollama in "
                              "a container or on another machine keeps its files there")


def test_a_projector_that_cannot_be_used_stands_in_the_way_too(tmp_path):
    said = shown(wire.QWEN, tmp_path)
    (tmp_path / QWEN_FILES[1]).write_bytes(b"not a model")
    got = llamafind.offered(QWEN_NAME, said, WAY, AT, 32768)
    assert got["command"] is None and got["why_not"] == f"its file is not a GGUF file ({tmp_path}/{QWEN_FILES[1]})"


def test_an_adapter_goes_along_and_one_that_cannot_be_used_stands_in_the_way(tmp_path):
    said = shown(wire.TINY, tmp_path, parameters=None)
    kept, gone = blob(tmp_path, "sha256-bb"), str(tmp_path / "sha256-cc")
    said["modelfile"] = said["modelfile"].replace("\nTEMPLATE ", f"\nADAPTER {kept}\nTEMPLATE ", 1)
    assert llamafind.offered("pakkun", said, WAY, AT, 2048)["command"] == (
        f"{PROGRAM} -m {tmp_path}/{TINY_FILE} --lora {kept} -c 2048 --alias pakkun --port 9931")
    said["modelfile"] = said["modelfile"].replace("\nTEMPLATE ", f"\nADAPTER {gone}\nTEMPLATE ", 1)
    got = llamafind.offered("pakkun", said, WAY, AT, 2048)
    assert got["command"] is None and got["why_not"].startswith(f"its file is not on this machine where Ollama names it ({gone})")


def test_only_the_first_projector_goes_along_as_in_ollama(tmp_path):
    said = shown(wire.VISION, tmp_path)
    said["modelfile"] = said["modelfile"].replace("\nTEMPLATE ", f"\nFROM {tmp_path}/sha256-not-there\nTEMPLATE ", 1)
    got = llamafind.offered("tinyvision", said, WAY, AT, 4096)
    assert got["command"] == (f"{PROGRAM} -m {tmp_path}/{VISION_FILES[0]} --mmproj {tmp_path}/{VISION_FILES[1]} -c 4096 "
                              "--alias tinyvision --port 9931")


@pytest.mark.parametrize("said", [{}, {"modelfile": None}, {"modelfile": "TEMPLATE {{ .Prompt }}\n"}, {"modelfile": 7},
                                  {"modelfile": "", "details": [], "capabilities": "vision", "model_info": "x", "parameters": 3}])
def test_a_model_ollama_names_no_file_for_is_offered_without_a_command(said):
    assert llamafind.offered("odd:1b", said, WAY, AT, 4096) == {
        "name": "odd:1b", "alias": "odd-1b", "images": False, "command": None, "why_not": "Ollama names no file for it", "note": ""}


def test_a_name_where_a_file_should_be_is_said_as_that(tmp_path):
    got = llamafind.offered("big:cloud", {"modelfile": "FROM big:cloud\nTEMPLATE x\n"}, WAY, AT, 4096)
    assert got["command"] is None and got["why_not"] == "Ollama names no file on this machine for it (FROM big:cloud)"


def test_a_model_ollama_keeps_for_its_own_engine_carries_a_note(tmp_path):
    said = shown(wire.TINY, tmp_path)
    said["details"]["runner"] = "ggml"
    got = llamafind.offered("tiny", said, WAY, AT, 2048)
    assert got["command"] and got["note"] == ("Ollama keeps this model in the form of its own engine, not in the one of llama.cpp, so "
                                              "llama-server may not load its file; it says why in its terminal.")


def test_a_model_that_reads_pictures_in_ollama_without_a_projector_file_says_so(tmp_path):
    said = shown(wire.TINY, tmp_path, capabilities=["completion", "vision"])
    got = llamafind.offered("tiny", said, WAY, AT, 2048)
    assert got["images"] is False and got["command"]
    assert got["note"] == ("Ollama reads pictures with this model. Started this way it reads text only: Ollama keeps no separate "
                           "projector file for it.")
    said["details"]["runner"] = "ggml"
    both = llamafind.offered("tiny", said, WAY, AT, 2048)["note"]
    assert both.startswith("Ollama keeps this model in the form of its own engine") and both.endswith("projector file for it.")
    assert "terminal. Ollama reads pictures" in both


def test_a_context_length_that_is_no_number_does_not_hold_the_context_down(tmp_path):
    said = shown(wire.TINY, tmp_path, parameters=None)
    said["model_info"]["llama.context_length"] = "2048"
    assert " -c 8192 " in llamafind.offered("tiny", said, WAY, AT, 8192)["command"]
    for odd in (True, 0, -5, 2048.0, None):
        said["model_info"]["llama.context_length"] = odd
        assert " -c 8192 " in llamafind.offered("tiny", said, WAY, AT, 8192)["command"], odd
    said["model_info"] = {"general.architecture": "llama", "other.context_length": 512}
    assert " -c 8192 " in llamafind.offered("tiny", said, WAY, AT, 8192)["command"]


# ---- another address than the saved one

@respx.mock
async def test_another_address_is_looked_at_once_and_what_is_seen_there_is_not_kept():
    server = llamacpp.make()
    asked = respx.get(USUAL + "/v1/models").mock(return_value=httpx.Response(200, text=llama_wire.MODELS_QWEN))
    seen = await server.look_at(USUAL)
    assert seen.state == "ok" and seen.at == USUAL and [m.id for m in seen.models] == ["qwen3.5-0.8b"]
    assert (await server.served()).state == "off" and await server.models() == [] and server.note() is None
    assert (await server.look_at(USUAL)).state == "ok" and asked.call_count == 2


# ---- what the page is told

OLLAMA = "http://127.0.0.1:11434"
USUAL = "http://127.0.0.1:9931"
FIND = "/api/system/llamacpp/find"
CARD = {"name": "CUDA0", "card": "NVIDIA GeForce RTX 5080 Laptop GPU", "total_mb": 16303, "free_mb": 15800}
SEES_A_CARD = "Available devices:\n  CUDA0: NVIDIA GeForce RTX 5080 Laptop GPU (16303 MiB, 15800 MiB free)"
SEES_NONE = "Available devices:\n  (none)"
TINY_FLAGS = ("--temp 0.6 --top-k 20 --top-p 0.95 --min-p 0.02 --repeat-penalty 1.1 --repeat-last-n 128 --presence-penalty 1.5 "
              "--frequency-penalty 0.25 --seed 42")


def ollama_has(folder: Path, models: dict[str, dict | None], held: tuple[str, ...] = (), mock=respx):
    """An Ollama that lists `models` and holds `held` in memory. A model stands with what Ollama shows for it (its files
    are made in `folder`), or with None for one Ollama lists and then does not have. Returns the routes for its list
    and for what it shows."""
    listed = mock.get(OLLAMA + "/api/tags").mock(return_value=httpx.Response(200, json={"models": [{"name": n, "model": n} for n in models]}))

    def show(request):
        said = models.get(json.loads(request.content)["model"])
        return httpx.Response(200, json=shown(said, folder)) if said else httpx.Response(404, text=wire.NOT_FOUND)
    showed = mock.post(OLLAMA + "/api/show").mock(side_effect=show)
    mock.get(OLLAMA + "/api/ps").mock(return_value=httpx.Response(200, json={"models": [{"name": n, "model": n, "size": 2, "size_vram": 2}
                                                                                         for n in held]}))
    return listed, showed


def nothing_at(address: str = USUAL, mock=respx):
    return mock.get(address + "/v1/models").mock(side_effect=httpx.ConnectError("refused"))


def unsloth(home: Path, text: str = SEES_A_CARD) -> str:
    return says(home / ".unsloth" / "llama.cpp" / "llama-server", text)


@respx.mock
async def test_the_page_gets_the_program_its_card_and_a_command_for_each_chat_model_of_ollama(client, home, tmp_path, monkeypatch):
    fixed_overhead(monkeypatch, 20537)          # what the rules and tools take on the owner's machine
    program, blobs = unsloth(home), tmp_path / "blobs"
    nothing_at()
    ollama_has(blobs, {QWEN_NAME: wire.QWEN, "bge-m3:latest": wire.TINY, "tiny:latest": wire.TINY})
    assert (await client.get(FIND)).json() == {
        "address": USUAL, "program": program, "from_ollama": False, "devices": [CARD], "answers": None, "context": 32768,
        "ollama": True, "held": [],
        "plain": f"{program} -m your-model.gguf -c 32768 --port 9931",
        "models": [{"name": QWEN_NAME, "alias": "Qwen3.5-0.8B-GGUF-Q4_K_M", "images": True, "why_not": "", "note": "",
                    "command": f"{program} -m {blobs}/{QWEN_FILES[0]} --mmproj {blobs}/{QWEN_FILES[1]} -c 32768 "
                               "--alias Qwen3.5-0.8B-GGUF-Q4_K_M --port 9931"},
                   {"name": "tiny:latest", "alias": "tiny", "images": False, "why_not": "", "note": "",
                    "command": f"{program} -m {blobs}/{TINY_FILE} -c 2048 --alias tiny --port 9931 {TINY_FLAGS}"}]}


@respx.mock
async def test_the_local_model_in_use_stands_first(client, home, tmp_path, monkeypatch):
    fixed_overhead(monkeypatch, 9000)
    unsloth(home)
    nothing_at()
    ollama_has(tmp_path, {QWEN_NAME: wire.QWEN, "tinyvision:latest": wire.VISION, "tiny:latest": wire.TINY})
    assert [m["name"] for m in (await client.get(FIND)).json()["models"]] == [QWEN_NAME, "tinyvision:latest", "tiny:latest"]
    assert (await client.put("/api/settings", json={"fallback_model": "tiny"})).status_code == 200
    assert [m["name"] for m in (await client.get(FIND)).json()["models"]] == ["tiny:latest", QWEN_NAME, "tinyvision:latest"]


@respx.mock
async def test_while_no_local_model_is_chosen_the_one_hunterai_uses_then_stands_first(client, home, tmp_path, monkeypatch):
    fixed_overhead(monkeypatch, 9000)
    unsloth(home)
    nothing_at()
    ollama_has(tmp_path, {"aaa:latest": wire.TINY, engine.DEFAULT_FALLBACK_MODEL: wire.TINY})
    assert [m["name"] for m in (await client.get(FIND)).json()["models"]] == [engine.DEFAULT_FALLBACK_MODEL, "aaa:latest"]


@respx.mock
async def test_an_ollama_whose_list_of_models_does_not_come_is_one_that_does_not_answer(client, home, monkeypatch):
    fixed_overhead(monkeypatch, 9000)
    unsloth(home)
    nothing_at()
    respx.get(OLLAMA + "/api/tags").mock(return_value=httpx.Response(500, text="no"))
    respx.get(OLLAMA + "/api/ps").mock(return_value=httpx.Response(200, json={"models": [{"name": "tiny:latest", "model": "tiny:latest"}]}))
    got = (await client.get(FIND)).json()
    assert got["ollama"] is False and got["models"] == [] and got["held"] == []


@respx.mock
@pytest.mark.parametrize("answer, said", [
    (httpx.Response(200, text=llama_wire.MODELS_QWEN), {"state": "ok", "serves": ["qwen3.5-0.8b"], "said": ""}),
    (httpx.Response(200, text='{"object":"list","data":[]}'),
     {"state": "nomodel", "serves": [], "said": "it lists no model: a llama-server started without one serves the GGUF files of the "
                                                "folder it was given (--models-dir)"}),
    (httpx.Response(503, text=llama_wire.LOADING), {"state": "loading", "serves": [], "said": ""}),
    (httpx.Response(401, text=llama_wire.NO_KEY), {"state": "key", "serves": [], "said": ""}),
    (httpx.Response(404, text="<html>not here</html>"),
     {"state": "odd", "serves": [], "said": f"what answers at {USUAL} is not llama-server (it answers HTTP 404)"})])
async def test_what_answers_at_the_usual_address_is_said_while_no_address_is_saved(client, home, monkeypatch, answer, said):
    fixed_overhead(monkeypatch, 9000)
    respx.get(USUAL + "/v1/models").mock(return_value=answer)
    got = (await client.get(FIND)).json()
    assert got["answers"] == said and got["address"] == USUAL and got["program"] is None


async def test_with_a_saved_address_on_this_machine_the_command_has_its_port_and_no_address_is_asked(client, home, tmp_path, monkeypatch):
    fixed_overhead(monkeypatch, 9000)
    monkeypatch.setenv(llamacpp.URL_ENV, "http://localhost:18084")
    program = unsloth(home)
    with respx.mock(assert_all_called=False) as mock:
        usual = mock.get(USUAL + "/v1/models").mock(return_value=httpx.Response(200, text=llama_wire.MODELS_QWEN))
        saved = mock.get("http://localhost:18084/v1/models").mock(return_value=httpx.Response(200, text=llama_wire.MODELS_QWEN))
        ollama_has(tmp_path, {"tiny:latest": wire.TINY}, mock=mock)
        got = (await client.get(FIND)).json()
    assert got["address"] == "http://localhost:18084" and got["answers"] is None
    assert got["plain"] == f"{program} -m your-model.gguf -c 20480 --port 18084"
    assert got["models"][0]["command"] == f"{program} -m {tmp_path}/{TINY_FILE} -c 2048 --alias tiny --port 18084 {TINY_FLAGS}"
    assert usual.call_count == 0 and saved.call_count == 0


async def test_a_saved_address_on_another_machine_gets_nothing_and_nothing_is_asked_or_run(client, home, tmp_path, monkeypatch):
    fixed_overhead(monkeypatch, 9000)
    monkeypatch.setenv(llamacpp.URL_ENV, "http://192.168.1.20:9931")
    ran = tmp_path / "ran"
    program_at(home / ".unsloth" / "llama.cpp" / "llama-server").write_text(f"#!/bin/sh\n: > {ran}\necho 'Available devices:'\n")
    with respx.mock(assert_all_called=False) as mock:
        usual = nothing_at(mock=mock)
        listed, showed = ollama_has(tmp_path, {"tiny:latest": wire.TINY}, mock=mock)
        got = (await client.get(FIND)).json()
    assert got == {"address": None, "program": None, "from_ollama": False, "devices": None, "answers": None, "context": 20480,
                   "ollama": False, "held": [], "plain": None, "models": []}
    assert usual.call_count == 0 and listed.call_count == 0 and showed.call_count == 0 and not ran.exists()


async def test_without_a_program_there_is_no_command_and_ollama_is_not_asked(client, home, tmp_path, monkeypatch):
    fixed_overhead(monkeypatch, 9000)
    with respx.mock(assert_all_called=False) as mock:
        nothing_at(mock=mock)
        listed, showed = ollama_has(tmp_path, {"tiny:latest": wire.TINY}, held=("tiny:latest",), mock=mock)
        got = (await client.get(FIND)).json()
    assert got == {"address": USUAL, "program": None, "from_ollama": False, "devices": None, "answers": None, "context": 20480,
                   "ollama": False, "held": [], "plain": None, "models": []}
    assert listed.call_count == 0 and showed.call_count == 0


@respx.mock
async def test_an_ollama_that_does_not_answer_leaves_the_command_for_a_file_of_the_owners_own(client, home, monkeypatch):
    fixed_overhead(monkeypatch, 9000)
    program = unsloth(home, SEES_NONE)
    nothing_at()
    respx.get(OLLAMA + "/api/tags").mock(side_effect=httpx.ConnectError("refused"))
    respx.get(OLLAMA + "/api/ps").mock(side_effect=httpx.ConnectError("refused"))
    got = (await client.get(FIND)).json()
    assert got["program"] == program and got["devices"] == [] and got["ollama"] is False and got["models"] == [] and got["held"] == []
    assert got["plain"] == f"{program} -m your-model.gguf -c 20480 --port 9931"


@respx.mock
async def test_the_chat_models_ollama_holds_in_memory_are_named(client, home, tmp_path, monkeypatch):
    fixed_overhead(monkeypatch, 9000)
    unsloth(home)
    nothing_at()
    ollama_has(tmp_path, {"tiny:latest": wire.TINY}, held=("bge-m3:latest", QWEN_NAME, "tiny:latest"))
    assert (await client.get(FIND)).json()["held"] == [QWEN_NAME, "tiny:latest"]


@respx.mock
@pytest.mark.parametrize("tokens, window, context", [(20537, None, 32768), (9000, None, 20480), (1000, None, 16384), (9000, "65536", 65536),
                                                    (60000, "65536", 69632)])
async def test_the_context_holds_the_rules_and_tools_with_room_and_is_not_less_than_the_window_for_local_models(
        client, home, monkeypatch, tokens, window, context):
    fixed_overhead(monkeypatch, tokens)
    if window:
        monkeypatch.setenv("HUNTERAI_OLLAMA_NUM_CTX", window)
    program = unsloth(home)
    nothing_at()
    ollama_has(home, {})
    got = (await client.get(FIND)).json()
    assert got["context"] == context and got["plain"] == f"{program} -m your-model.gguf -c {context} --port 9931"


@respx.mock
async def test_a_model_ollama_lists_and_does_not_show_is_left_out(client, home, tmp_path, monkeypatch):
    fixed_overhead(monkeypatch, 9000)
    unsloth(home)
    nothing_at()
    ollama_has(tmp_path, {"gone:1b": None, "tiny:latest": wire.TINY})
    got = (await client.get(FIND)).json()
    assert [m["name"] for m in got["models"]] == ["tiny:latest"] and got["ollama"] is True


@respx.mock
async def test_a_model_that_cannot_be_started_is_offered_with_the_reason(client, home, tmp_path, monkeypatch):
    fixed_overhead(monkeypatch, 9000)
    unsloth(home)
    nothing_at()
    elsewhere = {**wire.TINY, "modelfile": wire.TINY["modelfile"].replace("/BLOBS", "/nowhere/.ollama/models/blobs")}
    ollama_has(tmp_path, {"tiny:latest": elsewhere})         # as an Ollama in a container names its files
    model = (await client.get(FIND)).json()["models"][0]
    assert model["command"] is None and model["name"] == "tiny:latest"
    assert model["why_not"].startswith(f"its file is not on this machine where Ollama names it (/nowhere/.ollama/models/blobs/{TINY_FILE})")


@respx.mock
async def test_of_two_programs_the_one_that_sees_a_card_is_named(client, home, tmp_path, monkeypatch):
    fixed_overhead(monkeypatch, 9000)
    says(tmp_path / "bin" / "llama-server", SEES_NONE)
    with_card = unsloth(home)
    nothing_at()
    ollama_has(tmp_path, {})
    got = (await client.get(FIND)).json()
    assert got["program"] == with_card and got["devices"] == [CARD] and got["from_ollama"] is False
    Path(with_card).unlink()
    got = (await client.get(FIND)).json()
    assert got["program"] == str(tmp_path / "bin" / "llama-server") and got["devices"] == []


@respx.mock
async def test_on_a_machine_like_the_owners_the_command_starts_ollamas_llama_server_with_its_part_for_the_card(client, home, tmp_path, monkeypatch):
    fixed_overhead(monkeypatch, 20537)
    lib = ollama_at(tmp_path / "usr" / "local", "vulkan", "cuda_v12", "cuda_v13")
    monkeypatch.setenv("PATH", str(tmp_path / "usr" / "local" / "bin"))
    unsloth(home, SEES_NONE)
    nothing_at()
    ollama_has(tmp_path / "blobs", {"tiny:latest": wire.TINY})
    got = (await client.get(FIND)).json()
    start = f"LD_LIBRARY_PATH={lib}:{lib}/cuda_v13 GGML_BACKEND_PATH={lib}/cuda_v13/libggml-cuda.so {lib}/llama-server"
    assert got["program"] == f"{lib}/llama-server" and got["from_ollama"] is True
    assert got["devices"] == [{"name": "CUDA0", "card": "NVIDIA GeForce RTX 5080 Laptop GPU", "total_mb": 15839, "free_mb": 6015}]
    assert got["plain"] == f"{start} -m your-model.gguf -c 32768 --port 9931"
    assert got["models"][0]["command"] == f"{start} -m {tmp_path}/blobs/{TINY_FILE} -c 2048 --alias tiny --port 9931 {TINY_FLAGS}"


@respx.mock
@pytest.mark.parametrize("tags", [{"models": "many"}, {"models": 7}, {"models": [7, {"size": 3}, {"name": ""}, {"name": None}]}, {}, {"models": None}])
async def test_a_list_of_another_shape_offers_no_model(client, home, monkeypatch, tags):
    fixed_overhead(monkeypatch, 9000)
    unsloth(home)
    nothing_at()
    respx.get(OLLAMA + "/api/tags").mock(return_value=httpx.Response(200, json=tags))
    respx.get(OLLAMA + "/api/ps").mock(return_value=httpx.Response(200, json={"models": [3, {"size": 1}]}))
    got = (await client.get(FIND)).json()
    assert got["ollama"] is True and got["models"] == [] and got["held"] == []


@respx.mock
@pytest.mark.parametrize("answer", [httpx.Response(500, text="broken"), httpx.Response(200, text="<html>"), httpx.Response(200, text="[1]")])
async def test_what_ollama_shows_in_another_shape_is_no_model(answer):
    respx.post(OLLAMA + "/api/show").mock(return_value=answer)
    assert await llamafind.shown("tiny:latest") is None


@respx.mock
async def test_ollama_is_asked_for_one_model_by_its_name():
    asked = respx.post(OLLAMA + "/api/show").mock(return_value=httpx.Response(200, json=wire.TINY))
    assert await llamafind.shown("tiny:latest") == wire.TINY
    assert json.loads(asked.calls.last.request.content) == {"model": "tiny:latest"}
