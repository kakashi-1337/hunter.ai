"""With recording in Caido switched on, every command of the shell is given the address of Caido's proxy in a
variable, and the model is told to hand it to a program's own proxy switch. Nothing else about a command
changes: what is not told to go through Caido goes out as before, so an install does not fail on Caido's
certificate."""
import shutil

import pytest

from hunterai import caido, upstream
from hunterai.providers.base import TextDelta, ToolCall
from hunterai.repo import messages, settings
from hunterai.tools import registry, shell
from hunterai.types import TextBlock
from tests.proxies import HttpProxy, Site, running
from tests.test_engine import go, setup, use  # noqa: F401 - `setup` is a fixture `world` needs
from tests.test_subagents import ACCEPT, BRIEF, WORTH, world  # noqa: F401 - `world` is a fixture: delegate, with its decisions scripted
from tests.test_tools_caido import URL, call as caido_call, fake  # noqa: F401 - `fake` is a fixture: a Caido that answers by what is asked
from tests.toolkit import Turns, run_call, seeded_run

ALLOW = {"risk": {"execute": "allow"}}
PROXY_NAMES = "env | grep -E '^(ALL|HTTPS?|NO|CAIDO)_PROXY=|^(all|https?|no|caido)_proxy=' | LC_ALL=C sort"
THROUGH = 'curl -sS --max-time 10 -x "$CAIDO_PROXY" http://shop.acme.test/x'
DIRECT = "curl -sS --max-time 5 http://shop.acme.test/x"
NOT_REACHED = ("[Caido could not be reached (Caido does not answer at http://127.0.0.1:8791): CAIDO_PROXY is not set, so "
               "nothing this command sent was recorded.]")
pytestmark = pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")


@pytest.fixture(autouse=True)
def shell_registered(monkeypatch):
    for t in shell.tools():
        registry.register(t)
    for name in ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "all_proxy", "http_proxy", "https_proxy", "no_proxy",
                 "CAIDO_PROXY"):
        monkeypatch.delenv(name, raising=False)       # whatever proxy this machine itself is behind is no part of a test
    monkeypatch.setitem(caido._unreached, "until", 0.0)   # noqa: SLF001 - what an earlier test could not reach is not this one's


def a_caido(monkeypatch, port: int | None, why: str = "Caido does not answer at http://127.0.0.1:8791") -> list:
    """Caido is on this machine; its proxy listens on `port`, or cannot be reached when that is None.
    Returns the list that grows by one each time Caido is asked for its address."""
    asked = []

    async def address():
        asked.append(1)
        if port is None:
            raise caido.CaidoError(why)
        return "127.0.0.1", port
    monkeypatch.setattr(caido, "available", lambda: True)
    monkeypatch.setattr(caido, "proxy_address", address)
    return asked


async def a_chat(conn, recording=True, proxy=None):
    """A chat on a target, with recording in Caido switched on or off."""
    chat, run_id = await seeded_run(conn, scope={"in": ["*.acme.test"], "out": []}, permissions=ALLOW)
    await settings.put(conn, "browser_via_caido", bool(recording))      # said either way: where nobody says, it is on
    if proxy:
        await upstream.put_target(conn, chat.target_id, proxy)
    return chat, run_id


async def command(conn, chat, run_id, text, **more) -> str:
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c", "execute", {"command": text, "timeout": 15, **more}))
    return out.block.content


async def run(conn, text, recording=True, proxy=None, **more) -> str:
    """The result of the command `text` in a new chat."""
    chat, run_id = await a_chat(conn, recording, proxy)
    return await command(conn, chat, run_id, text, **more)


async def test_a_command_told_to_go_through_caido_is_carried_by_it(conn, monkeypatch):
    site = Site()
    recorder = HttpProxy(site)
    async with running(site, recorder):
        a_caido(monkeypatch, recorder.port)
        text = await run(conn, f'echo "at $CAIDO_PROXY"; {THROUGH}')
    lines = text.splitlines()
    assert lines[0] == "exit code: 0" and f"at http://127.0.0.1:{recorder.port}" in lines and "site says: shop.acme.test/x" in lines, text
    assert recorder.asked == [("GET", "http://shop.acme.test/x")] and "Caido" not in text


