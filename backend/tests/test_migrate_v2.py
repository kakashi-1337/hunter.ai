import json
from pathlib import Path

from hunterai import config, db
from hunterai.repo import chats
from hunterai.types import BlockList, ThinkingBlock, ToolCallBlock

V1 = (Path(__file__).parent / "data" / "schema_v1.sql").read_text()


async def test_v1_database_gains_columns_and_keeps_rows(isolated_home):
    conn = await db.connect()
    await conn.executescript(V1)
    await conn.execute("pragma user_version=1")
    await conn.execute("insert into chats(id,title,mode,provider,model,created_at,updated_at)"
                       " values('c1','t','plan','mock','m','x','x')")
    await conn.commit()
    await db.migrate(conn)
    await db.migrate(conn)          # idempotent
    chat = await chats.get(conn, "c1")
    assert (chat.workdir, chat.permissions) == (None, {})
    assert (await (await conn.execute("pragma user_version")).fetchone())[0] == db.VERSION >= 2
    names = {r["name"] for r in await (await conn.execute("select name from sqlite_master")).fetchall()}
    assert {"chat_grants", "processes"} <= names
    await conn.close()


async def test_workdir_default_and_override(conn, tmp_path):
    chat = await chats.create(conn, None, "coding", "mock", "m", {})
    assert chats.resolve_workdir(chat) == config.home() / "work" / chat.id
    chat = await chats.update(conn, chat.id, workdir=str(tmp_path),
                              permissions={"risk": {"network": "allow"}})
    assert chats.resolve_workdir(chat) == tmp_path
    assert chat.permissions == {"risk": {"network": "allow"}}


def test_blocks_round_trip_opaque():
    b = BlockList.validate_json(BlockList.dump_json(
        [ThinkingBlock(text="t", opaque={"provider": "anthropic"})]))
    assert b[0].opaque == {"provider": "anthropic"}
    assert BlockList.validate_json('[{"type":"thinking","text":"old"}]')[0].opaque is None
    assert ThinkingBlock(text="t").model_dump() == {"type": "thinking", "text": "t"}
    assert ToolCallBlock(id="c", name="n", arguments={}).model_dump() == {
        "type": "tool_call", "id": "c", "name": "n", "arguments": {}}
    dumped = BlockList.dump_json([ToolCallBlock(id="c", name="n", arguments={},
                                                opaque={"provider": "google"})])
    assert json.loads(dumped)[0]["opaque"] == {"provider": "google"}


def test_port_origins_and_mcp_switch(monkeypatch):
    assert config.port() == 8790 and config.mcp_enabled() is False   # fixture sets HUNTERAI_MCP=off
    assert config.allowed_origins() == {"http://127.0.0.1:8790", "http://localhost:8790"}
    monkeypatch.setenv("HUNTERAI_DEV", "1")
    assert config.allowed_origins() == {"http://127.0.0.1:8790", "http://localhost:8790",
                                        "http://127.0.0.1:5173", "http://localhost:5173"}
    monkeypatch.setenv("HUNTERAI_PORT", "9001")
    monkeypatch.delenv("HUNTERAI_MCP")
    assert config.port() == 9001 and config.mcp_enabled() is True
    monkeypatch.setenv("HUNTERAI_PORT", "abc")
    assert config.port() == 8790


def test_project_root_can_point_at_the_owners_checkout(tmp_path):
    """A deployed copy of the app still protects the real project folder and reads its .env."""
    import os
    import subprocess
    import sys
    code = "from hunterai import config; print(config.PROJECT_ROOT); print(config.env_file())"
    env = {**os.environ, "HUNTERAI_PROJECT": str(tmp_path)}
    env.pop("HUNTERAI_ENV_FILE", None)
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                         cwd=str(config.PROJECT_ROOT / "backend")).stdout.split()
    assert out == [str(tmp_path), str(tmp_path / ".env")]
