from hunterai.modes import build_system
from hunterai.providers.base import ToolCall
from hunterai.repo import chats, kb, messages, personas
from hunterai.tools import registry, runner
from hunterai.types import TextBlock
from tests.kb_samples import make_collection
from tests.test_api import client, new_chat  # noqa: F401
from tests.test_engine import go, use
from tests.toolkit import fake_tool


async def test_builtin_personas_are_seeded_once_and_stay_deleted(conn):
    first = await personas.list(conn)
    assert [p.name for p in first][:2] == ["Binary and reverse engineering", "Coding"] and len(first) == 6 and all(p.builtin for p in first)
    assert {p.default_mode for p in first} == {"pentesting", "coding", None}
    await personas.delete(conn, first[0].id)
    assert len(await personas.list(conn)) == 5


def test_persona_prompt_comes_first_and_the_mode_still_applies():
    p = personas.Persona(id="p", name="RE", system_prompt="You are a reverse engineer.", created_at="t", updated_at="t")
    system = build_system("plan", None, ["read"], persona=p)
    assert system.startswith("You are a reverse engineer.\n\nCurrent mode: plan. You are a planning assistant")
    assert "You have tools." in system and build_system("plan", None, ["read"]) in system.replace("You are a reverse engineer.\n\nCurrent mode: plan. ", "")


async def test_crud_and_validation(client, conn):
    listed = (await client.get("/api/personas")).json()
    assert len(listed) == 6
    made = (await client.post("/api/personas", json={"name": " Recon ", "system_prompt": " You map attack surface. ", "default_mode": "plan",
                                                     "tools": ["read", "web_fetch", "read"], "provider": "ollama", "model": "qwen3:8b"})).json()
    assert (made["name"], made["system_prompt"], made["tools"], made["builtin"]) == ("Recon", "You map attack surface.", ["read", "web_fetch"], False)
    assert (await client.post("/api/personas", json={"name": "recon", "system_prompt": "x"})).status_code == 409
    assert (await client.post("/api/personas", json={"name": "x", "system_prompt": " "})).status_code == 400
    assert (await client.post("/api/personas", json={"name": "x", "system_prompt": "y", "default_mode": "god"})).status_code == 422
    assert (await client.post("/api/personas", json={"name": "x", "system_prompt": "y", "collections": ["nope"]})).status_code == 404
    changed = (await client.patch(f"/api/personas/{made['id']}", json={"description": "maps things", "provider": "", "model": ""})).json()
    assert (changed["description"], changed["provider"], changed["model"], changed["name"]) == ("maps things", None, None, "Recon")
    assert (await client.patch("/api/personas/nope", json={"name": "y"})).status_code == 404
    assert "read" in (await client.get("/api/personas/tools")).json()
    chat = await new_chat(client, persona_id=made["id"])
    assert chat["persona_id"] == made["id"]
    assert (await client.delete(f"/api/personas/{made['id']}")).status_code == 204
    assert (await client.get(f"/api/chats/{chat['id']}")).json()["chat"]["persona_id"] is None        # the chat survives its persona


async def test_a_chat_takes_the_personas_model_and_collections(client, conn):
    col = await make_collection(conn, "RE notes")
    p = (await client.post("/api/personas", json={"name": "RE", "system_prompt": "x", "provider": "ollama", "model": "qwen3:8b", "collections": [col.id]})).json()
    r = await client.post("/api/chats", json={"persona_id": p["id"]})
    chat = r.json()
    assert (chat["provider"], chat["model"], chat["persona_id"]) == ("ollama", "qwen3:8b", p["id"])
    assert (await kb.get_chat_kb(conn, chat["id"])).collection_ids == [col.id]
    explicit = (await client.post("/api/chats", json={"persona_id": p["id"], "provider": "mock", "model": "mock-1"})).json()
    assert (explicit["provider"], explicit["model"]) == ("mock", "mock-1")                              # what the owner picked wins
    other = await new_chat(client)
    assert (await client.patch(f"/api/chats/{other['id']}", json={"persona_id": p["id"]})).json()["persona_id"] == p["id"]
    assert (await kb.get_chat_kb(conn, other["id"])).collection_ids == [col.id]
    assert (await client.patch(f"/api/chats/{other['id']}", json={"persona_id": None})).json()["persona_id"] is None
    assert (await client.patch(f"/api/chats/{other['id']}", json={"persona_id": "nope"})).status_code == 404
    assert (await client.post("/api/chats", json={"persona_id": "nope"})).status_code == 404


async def test_the_run_uses_the_persona_prompt_and_its_tool_list(conn):
    for name in ("t_a", "t_b"):
        registry.register(fake_tool(name)[0])
    p = await personas.create(conn, name="Narrow", system_prompt="You are narrow.", tools=["t_a", "execute"])
    chat = await chats.create(conn, None, "pentesting", "abliteration", "m", {}, p.id)
    user = await messages.add(conn, chat.id, None, "user", [TextBlock(text="hi")])
    from hunterai.providers.base import TextDelta
    from tests.toolkit import Turns
    prov = Turns("abliteration", [TextDelta("ok")])
    use(abliteration=prov)
    await go(conn, chat, user)
    call = prov.calls[0]
    assert call["system"].startswith("You are narrow.\n\nCurrent mode: pentesting.")
    assert [t.name for t in await runner.offered_tools(conn, chat)] == ["t_a"]           # only what both the persona and the registry have
    out = await runner.call(conn, chat_id=chat.id, run_id=(await _run(conn, chat, user)), tc=ToolCall("c1", "t_b", {}),
                            offered={t.name for t in await runner.offered_tools(conn, chat)}, publish=_quiet)
    assert out.invalid and "Unknown tool" in out.block.content


async def _run(conn, chat, user):
    from hunterai.repo import runs
    reply = await messages.add(conn, chat.id, user.id, "assistant", [], status="streaming")
    return (await runs.create(conn, chat.id, user.id, reply.id)).id


async def _quiet(event):
    return None
