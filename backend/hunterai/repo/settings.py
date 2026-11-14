from __future__ import annotations

import json


async def get(conn, key: str, default=None):
    r = await (await conn.execute("select value_json from settings where key=?",
                                  (key,))).fetchone()
    return json.loads(r["value_json"]) if r else default


async def put(conn, key: str, value) -> None:
    await conn.execute(
        "insert into settings(key,value_json) values(?,?)"
        " on conflict(key) do update set value_json=excluded.value_json",
        (key, json.dumps(value)))
    await conn.commit()
