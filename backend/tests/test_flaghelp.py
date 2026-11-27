"""A program that answers "flag provided but not defined" was given a flag from the model's memory. Seen on the owner's
machine: the instructions say to ask a program for its flags and not to invent any, and a local model answered one
unknown flag with two more guesses. What the program's own help says about the flag now comes with the error."""
import asyncio
import time

import pytest

from hunterai import flaghelp, procs, toolbox
from hunterai.providers.base import ToolCall
from hunterai.tools import registry, shell
from tests.toolkit import run_call, seeded_run

ALLOW = {"risk": {"execute": "allow"}}

# The help of a made-up program, laid out as the tools of one well-known family lay theirs out.
HELP = """probe is a made-up program for these tests.

Usage:
  probe [flags]

Flags:
INPUT:
   -u, -target string[]     input target host(s) to probe
   -l, -list string         input file containing list of hosts to process

PROBES:
   -sc, -status-code        display response status-code
   -title                   display page title

MATCHERS:
   -mc, -match-code string  match response with specified status code (-mc 200,302)

RATE-LIMIT:
   -rl, -rate-limit int     maximum requests to send per second (default 150)
   -timeout int             timeout in seconds (default 10)
"""
PROBE = """#!/bin/sh
case "$1" in
  -h) echo asked >> "$0.asked"; cat "$0.help"; exit 0;;
esac
for a in "$@"; do
  case "$a" in
    -scrape|-status-code-range|-resolve-all) echo "flag provided but not defined: $a" >&2; exit 2;;
  esac
done
echo probed
"""


@pytest.fixture(autouse=True)
def shell_registered():
    for t in shell.tools():
        registry.register(t)
    flaghelp.forget()


def program(name: str, text: str = PROBE, help_text: str = HELP):
    path = toolbox.folder() / name
    path.write_text(text)
    path.chmod(0o755)
    (toolbox.folder() / f"{name}.help").write_text(help_text)
    return path


def asked(name: str = "probe") -> int:
    """How often the program was asked for its help."""
    counted = toolbox.folder() / f"{name}.asked"
    return len(counted.read_text().splitlines()) if counted.exists() else 0


async def run(conn, command: str, **args) -> str:
    chat, run_id = await seeded_run(conn, permissions=ALLOW)
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c", "execute", {"command": command, **args}))
    return out.block.content


# ---------------------------------------------------------------- what a program says when it does not know a flag

@pytest.mark.parametrize("said, flag, switch", [
    ("flag provided but not defined: -scrape\nUsage of probe:", "-scrape", "-h"),                    # Go's flag package
    ("Error: unknown flag: --scrape\nUsage:\n  probe [flags]", "--scrape", "--help"),                # pflag, cobra
    ("Error: unknown shorthand flag: 'z' in -z", "-z", "--help"),
    ("usage: probe [-h] [-u TARGET]\nprobe: error: unrecognized arguments: --scrape pages", "--scrape", "-h"),     # argparse
    ("probe: unrecognized option '--scrape'\nTry 'probe --help' for more information.", "--scrape", "--help"),     # getopt
    ("probe: invalid option -- 'z'", "-z", "--help"),
    ("Error: No such option: --scrape", "--scrape", "--help"),                                       # click
    ("Usage: probe [OPTIONS]\nTry 'probe --help' for help.\n\nError: No such option '--scrape'.", "--scrape", "--help"),
    ("Error: no such option: -z", "-z", "--help"),
    ("error: unexpected argument '--scrape' found", "--scrape", "--help"),                           # clap
])
def test_the_flag_a_program_does_not_know_is_read_from_what_it_says(said, flag, switch):
    assert flaghelp.unknown(said) == [(flag, switch)]


def test_other_output_names_no_unknown_flag():
    assert flaghelp.unknown("") == []
    assert flaghelp.unknown("probed 3 hosts\nexit") == []
    assert flaghelp.unknown("the flag -scrape is fine here") == []
    assert flaghelp.unknown("unrecognized arguments: pages") == []                 # a word that is no flag


def test_a_flag_is_named_once_and_no_more_than_a_few_are():
    said = "\n".join(f"flag provided but not defined: -f{n}" for n in (1, 1, 2, 3, 4, 5))
    assert flaghelp.unknown(said) == [("-f1", "-h"), ("-f2", "-h"), ("-f3", "-h")]


# ---------------------------------------------------------------- what the help has that is close

