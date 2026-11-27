"""The machine as the Live monitor shows it: the processor's load, who holds the memory, and each graphics card."""
import os
import time

import pytest

from hunterai import monitor, thermal
from tests.test_api import client  # noqa: F401 (client is a fixture)
from tests.test_thermal import ASKED, default_limits

GIB, MIB = 1024**3, 1024**2
SMI = ASKED + 'case "$1" in\npmon) cat "$FAKE_OUT/pmon" ;;\n*) cat "$FAKE_OUT/cards" ;;\nesac\n'
QUERY = "--query-gpu=index,utilization.gpu,temperature.gpu,power.draw,memory.used,memory.total,name --format=csv,noheader,nounits"
ONE = "0, 32, 50, 31.20, 10444, 16303, NVIDIA GeForce RTX 5080 Laptop GPU\n"
PMON = ("# gpu         pid   type     fb   ccpm    command \n"
        "# Idx           #    C/G     MB     MB    name \n"
        "    0       4321     C   7864      0    coli           \n"
        "    0       1500     G    412      0    Xorg           \n"
        "    0       2600   C+G    128      0    firefox-esr    \n")
CARD = {"name": "NVIDIA GeForce RTX 5080 Laptop GPU", "load": 32.0, "temp": 50.0, "limit": 85.0, "power": 31.2,
        "used": 10444 * MIB, "total": 16303 * MIB}


class Proc:
    """A /proc of the test's own."""
    def __init__(self, root):
        self.root = root

    def stat(self, busy, idle, iowait=0):
        (self.root / "stat").write_text(f"cpu  {busy} 0 0 {idle} {iowait} 0 0 0 0 0\ncpu0 1 2 3 4 5 6 7 8 9 10\nintr 5\n")

    def memory(self, total_kb, available_kb):
        (self.root / "meminfo").write_text(f"MemTotal:       {total_kb} kB\nMemFree:          100 kB\n"
                                           f"MemAvailable:   {available_kb} kB\nBuffers:            5 kB\n")

    def process(self, pid, name, anon_kb=None, exe=None, args=None):
        folder = self.root / str(pid)
        folder.mkdir()
        held = f"RssAnon:\t{anon_kb:>8} kB\nRssFile:\t  999999 kB\n" if anon_kb is not None else ""
        (folder / "status").write_text(f"Name:\t{name}\nUmask:\t0022\nState:\tS (sleeping)\nVmRSS:\t 5000000 kB\n{held}Threads:\t4\n")
        if exe:
            os.symlink(exe, folder / "exe")
        if args is not None:
            (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in args) + b"\0")


@pytest.fixture
def proc(tmp_path, monkeypatch):
    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setattr(monitor, "PROC", root)
    monkeypatch.setattr(monitor, "CPU_LOOK", 0)
    default_limits(monkeypatch)
    return Proc(root)


@pytest.fixture
def card(fake_bin, monkeypatch):
    """A machine with nvidia-smi: card(cards, pmon) sets what it prints, and returns the folder with what it was asked."""
    monkeypatch.setattr(thermal, "NVIDIA_SMI", "nvidia-smi")
    monkeypatch.setattr(monitor, "KEEP", 0)
    default_limits(monkeypatch)

    def says(cards=ONE, pmon=PMON, script=SMI):
        out = fake_bin("nvidia-smi", script)
        (out / "cards").write_text(cards)
        (out / "pmon").write_text(pmon)
        return out
    return says


# ---- the processor

async def test_the_load_of_the_processor_is_the_share_of_its_time_that_was_work_since_the_last_look(proc):
    proc.stat(busy=1000, idle=9000)
    assert await monitor.cpu_load() is None                    # no time passed between its two looks
    proc.stat(busy=1480, idle=9520)                            # 480 of the 1000 ticks since were work
    assert await monitor.cpu_load() == 48.0
    proc.stat(busy=1480, idle=10520)
    assert await monitor.cpu_load() == 0.0
    proc.stat(busy=2480, idle=10520)
    assert await monitor.cpu_load() == 100.0


async def test_time_spent_waiting_for_a_disk_is_not_work(proc):
    proc.stat(busy=1000, idle=9000)
    await monitor.cpu_load()
    proc.stat(busy=1100, idle=9400, iowait=500)
    assert await monitor.cpu_load() == 10.0


