import asyncio
import os
import stat

import pytest

from hunterai.providers.base import (Options, ProviderError, Stop, TextDelta, ThinkingDelta,
                                     Usage, WireMessage)
from hunterai.providers.codex import flatten_conversation, make
from hunterai.types import TextBlock

OK = r'''#!/bin/sh
cat > "$FAKE_OUT/stdin.txt"
echo "$@" > "$FAKE_OUT/args.txt"
pwd > "$FAKE_OUT/cwd.txt"
echo '{"type":"thread.started","thread_id":"t"}'
echo 'not json at all'
echo '{"type":"item.completed","item":{"type":"reasoning","text":"mulling"}}'
echo '{"type":"item.completed","item":{"type":"agent_message","text":"Hello"}}'
echo '{"type":"item.completed","item":{"type":"command_execution","command":"ls"}}'
echo '{"type":"turn.completed","usage":{"input_tokens":20,"cached_input_tokens":5,"output_tokens":9}}'
'''
FAIL = '#!/bin/sh\ncat >/dev/null\necho "not logged in" >&2\nexit 3\n'
SLOW = '#!/bin/sh\ncat >/dev/null\necho $$ > "$FAKE_OUT/pid.txt"\n' \
       'echo \'{"type":"item.completed","item":{"type":"agent_message","text":"one"}}\'\n' \
       'sleep 30\n'
TURN_FAILED = '#!/bin/sh\ncat >/dev/null\n' \
              'echo \'{"type":"turn.failed","error":{"message":"usage limit reached"}}\'\nexit 1\n'


@pytest.fixture
def fake(tmp_path, monkeypatch):
    def install(script):
        bindir = tmp_path / "bin"
        bindir.mkdir(exist_ok=True)
        exe = bindir / "codex"
        exe.write_text(script)
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        monkeypatch.setenv("PATH", f"{bindir}:/usr/bin:/bin")
        monkeypatch.setenv("FAKE_OUT", str(tmp_path))
        return tmp_path
    return install


def msgs():
    return [WireMessage("user", [TextBlock(text="first")]),
            WireMessage("assistant", [TextBlock(text="reply")]),
            WireMessage("user", [TextBlock(text="second")])]


async def run(options=None, system="be brief"):
    return [e async for e in make().stream("codex-default", system, msgs(), options or Options())]


async def test_events_mapped(fake):
    fake(OK)
    assert await run() == [ThinkingDelta("mulling"), TextDelta("Hello"), Usage(20, 9),
                           Stop("end")]


async def test_invocation_is_sandboxed_and_reads_stdin(fake):
    out = fake(OK)
    await run(Options(effort="high"))
    args = (out / "args.txt").read_text().split()
    assert args[0] == "exec" and "--json" in args and args[-1] == "-"
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert "--skip-git-repo-check" in args
    assert 'model_reasoning_effort="high"' in " ".join(args)
    stdin = (out / "stdin.txt").read_text()
    assert "be brief" in stdin and "first" in stdin and "reply" in stdin
    assert stdin.rstrip().endswith("second")
    cwd = (out / "cwd.txt").read_text().strip()
    assert cwd != os.getcwd() and not os.path.exists(cwd)  # temp dir, removed afterwards


async def test_effort_flag_absent_by_default(fake):
    out = fake(OK)
    await run()
    assert "model_reasoning_effort" not in (out / "args.txt").read_text()


def test_configured_requires_executable(fake, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert make().configured() is False
    fake(OK)
    assert make().configured() is True and make().name == "codex"


async def test_missing_executable_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(ProviderError) as ei:
        await run()
    assert ei.value.kind == "unavailable"


async def test_nonzero_exit_before_output_is_unavailable(fake):
    fake(FAIL)
    with pytest.raises(ProviderError) as ei:
        await run()
    assert ei.value.kind == "unavailable" and "not logged in" in ei.value.message


async def test_turn_failed_event_raises_with_message(fake):
    fake(TURN_FAILED)
    with pytest.raises(ProviderError) as ei:
        await run()
    assert "usage limit reached" in ei.value.message


async def test_abandoning_the_stream_kills_the_child(fake):
    out = fake(SLOW)
    agen = make().stream("codex-default", "", msgs(), Options())
    assert await agen.__anext__() == TextDelta("one")
    pid = int((out / "pid.txt").read_text())
    await agen.aclose()
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail("codex child still running after the stream was closed")


async def test_abandoning_the_stream_returns_promptly(fake):
    fake(SLOW)
    agen = make().stream("codex-default", "", msgs(), Options())
    await agen.__anext__()
    await asyncio.wait_for(agen.aclose(), timeout=3)


async def test_models_single_entry():
    ms = await make().models()
    assert [(m.id, m.options, m.supports_images) for m in ms] == [("codex-default", ["effort"], False)]


def test_flatten_conversation_labels_roles():
    text = flatten_conversation("sys", msgs())
    assert text.index("sys") < text.index("first") < text.index("reply") < text.index("second")
    assert "Assistant:" in text and "User:" in text