def test_the_lines_of_the_help_closest_to_a_flag_are_found_by_its_words():
    got = flaghelp.closest(HELP, "-status-code-range")
    assert got[0] == "-sc, -status-code        display response status-code"
    assert "-mc, -match-code string  match response with specified status code (-mc 200,302)" in got
    assert not any(line.startswith(("-u,", "-l,", "-title", "-timeout")) for line in got)


def test_and_by_its_spelling():
    assert flaghelp.closest(HELP, "-titel") == ["-title                   display page title"]
    assert flaghelp.closest(HELP, "--rate-limit")[0].startswith("-rl, -rate-limit int")          # two dashes for one
    assert flaghelp.closest(HELP, "-time-out")[0].startswith("-timeout int")


def test_a_flag_with_nothing_close_gets_no_line():
    assert flaghelp.closest(HELP, "-scrape") == []
    assert flaghelp.closest(HELP, "-z") == []
    assert flaghelp.closest("", "-status-code") == []
    assert flaghelp.closest("no flags here\njust words", "-status-code") == []


def test_a_description_on_the_line_after_its_flag_comes_with_it():
    """How Go's own flag package prints a help."""
    plain = "Usage of probe:\n  -match-code string\n    \tmatch response with specified status code\n  -title\n    \tdisplay page title\n"
    assert flaghelp.closest(plain, "-match-codes") == ["-match-code string: match response with specified status code"]


def test_what_a_program_with_gos_own_flag_package_prints_is_read():
    """Copied from such a program, built for this: it prints its whole help after the error."""
    said = ("flag provided but not defined: -status-code-range\nUsage of ./goprobe:\n  -match-code string\n    \tmatch response with "
            "specified status code\n  -rate-limit int\n    \tmaximum requests to send per second (default 150)\n  -status-code\n"
            "    \tdisplay response status-code\n  -target string\n    \tinput target host to probe\n")
    assert flaghelp.unknown(said) == [("-status-code-range", "-h")]
    assert flaghelp.closest(said, "-status-code-range") == ["-status-code: display response status-code",
                                                            "-match-code string: match response with specified status code"]


def test_no_more_than_a_few_lines_and_none_of_them_long():
    many = "\n".join(f"   -code-{n}, -match-code-{n} string   " + "matches the code " * 30 for n in range(40))
    got = flaghelp.closest(many, "-match-code")
    assert len(got) == 6 and {len(line) for line in got} == {200}


def test_a_flag_without_a_description_does_not_take_the_next_flag_for_one():
    plain = "Usage of probe:\n  -title\n  -match-code string\n    \tmatch response with specified status code\n"
    assert flaghelp.closest(plain, "-titel") == ["-title"]
    assert flaghelp.closest(plain, "-match-codes") == ["-match-code string: match response with specified status code"]


def test_a_flag_that_a_description_only_mentions_does_not_make_its_entry_close():
    text = HELP + "   -x, -extra               the same as -title, for old servers\n"
    assert flaghelp.closest(text, "-titel") == ["-title                   display page title"]


def test_a_piece_of_one_or_two_letters_is_no_word_to_go_by():
    assert flaghelp.closest(HELP, "-no-u") == []                        # not the entry of -u
    assert flaghelp.closest(HELP, "-l-x") == []


# ---------------------------------------------------------------- through the tool that runs commands

async def test_a_flag_error_of_a_program_of_the_tool_folder_comes_with_what_its_help_says(conn):
    program("probe")
    text = await run(conn, "probe -u a.shop.example -status-code-range 200-599")
    assert text.startswith("exit code: 2") and "flag provided but not defined: -status-code-range" in text
    note = text[text.index("[probe does not know the flag"):]
    assert note.startswith("[probe does not know the flag -status-code-range. Its own help (probe -h) has these that are close:\n")
    assert "\n  -sc, -status-code        display response status-code\n" in note
    assert "\n  -mc, -match-code string  match response with specified status code (-mc 200,302)\n" in note
    assert note.endswith("Use a flag only as the help writes it; read all of it with: probe -h]")
    assert asked() == 1


async def test_a_flag_with_nothing_close_is_sent_to_the_help(conn):
    program("probe")
    text = await run(conn, "probe -scrape -u a.shop.example")
    assert text.endswith("[probe does not know the flag -scrape, and its own help (probe -h) has no flag close to it. "
                         "Do not try another guess: read the help first, with: probe -h]")