async def test_the_first_look_at_the_processor_measures_over_a_moment(proc, monkeypatch):
    proc.stat(busy=1000, idle=9000)
    async def a_moment():
        proc.stat(busy=1250, idle=9750)
    monkeypatch.setattr(monitor, "_moment", a_moment)
    assert await monitor.cpu_load() == 25.0


async def test_a_look_from_long_ago_is_not_measured_against(proc, monkeypatch):
    proc.stat(busy=1000, idle=9000)
    await monitor.cpu_load()
    monkeypatch.setattr(monitor, "CPU_STALE", 0)               # the panel was closed for a while
    proc.stat(busy=5000, idle=9000)
    assert await monitor.cpu_load() is None                    # its two fresh looks saw the same counters
    monkeypatch.setattr(monitor, "CPU_STALE", 15.0)
    proc.stat(busy=5100, idle=9900)
    assert await monitor.cpu_load() == 10.0


async def test_a_machine_whose_counters_cannot_be_read_has_no_load(proc):
    assert await monitor.cpu_load() is None
    (proc.root / "stat").write_text("intr 5\n")
    assert await monitor.cpu_load() is None
    (proc.root / "stat").write_text("cpu  1 2 many\n")
    assert await monitor.cpu_load() is None
    (proc.root / "stat").write_text("cpu0 100 0 0 900 0 0 0 0 0 0\n")         # one processor's line is not the line of them all
    await monitor.cpu_load()
    (proc.root / "stat").write_text("cpu0 200 0 0 1800 0 0 0 0 0 0\n")
    assert await monitor.cpu_load() is None


# ---- the memory

def test_memory_in_use_is_what_the_kernel_does_not_call_available(proc):
    proc.memory(total_kb=31_666_176, available_kb=5_515_264)
    assert monitor.memory() == {"used": (31_666_176 - 5_515_264) * 1024, "total": 31_666_176 * 1024, "holders": []}


def test_a_machine_that_does_not_say_how_much_memory_it_has_has_none_to_show(proc):
    assert monitor.memory() is None
    (proc.root / "meminfo").write_text("MemTotal:       1000 kB\n")
    assert monitor.memory() is None
    (proc.root / "meminfo").write_text("MemTotal: lots kB\nMemAvailable: 5 kB\n")
    assert monitor.memory() is None


def test_the_programs_that_hold_the_most_memory_are_named_largest_first(proc, monkeypatch):
    proc.memory(32 * MIB, 8 * MIB)                             # in kB: 32 GiB, of which 8 are available
    proc.process(10, "coli", anon_kb=8 * MIB, exe="/run/media/dave/1TB_HAI/colibri/c/coli")
    proc.process(11, "firefox-esr", anon_kb=1 * MIB, exe="/usr/lib/firefox-esr/firefox-esr")
    proc.process(12, "Isolated Web Co", anon_kb=2 * MIB, exe="/usr/lib/firefox-esr/firefox-esr")      # the processes of one program are one holder
    proc.process(13, "ollama", anon_kb=4 * MIB)                # another user's: the file it runs cannot be read, its name can
    proc.process(14, "kworker/0:1")                            # a thread of the kernel holds none
    proc.process(15, "old", anon_kb=4096, exe="/opt/tool/bin/renamed (deleted)")
    proc.process(16, "sshd", anon_kb=2048, exe="/usr/sbin/sshd")
    (proc.root / "17").mkdir()                                 # a process that ended while the list was read
    (proc.root / "self").mkdir()
    assert monitor.memory()["holders"] == [{"name": "coli", "bytes": 8 * GIB}, {"name": "ollama", "bytes": 4 * GIB},
                                           {"name": "firefox-esr", "bytes": 3 * GIB}, {"name": "renamed", "bytes": 4096 * 1024}]
    monkeypatch.setattr(monitor, "HOLDERS", 2)
    assert [h["name"] for h in monitor.memory()["holders"]] == ["coli", "ollama"]


def test_programs_that_hold_the_same_are_named_in_the_order_of_the_alphabet(proc):
    proc.memory(1000, 500)
    for pid, name in ((1, "zsh"), (2, "bash"), (3, "fish")):
        proc.process(pid, name, anon_kb=100, exe=f"/usr/bin/{name}")
    proc.process(4, "asleep", anon_kb=0, exe="/usr/bin/asleep")          # a process that holds nothing is not a holder
    assert [h["name"] for h in monitor.memory()["holders"]] == ["bash", "fish", "zsh"]


