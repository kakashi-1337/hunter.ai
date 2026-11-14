import pytest

from hunterai.kb import tools
from tests.kb_samples import HANG, assert_dead


async def test_timeout_kills_the_whole_process_group(fake_bin, tmp_path):                # Review Focus 3, runner half
    out = fake_bin("slowtool", HANG)
    with pytest.raises(tools.ToolTimeout):
        await tools.run_tool(["slowtool"], timeout=0.3, cwd=tmp_path)
    for name in ("main.pid", "child.pid"):
        assert_dead(int((out / name).read_text()))


async def test_arguments_are_never_shell_parsed(fake_bin, tmp_path):
    fake_bin("echoer", '#!/bin/sh\nprintf "%s" "$1"\n')
    res = await tools.run_tool(["echoer", "a; touch pwned $(id)"], timeout=5, cwd=tmp_path)
    assert res.stdout == b"a; touch pwned $(id)" and res.returncode == 0 and not (tmp_path / "pwned").exists()


async def test_growth_limit_kills_the_tool(fake_bin, tmp_path):
    out = fake_bin("grower", '#!/bin/sh\necho $$ > "$FAKE_OUT/main.pid"\nwhile true; do head -c 200000 /dev/zero >> "$1/f"; sleep 0.05; done\n')
    grow = tmp_path / "grow"; grow.mkdir()
    with pytest.raises(tools.ToolLimit):
        await tools.run_tool(["grower", str(grow)], timeout=20, cwd=tmp_path, grow_dir=grow, grow_limit=500_000)
    assert_dead(int((out / "main.pid").read_text()))


async def test_missing_tool(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(tools.ToolMissing):
        await tools.run_tool(["7z", "l"], timeout=1, cwd=tmp_path)