async def test_the_help_of_a_program_is_asked_for_once(conn):
    program("probe")
    for flag in ("-scrape", "-status-code-range 1", "-resolve-all"):
        assert "[probe does not know the flag" in await run(conn, f"probe {flag}")
    assert asked() == 1


async def test_a_program_that_was_replaced_is_asked_again(conn):
    path = program("probe")
    await run(conn, "probe -scrape")
    path.write_text(PROBE + "# a newer one\n")
    assert "[probe does not know the flag" in await run(conn, "probe -scrape")
    assert asked() == 2


async def test_a_command_without_a_flag_error_gets_nothing_added_and_no_help_is_asked_for(conn):
    program("probe")
    assert (await run(conn, "probe -u a.shop.example")).rstrip().endswith("probed")
    assert (await run(conn, "echo 'flag provided but not defined: -scrape' is what probe would say")).count("[") == 0
    assert asked() == 0


async def test_a_program_that_is_not_in_the_tool_folder_is_not_asked_for_its_help(conn, tmp_path):
    other = tmp_path / "elsewhere"
    other.write_text(PROBE)
    other.chmod(0o755)
    (tmp_path / "elsewhere.help").write_text(HELP)
    text = await run(conn, f"{other} -scrape")
    assert "flag provided but not defined: -scrape" in text and "does not know the flag" not in text
    assert not (tmp_path / "elsewhere.asked").exists()


async def test_the_error_is_laid_to_the_program_that_was_given_the_flag(conn):
    program("probe")
    program("other", help_text="Usage of other:\n   -sc, -score   a flag of the other program\n")
    text = await run(conn, "other -u x | probe -status-code-range 1")
    assert "[probe does not know the flag -status-code-range." in text and "[other does not know" not in text
    assert (asked("probe"), asked("other")) == (1, 0)


async def test_a_flag_that_stands_in_quotes_is_laid_to_the_only_program_there_is(conn):
    program("probe")
    assert "[probe does not know the flag -scrape" in await run(conn, 'probe "-scrape"')


async def test_and_to_no_program_when_two_could_have_been_given_it(conn):
    program("probe")
    program("other", help_text="Usage of other:\n   -sc, -score   a flag of the other program\n")
    text = await run(conn, 'other -u x | probe "-scrape"')
    assert "flag provided but not defined: -scrape" in text and "does not know the flag" not in text
    assert (asked("probe"), asked("other")) == (0, 0)


async def test_a_help_that_does_not_come_is_not_waited_for(conn, monkeypatch):
    monkeypatch.setattr(flaghelp, "HELP_SECONDS", 0.3)
    program("probe", text=PROBE.replace('-h) echo asked >> "$0.asked"; cat "$0.help"; exit 0;;', "-h) sleep 30;;"))
    began = time.monotonic()
    text = await run(conn, "probe -scrape")
    assert time.monotonic() - began < 5
    assert text.endswith("[probe does not know the flag -scrape. Do not try another guess: read its help first, with: probe -h]")


async def test_a_program_asked_for_its_help_by_the_model_gets_nothing_added(conn):
    program("probe")
    text = await run(conn, "probe -h")
    assert "-sc, -status-code" in text and "does not know the flag" not in text


async def test_a_background_run_that_ends_at_once_says_it_too(conn):
    program("probe")
    text = await run(conn, "probe -scrape", background=True)
    assert text.startswith("exit code: 2") and "[probe does not know the flag -scrape" in text


async def test_the_output_of_a_background_run_says_it_when_it_is_read(conn, monkeypatch):
    monkeypatch.setattr(procs, "BACKGROUND_FIRST_WAIT", 0.05)
    program("probe")
    chat, run_id = await seeded_run(conn, permissions=ALLOW)
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c", "execute", {"command": "sleep 0.4; probe -status-code-range 1", "background": True}))
    assert out.block.content.startswith("started background process") and "does not know the flag" not in out.block.content
    proc_id = out.block.content.split()[3]
    for _ in range(100):
        read, _, _ = await run_call(conn, chat, run_id, ToolCall("r", "process_output", {"id": proc_id}))
        if "status: exited" in read.block.content:
            break
        await asyncio.sleep(0.05)
    assert "flag provided but not defined: -status-code-range" in read.block.content
    assert "[probe does not know the flag -status-code-range. Its own help (probe -h) has these that are close:" in read.block.content