def test_a_python_process_is_named_for_what_it_was_started_to_run(proc):
    proc.memory(1000, 500)
    proc.process(20, "python", anon_kb=4000, exe="/usr/bin/python3.12", args=["python", "-m", "hunterai.worker"])
    proc.process(21, "python3", anon_kb=3000, exe="/home/dave/studio/.venv/bin/python3",
                 args=["/home/dave/studio/.venv/bin/python3", "-X", "dev", "-u", "/home/dave/studio/.venv/bin/unsloth", "studio"])
    proc.process(22, "python3", anon_kb=2000, exe="/usr/bin/python3.12", args=["python3", "-c", "import time; time.sleep(9)"])
    proc.process(23, "python3", anon_kb=1000, exe="/usr/bin/python3", args=["python3", "-W", "ignore", "/home/dave/train.py", "--epochs", "3"])
    assert monitor.memory()["holders"] == [{"name": "hunterai", "bytes": 4000 * 1024}, {"name": "unsloth", "bytes": 3000 * 1024},
                                           {"name": "python3.12", "bytes": 2000 * 1024}, {"name": "train", "bytes": 1000 * 1024}]


def test_a_python_whose_arguments_cannot_be_read_keeps_the_name_of_its_file(proc):
    proc.memory(1000, 500)
    proc.process(24, "python3", anon_kb=500, exe="/usr/bin/python3.12")
    proc.process(25, "python3", anon_kb=400, exe="/usr/bin/python3.13", args=["python3"])
    proc.process(26, "pythonista", anon_kb=300, exe="/usr/bin/pythonista", args=["pythonista", "-m", "other"])       # not Python
    assert [h["name"] for h in monitor.memory()["holders"]] == ["python3.12", "python3.13", "pythonista"]


# ---- the graphics cards

async def test_a_graphics_card_is_read_from_nvidia_smi_with_who_holds_its_memory(card, proc):
    out = card()
    assert await monitor.cards() == [{**CARD, "holders": [{"name": "coli", "bytes": 7864 * MIB}, {"name": "Xorg", "bytes": 412 * MIB},
                                                         {"name": "firefox-esr", "bytes": 128 * MIB}]}]
    assert sorted((out / "asked").read_text().splitlines()) == [QUERY, "pmon -c 1 -s m"]


async def test_a_machine_without_nvidia_smi_has_no_cards(proc):
    assert await monitor.cards() == []


async def test_who_holds_a_cards_memory_is_named_as_in_the_list_of_the_memory(card, proc):
    proc.process(4321, "python3", anon_kb=10, exe="/usr/bin/python3.12", args=["python3", "-m", "studio.server"])
    proc.process(1500, "llama-server", anon_kb=10, exe="/usr/local/bin/llama-server")
    proc.process(2600, "llama-server", anon_kb=10, exe="/usr/local/bin/llama-server")
    card()
    assert (await monitor.cards())[0]["holders"] == [{"name": "studio", "bytes": 7864 * MIB}, {"name": "llama-server", "bytes": 540 * MIB}]


async def test_only_the_largest_holders_of_a_card_are_named(card, proc, monkeypatch):
    monkeypatch.setattr(monitor, "HOLDERS", 2)
    card()
    assert [h["name"] for h in (await monitor.cards())[0]["holders"]] == ["coli", "Xorg"]


async def test_what_a_card_or_its_driver_does_not_say_is_left_open(card, proc):
    card(cards="0, 5, 40, 10.00, 100, 200, ACME Card, the big one\n1, [N/A], 41, [N/A], 3, 8192, Second\nnot a card\n",
         pmon=("# gpu        pid  type    fb   command\n# Idx          #   C/G    MB   name\n"
               "    0      4321     C  7864   coli\n    0      1500     G     -   Xorg\n    1         -     -     -   -\n"
               "    0      77       C    12   two words\n"))
    first, second = await monitor.cards()
    assert first == {"name": "ACME Card, the big one", "load": 5.0, "temp": 40.0, "limit": 85.0, "power": 10.0, "used": 100 * MIB,
                     "total": 200 * MIB, "holders": [{"name": "coli", "bytes": 7864 * MIB}, {"name": "two words", "bytes": 12 * MIB}]}
    assert second == {"name": "Second", "load": None, "temp": 41.0, "limit": 85.0, "power": None, "used": 3 * MIB, "total": 8192 * MIB,
                      "holders": []}


