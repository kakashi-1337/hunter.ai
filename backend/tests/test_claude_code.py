import os
import stat

import pytest

from hunterai.providers import registry
from hunterai.providers.base import Options, ProviderError, Stop, TextDelta, ThinkingDelta, Usage, WireMessage
from hunterai.providers.claude_code import make
from hunterai.types import TextBlock

OK = r'''#!/bin/sh
cat > "$FAKE_OUT/stdin.txt"
for a in "$@"; do printf '%s\n' "$a"; done > "$FAKE_OUT/args.txt"
pwd > "$FAKE_OUT/cwd.txt"
env > "$FAKE_OUT/env.txt"
echo '{"type":"system","subtype":"init","tools":[],"apiKeySource":"none"}'
echo 'not json at all'
echo '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"mulling"}}}'
echo '{"type":"stream_event","event":{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"Hel"}}}'
echo '{"type":"stream_event","event":{"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"lo"}}}'
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"Hello"}]}}'
echo '{"type":"result","subtype":"success","is_error":false,"result":"Hello","usage":{"input_tokens":2,"cache_creation_input_tokens":640,"cache_read_input_tokens":8,"output_tokens":4}}'
'''
WHOLE = r'''#!/bin/sh
cat >/dev/null
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"All at once"}]}}'
echo '{"type":"result","subtype":"success","is_error":false,"result":"All at once","usage":{"input_tokens":3,"output_tokens":5}}'
'''
LIMIT = r'''#!/bin/sh
cat >/dev/null
echo '{"type":"result","subtype":"success","is_error":true,"result":"Claude usage limit reached. Your limit will reset at 5pm."}'
exit 1
'''
LOGIN = '#!/bin/sh\ncat >/dev/null\necho "Not logged in. Please run /login" >&2\nexit 1\n'
SLOW = '#!/bin/sh\ncat >/dev/null\necho $$ > "$FAKE_OUT/pid.txt"\n' \
       'echo \'{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"one"}}}\'\nsleep 30\n'


@pytest.fixture
def fake(tmp_path, monkeypatch):
    def install(script):
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        exe = bindir / "claude"
        exe.write_text(script)
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
        monkeypatch.setenv("FAKE_OUT", str(tmp_path))
        return tmp_path
    return install


def msgs():
    return [WireMessage("user", [TextBlock(text="first")]), WireMessage("assistant", [TextBlock(text="reply")]),
            WireMessage("user", [TextBlock(text="second")])]


async def run(options=None, system="be brief", model="haiku"):
    return [e async for e in make().stream(model, system, msgs(), options or Options())]


async def test_the_cli_answer_is_streamed_with_its_usage(fake):
    fake(OK)
    assert await run() == [ThinkingDelta("mulling"), TextDelta("Hel"), TextDelta("lo"), Usage(650, 4), Stop("end")]
    fake(WHOLE)                                   # a build that sends no partial pieces: the whole message is taken once
    assert await run() == [TextDelta("All at once"), Usage(3, 5), Stop("end")]


async def test_the_cli_gets_no_tools_no_settings_and_none_of_our_keys(fake, monkeypatch):
    out = fake(OK)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-would-be-billed")
    monkeypatch.setenv("HUNTERAI_SECRET", "x")
    await run(Options(effort="high"), model="sonnet")
    args = (out / "args.txt").read_text().split("\n")
    assert args[0] == "-p" and args[args.index("--output-format") + 1] == "stream-json"
    assert args[args.index("--model") + 1] == "sonnet" and args[args.index("--effort") + 1] == "high"
    assert args[args.index("--system-prompt") + 1] == "be brief"
    assert args[args.index("--tools") + 1] == "" and args[args.index("--setting-sources") + 1] == ""
    for flag in ("--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
        assert flag in args
    stdin = (out / "stdin.txt").read_text()
    assert "be brief" not in stdin and "first" in stdin and "reply" in stdin and stdin.rstrip().endswith("second")
    env = (out / "env.txt").read_text()
    assert "ANTHROPIC_API_KEY" not in env and "HUNTERAI_SECRET" not in env and "HOME=" in env
    cwd = (out / "cwd.txt").read_text().strip()
    assert cwd != os.getcwd() and not os.path.exists(cwd)          # an empty folder, removed afterwards


async def test_a_system_prompt_too_long_for_an_argument_goes_with_the_conversation(fake):
    out = fake(OK)
    await run(system="rule " * 30000)
    args = (out / "args.txt").read_text().split("\n")
    assert "--system-prompt" not in args and (out / "stdin.txt").read_text().startswith("rule rule")


async def test_failures_say_what_to_do(fake, monkeypatch):
    fake(LIMIT)
    with pytest.raises(ProviderError) as e:
        await run()
    assert e.value.kind == "rate_limit" and "usage limit reached" in e.value.message
    fake(LOGIN)
    with pytest.raises(ProviderError) as e:
        await run()
    assert e.value.kind == "auth" and "claude" in e.value.message and "log in" in e.value.message.lower()
    monkeypatch.setenv("PATH", "/nonexistent")
    assert make().configured() is False
    with pytest.raises(ProviderError) as e:
        await run()
    assert e.value.kind == "unavailable"


async def test_stopping_kills_the_cli(fake):
    out = fake(SLOW)
    gen = make().stream("haiku", "s", msgs(), Options())
    assert await gen.__anext__() == TextDelta("one")
    await gen.aclose()
    pid = int((out / "pid.txt").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_it_is_listed_with_models_and_has_no_tools(fake):
    fake(OK)
    p = registry.get("claude_code")
    assert p.configured() and not getattr(p, "supports_tools", False)
    assert [m.id for m in await p.models()] == ["haiku", "sonnet", "opus"]
