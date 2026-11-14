from hunterai import db
from hunterai.repo import findings, goals
from tests.test_api import client, new_chat  # noqa: F401 (client is a fixture)


async def test_findings_and_goal_routes(client):
    target = (await client.post("/api/targets", json={"name": "acme"})).json()
    chat = await new_chat(client, target_id=target["id"])
    conn = await db.connect()
    try:
        f = await findings.add(conn, target["id"], "IDOR", "high", "evidence", chat_id=chat["id"],
                               triage={"false_positive": 0.1})
        await goals.put(conn, chat["id"], "Find an IDOR", ["one"], ["two"])
    finally:
        await conn.close()
    listed = (await client.get(f"/api/targets/{target['id']}/findings")).json()
    assert [(x["title"], x["severity"], x["status"], x["triage"]) for x in listed] == [("IDOR", "high", "open", {"false_positive": 0.1})]
    r = await client.patch(f"/api/findings/{f.id}", json={"status": "confirmed", "severity": "critical"})
    assert (r.json()["status"], r.json()["severity"]) == ("confirmed", "critical")
    assert (await client.patch(f"/api/findings/{f.id}", json={"status": "nope"})).status_code == 422
    assert (await client.patch("/api/findings/nope", json={"status": "fixed"})).status_code == 404
    goal = (await client.get(f"/api/chats/{chat['id']}/goal")).json()
    assert (goal["objective"], goal["criteria"], goal["open_items"]) == ("Find an IDOR", ["one"], ["two"])
    assert (await client.delete(f"/api/chats/{chat['id']}/goal")).status_code == 204
    assert (await client.get(f"/api/chats/{chat['id']}/goal")).json() is None
    assert (await client.delete(f"/api/findings/{f.id}")).status_code == 204
    assert (await client.get(f"/api/targets/{target['id']}/findings")).json() == []
