import asyncio

from hunterai import db, engine
from hunterai.providers.base import TextDelta, ToolCall
from hunterai.repo import queue, runs, settings
from hunterai.tools import registry
from tests.test_api import client, new_chat  # noqa: F401 (client is a fixture)
from tests.test_engine import go, setup, use  # noqa: F401 (setup is a fixture)
from tests.toolkit import Turns, fake_tool


async def test_a_message_sent_while_the_agent_works_reaches_it_at_its_next_step(conn, setup):
    """Not an interruption: the tool that is running finishes, and the next model call reads the message."""
    gate = asyncio.Event()

    async def slow(ctx, args):
        await gate.wait()
        from hunterai.tools.base import ToolResult
        return ToolResult("scan finished: 3 hosts")
    tool, _ = fake_tool("t_scan")
    import dataclasses
    registry.register(dataclasses.replace(tool, handler=slow))
    p = Turns("abliteration", [ToolCall("c1", "t_scan", {})], [TextDelta("Skipping host b as you asked.")])
    use(abliteration=p)
    chat, user = await setup()
    task = asyncio.create_task(go(conn, chat, user))
    for _ in range(200):                                   # until the tool is running
        if len(p.calls) == 1:
            break
        await asyncio.sleep(0.01)
    await queue.add(conn, chat.id, "skip host b, it is out of scope")
    await queue.add(conn, chat.id, "and keep it quiet")
    gate.set()
    run, msg = await task
    assert run.status == "done" and await queue.list(conn, chat.id) == []
    # The second model call got both, in order, inside the marked block the system prompt announces.
    nonce = await queue.nonce(conn, chat.id)
    sent = p.calls[1]["messages"][-1].blocks[-1].content
    assert sent.startswith("scan finished: 3 hosts") and f"<<<OWNER_MESSAGE {nonce}>>>" in sent
    assert sent.index("skip host b, it is out of scope") < sent.index("and keep it quiet") < sent.index(f"<<<END_OWNER_MESSAGE {nonce}>>>")
    assert f"<<<OWNER_MESSAGE {nonce}>>>" in p.calls[0]["system"] and "from the owner" in p.calls[0]["system"]
    # Reported: a model read such a message beside a goal record that said something else, and weighed the two.
    assert "their latest message. It overrides earlier messages, summaries, notes and the goal record" in p.calls[0]["system"]
    assert len(queue.rule(nonce)) <= 414          # what it was before it said so: a local model's window has no room to spare
    note = [s for s in run.steps if s.kind == "user_note"]
    assert len(note) == 1 and note[0].status == "done" and "skip host b" in note[0].detail["text"]
    assert msg.blocks[-1].text == "Skipping host b as you asked."
    # In the transcript the message stands where it was read; the model was not sent that block a second time.
    assert [b.type for b in msg.blocks] == ["tool_call", "tool_result", "owner_note", "text"]
    assert msg.blocks[2].text == "skip host b, it is out of scope\n\nand keep it quiet"
    assert [b.type for b in p.calls[1]["messages"][-1].blocks] == ["tool_call", "tool_result"]


async def test_the_code_is_the_same_for_a_chat_and_differs_between_chats(conn, setup):
    a, _ = await setup()
    b, _ = await setup()
    assert await queue.nonce(conn, a.id) == await queue.nonce(conn, a.id) != await queue.nonce(conn, b.id)
    assert len(await queue.nonce(conn, a.id)) == 12 and await settings.get(conn, "note_key")


async def test_the_queue_routes(client):
    chat = await new_chat(client)
    cid = chat["id"]
    assert (await client.get(f"/api/chats/{cid}/queue")).json() == []
    # Nothing is running: there is nothing to queue behind. The interface sends it as a normal message instead.
    r = await client.post(f"/api/chats/{cid}/queue", json={"text": "hello"})
    assert r.status_code == 409
    conn = await db.connect()
    try:
        from hunterai.repo import messages
        from hunterai.types import TextBlock
        u = await messages.add(conn, cid, None, "user", [TextBlock(text="hi")])
        a = await messages.add(conn, cid, u.id, "assistant", [], status="streaming")
        run = await runs.create(conn, cid, u.id, a.id)                       # a run in progress
        one = (await client.post(f"/api/chats/{cid}/queue", json={"text": "  first  "})).json()
        two = (await client.post(f"/api/chats/{cid}/queue", json={"text": "second"})).json()
        assert one["text"] == "first" and [q["text"] for q in (await client.get(f"/api/chats/{cid}/queue")).json()] == ["first", "second"]
        assert (await client.post(f"/api/chats/{cid}/queue", json={"text": "   "})).status_code == 422
        assert (await client.delete(f"/api/chats/{cid}/queue/{one['id']}")).status_code == 204
        assert (await client.delete(f"/api/chats/{cid}/queue/{one['id']}")).status_code == 404
        # The run ended without reading them: the interface takes what is left and sends it as the next message.
        await runs.finish(conn, run.id, "done")
        left = (await client.post(f"/api/chats/{cid}/queue/take")).json()
        assert [q["text"] for q in left] == ["second"] and (await client.get(f"/api/chats/{cid}/queue")).json() == [two][:0]
        assert (await client.get("/api/chats/nope/queue")).status_code == 404
    finally:
        await conn.close()