async def test_nothing_else_about_a_command_changes(conn, monkeypatch):
    """No HTTP_PROXY and its kin: a command that is not told to go through Caido does not, so pip, git and
    apt go on working (through Caido they would fail on its certificate)."""
    site = Site()
    recorder = HttpProxy(site)
    async with running(site, recorder):
        a_caido(monkeypatch, recorder.port)
        text = await run(conn, f"{PROXY_NAMES}; {DIRECT}; echo done")
    assert [line for line in text.splitlines() if "_PROXY=" in line.upper()] == [f"CAIDO_PROXY=http://127.0.0.1:{recorder.port}"]
    assert recorder.asked == [] and "done" in text.splitlines()


async def test_with_recording_off_a_command_gets_no_address_and_caido_is_left_alone(conn, monkeypatch):
    asked = a_caido(monkeypatch, 18791)
    text = await run(conn, f'{PROXY_NAMES}; echo "at [$CAIDO_PROXY]"', recording=False)
    assert "at []" in text.splitlines() and "CAIDO_PROXY=" not in text and "Caido" not in text
    assert asked == []                                  # asking for the address starts Caido: not while recording is off


async def test_without_caido_on_this_machine_nothing_is_said(conn, monkeypatch):
    asked = a_caido(monkeypatch, 18791)
    monkeypatch.setattr(caido, "available", lambda: False)
    text = await run(conn, 'echo "at [$CAIDO_PROXY]"')
    assert "at []" in text.splitlines() and "Caido" not in text and asked == []


async def test_a_caido_that_cannot_be_reached_is_named_under_the_commands_output(conn, monkeypatch):
    a_caido(monkeypatch, None)
    text = await run(conn, 'echo "at [$CAIDO_PROXY]"; echo after')
    assert text.splitlines()[0] == "exit code: 0" and "at []" in text.splitlines() and "after" in text.splitlines()
    assert text.rstrip().endswith(NOT_REACHED) and text.count("Caido could not be reached") == 1


async def test_a_caido_that_could_not_be_reached_is_not_waited_for_again_at_once(conn, monkeypatch):
    """A Caido that does not come up takes half a minute to say so: not before every command."""
    clock = [1000.0]
    monkeypatch.setattr(caido, "_now", lambda: clock[0])
    asked = a_caido(monkeypatch, None)
    chat, run_id = await a_chat(conn)
    first = await command(conn, chat, run_id, "echo one")
    clock[0] += caido.RETRY_AFTER - 1
    second = await command(conn, chat, run_id, "echo two")
    assert len(asked) == 1 and first.rstrip().endswith(NOT_REACHED) and second.rstrip().endswith(NOT_REACHED)
    clock[0] += 2
    third = await command(conn, chat, run_id, 'echo "at [$CAIDO_PROXY]"')
    assert len(asked) == 2 and third.rstrip().endswith(NOT_REACHED)