async def test_a_card_whose_holders_cannot_be_asked_is_shown_without_them(card, proc):
    card(script=ASKED + 'case "$1" in\npmon) echo "Failed to initialize NVML"; exit 9 ;;\n*) cat "$FAKE_OUT/cards" ;;\nesac\n')
    assert await monitor.cards() == [{**CARD, "holders": []}]
    card(pmon="no table at all\n   0   4321   C   7864   0   coli\n")
    assert await monitor.cards() == [{**CARD, "holders": []}]


async def test_a_nvidia_smi_that_fails_or_names_no_card_is_no_card(card, proc, monkeypatch):
    monkeypatch.setattr(monitor, "RETRY", 0)
    for broken in ("#!/bin/sh\necho 'NVIDIA-SMI has failed because it could not communicate with the NVIDIA driver.'\nexit 9\n",
                   "#!/bin/sh\necho\n", "#!/bin/sh\necho '0, 32, 50, 31.20, 10444, 16303, A card'\nexit 1\n"):
        card(script=broken)
        assert await monitor.cards() == [], broken


async def test_a_nvidia_smi_that_hangs_is_not_waited_for(card, proc, monkeypatch):
    monkeypatch.setattr(monitor, "TIMEOUT", 0.2)
    card(script="#!/bin/sh\nexec sleep 30\n")
    began = time.monotonic()
    assert await monitor.cards() == [] and time.monotonic() - began < 10


async def test_one_look_at_the_cards_serves_for_a_moment_and_a_failure_for_longer(card, proc, monkeypatch):
    monkeypatch.setattr(monitor, "KEEP", 2.0)
    out = card()
    def asked():
        return len((out / "asked").read_text().splitlines())
    assert [len(await monitor.cards()) for _ in range(4)] == [1] * 4 and asked() == 2          # one look is two programs
    monkeypatch.setattr(monitor, "KEEP", 0)
    assert len(await monitor.cards()) == 1 and asked() == 4
    card(script=ASKED + "exit 9\n")
    assert await monitor.cards() == [] and asked() == 6
    card()
    assert await monitor.cards() == [] and asked() == 6        # it failed just now: not asked again so soon
    monkeypatch.setattr(monitor, "RETRY", 0)
    assert len(await monitor.cards()) == 1


# ---- one reading

async def test_one_reading_has_the_processor_the_memory_and_the_cards(card, proc, monkeypatch):
    monkeypatch.setattr(thermal, "cpu_temp", lambda: 61.0)
    proc.memory(total_kb=2000, available_kb=500)
    proc.process(10, "coli", anon_kb=700, exe="/opt/colibri/c/coli")
    async def a_moment():
        proc.stat(busy=1480, idle=9520)
    proc.stat(busy=1000, idle=9000)
    monkeypatch.setattr(monitor, "_moment", a_moment)
    card()
    assert await monitor.read() == {
        "cpu": {"load": 48.0, "temp": 61.0, "limit": 90.0},
        "ram": {"used": 1500 * 1024, "total": 2000 * 1024, "holders": [{"name": "coli", "bytes": 700 * 1024}]},
        "cards": [{**CARD, "holders": [{"name": "coli", "bytes": 7864 * MIB}, {"name": "Xorg", "bytes": 412 * MIB},
                                       {"name": "firefox-esr", "bytes": 128 * MIB}]}]}


async def test_a_machine_that_says_nothing_gives_a_reading_with_nothing_in_it(monkeypatch):
    default_limits(monkeypatch)
    monkeypatch.setattr(monitor, "CPU_LOOK", 0)
    assert await monitor.read() == {"cpu": {"load": None, "temp": None, "limit": 90.0}, "ram": None, "cards": []}


async def test_the_page_asks_for_the_reading(client, proc):
    proc.memory(total_kb=2000, available_kb=500)
    answer = await client.get("/api/system/monitor")
    assert answer.status_code == 200
    assert answer.json() == {"cpu": {"load": None, "temp": None, "limit": 90.0},
                             "ram": {"used": 1500 * 1024, "total": 2000 * 1024, "holders": []}, "cards": []}