async def test_a_caido_that_answers_again_is_used_again(conn, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(caido, "_now", lambda: clock[0])
    a_caido(monkeypatch, None)
    chat, run_id = await a_chat(conn)
    assert (await command(conn, chat, run_id, "echo one")).rstrip().endswith(NOT_REACHED)
    a_caido(monkeypatch, 18791)
    clock[0] += caido.RETRY_AFTER + 1
    text = await command(conn, chat, run_id, 'echo "at $CAIDO_PROXY"')
    assert "at http://127.0.0.1:18791" in text.splitlines() and "Caido" not in text


async def test_a_command_in_the_background_gets_the_address_too(conn, monkeypatch):
    a_caido(monkeypatch, 18791)
    text = await run(conn, 'echo "at $CAIDO_PROXY"', background=True)
    assert "at http://127.0.0.1:18791" in text.splitlines() and "Caido" not in text


async def test_a_command_in_the_background_is_told_too_that_nothing_is_recorded(conn, monkeypatch):
    a_caido(monkeypatch, None)
    chat, run_id = await a_chat(conn)
    ended = await command(conn, chat, run_id, "echo quick", background=True)
    assert ended.splitlines()[0] == "exit code: 0" and ended.rstrip().endswith(NOT_REACHED)
    going = await command(conn, chat, run_id, "sleep 300", background=True)
    assert going.startswith("started background process ") and "read more: process_output(" in going
    assert going.rstrip().endswith(NOT_REACHED)


async def test_a_target_with_a_proxy_of_its_own_is_not_sent_through_caido_instead(conn, monkeypatch):
    """The program asked for that proxy: Caido would send from this machine's own address."""
    site = Site()
    carrier = HttpProxy(site)
    async with running(site, carrier):
        asked = a_caido(monkeypatch, 18791)
        text = await run(conn, f'{PROXY_NAMES}; echo "at [$CAIDO_PROXY]"', proxy=f"http://127.0.0.1:{carrier.port}")
    assert "at []" in text.splitlines() and "CAIDO_PROXY=" not in text and "HTTP_PROXY=http://127.0.0.1:" in text
    assert asked == [] and "Caido" not in text


# ---- the address itself, with a Caido that answers

async def test_the_address_is_that_of_a_running_caido_with_hunterais_filter_as_its_way_out(conn, fake):
    await settings.put(conn, "browser_via_caido", True)
    assert await caido.for_command() == ("http://127.0.0.1:8791", "")
    assert [u["connection"]["port"] for u in fake.upstreams] == [(await caido.egress()).port]


async def test_an_ipv6_address_is_given_the_way_a_program_takes_it(conn, monkeypatch):
    a_caido(monkeypatch, 8791)

    async def address():
        return "::1", 8791
    monkeypatch.setattr(caido, "proxy_address", address)         # HUNTERAI_CAIDO_URL=http://[::1]:8791
    await settings.put(conn, "browser_via_caido", True)
    assert await caido.for_command() == ("http://[::1]:8791", "")


async def test_a_caido_that_is_switched_off_is_not_started_for_a_command(conn, fake, monkeypatch):
    started = []
    monkeypatch.setattr(caido, "_spawn", lambda: started.append(1))
    await settings.put(conn, "browser_via_caido", False)
    fake.up = False
    assert await caido.for_command() == ("", "") and started == [] and fake.calls == []


async def test_a_caido_that_something_else_reached_since_is_used_at_once(conn, fake, monkeypatch):
    """The owner starts it from the Caido panel after reading that it is down: the next command gets the
    address, not the same line for a minute more."""
    monkeypatch.setenv("HUNTERAI_CAIDO_URL", URL)
    await settings.put(conn, "browser_via_caido", True)
    fake.up = False
    assert await caido.for_command() == ("", f"Caido does not answer at {URL}")
    fake.up = True
    assert await caido.for_command() == ("", f"Caido does not answer at {URL}")     # not asked again yet
    assert await caido.proxy_address() == ("127.0.0.1", 8791)                        # the panel, the browser, web_fetch
    assert await caido.for_command() == ("http://127.0.0.1:8791", "")


async def test_the_caido_tool_points_a_command_at_the_variable_while_recording_is_on(conn, fake):
    """Its status used to give the address for a command to use: a command that names it is held for the owner."""
    await settings.put(conn, "browser_via_caido", False)
    off = (await caido_call(conn, "caido", {"action": "status"})).content
    assert "curl -x http://127.0.0.1:8791 -k <url>" in off and "CAIDO_PROXY" not in off
    await settings.put(conn, "browser_via_caido", True)
    on = (await caido_call(conn, "caido", {"action": "status"})).content
    assert 'curl -k -x "$CAIDO_PROXY" <url>' in on and "curl -x http://" not in on
    assert "Proxy: http://127.0.0.1:8791." in on and "Caido 0.59.0 is running at http://127.0.0.1:8791" in on


async def test_the_caido_tool_does_not_promise_the_variable_to_a_chat_that_does_not_get_it(conn, fake):
    chat, run_id = await a_chat(conn, proxy="http://127.0.0.1:18080")
    out, _, _ = await run_call(conn, chat, run_id, ToolCall("c", "caido", {"action": "status"}))
    assert "Requests recorded: 3" in out.block.content and "CAIDO_PROXY" not in out.block.content


# ---- what the model is told

FIRST = ("Recording commands in Caido: a command of the shell is recorded only when it is sent through Caido. Every command "
         "has the variable CAIDO_PROXY, the address of Caido's proxy.")
READ_IT = "Afterwards read what was recorded with caido, filtered by the host."


def test_the_rule_names_the_switch_of_each_program_and_what_cannot_be_recorded():
    text = caido.commands_rule(True, ["execute", "caido"])
    assert text.startswith(FIRST) and text.endswith(READ_IT)
    for must in ('httpx -http-proxy "$CAIDO_PROXY"', 'nuclei -proxy "$CAIDO_PROXY"', 'katana -proxy "$CAIDO_PROXY"',
                 'sqlmap --proxy="$CAIDO_PROXY"', 'curl -k -x "$CAIDO_PROXY"', 'ffuf -replay-proxy "$CAIDO_PROXY"',
                 "as the variable and never as the address itself", "a certificate of its own",
                 "do not put it into HTTP_PROXY or HTTPS_PROXY", "(pip, npm, git, apt)", "(naabu, dnsx, nmap, dig, ssh)",
                 "subfinder asks other services", "on this machine or the local network"):
        assert must in text, must
    assert "8791" not in text and "127.0.0.1" not in text      # the address is the variable's to give, not the prompt's


def test_the_rule_is_for_a_chat_with_the_shell_and_recording_on():
    assert caido.commands_rule(False, ["execute", "caido"]) == ""
    assert caido.commands_rule(True, ["web_fetch", "caido"]) == ""
    assert caido.commands_rule(True, []) == ""


def test_the_rule_sends_the_model_to_the_caido_tool_only_when_it_has_one():
    text = caido.commands_rule(True, ["execute"])
    assert text.startswith(FIRST) and "read what was recorded" not in text and text.endswith("for a command.")


async def told(conn, monkeypatch, recording=True, proxy=None, available=True) -> tuple[str, list]:
    """(the system prompt of a run in a chat with the shell tool, the times Caido was asked for its address)."""
    asked = a_caido(monkeypatch, 18791)
    monkeypatch.setattr(caido, "available", lambda: available)
    prov = Turns("abliteration", [TextDelta("ok")])
    use(abliteration=prov)
    chat, _ = await a_chat(conn, recording, proxy)
    user = await messages.add(conn, chat.id, None, "user", [TextBlock(text="probe the shop")])
    await go(conn, chat, user)
    assert any(t.name == "execute" for t in prov.calls[0]["tools"])
    return prov.calls[0]["system"], asked


async def test_the_model_is_told_how_a_command_gets_recorded(conn, monkeypatch):
    system, asked = await told(conn, monkeypatch)
    assert FIRST in system and 'httpx -http-proxy "$CAIDO_PROXY"' in system
    assert asked == []                                  # the rule is words: Caido is not started to say them


async def test_the_model_is_told_nothing_of_it_while_recording_is_off_or_there_is_no_caido(conn, monkeypatch):
    assert "CAIDO_PROXY" not in (await told(conn, monkeypatch, recording=False))[0]
    assert "CAIDO_PROXY" not in (await told(conn, monkeypatch, available=False))[0]


async def test_the_model_is_told_nothing_of_it_in_a_chat_whose_target_has_a_proxy_of_its_own(conn, monkeypatch):
    system, _ = await told(conn, monkeypatch, proxy="http://127.0.0.1:18080")
    assert "CAIDO_PROXY" not in system and "Proxy: the traffic to this chat's target goes out through the owner's proxy" in system


async def test_a_subagent_that_has_the_shell_is_told_too(conn, world, monkeypatch):
    """It is the sub-agents that run the scans of a swarm."""
    asked = a_caido(monkeypatch, 18791)
    await settings.put(conn, "browser_via_caido", True)
    p = Turns("abliteration", [ToolCall("d1", "delegate", {**BRIEF, "tools": ["execute"]})],
              [TextDelta("Nothing needed running.")], [TextDelta("Done.")])
    use(abliteration=p)
    world.decide_with(WORTH, ACCEPT)
    run, _ = await go(conn, *await world.setup())
    assert run.status == "done", run.error
    sub = p.calls[1]
    assert [t.name for t in sub["tools"]] == ["execute"] and FIRST in sub["system"] and asked == []
    await settings.put(conn, "browser_via_caido", False)
    p = Turns("abliteration", [ToolCall("d1", "delegate", {**BRIEF, "tools": ["execute"]})],
              [TextDelta("Nothing needed running.")], [TextDelta("Done.")])
    use(abliteration=p)
    world.decide_with(WORTH, ACCEPT)
    await go(conn, *await world.setup())
    assert [t.name for t in p.calls[1]["tools"]] == ["execute"] and "CAIDO_PROXY" not in p.calls[1]["system"]
