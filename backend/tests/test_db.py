from hunterai import config, db

TABLES = {"targets", "findings", "chats", "messages", "messages_fts", "runs", "steps",
          "uploads", "settings", "jobs", "kb_collections", "kb_documents", "kb_chunks", "kb_chunks_fts",
          "kb_job_items", "chat_kb", "personas", "memories", "memory_state", "chat_kb_collections", "kb_vector_collections", "kb_vectors"}


async def test_migrate_is_idempotent(conn):
    await db.migrate(conn)
    await db.migrate(conn)
    rows = await (await conn.execute("select name from sqlite_master")).fetchall()
    assert TABLES <= {r["name"] for r in rows}
    assert (await (await conn.execute("pragma user_version")).fetchone())[0] == db.VERSION


async def test_deleting_chat_removes_its_messages(conn):
    await conn.execute(
        "insert into chats(id,title,mode,provider,model,options_json,created_at,updated_at)"
        " values('c','t','plan','mock','m','{}','n','n')")
    await conn.execute(
        "insert into messages(id,chat_id,role,blocks_json,status,created_at)"
        " values('m','c','user','[]','complete','n')")
    await conn.execute("delete from chats where id='c'")
    assert (await (await conn.execute("select count(*) c from messages")).fetchone())["c"] == 0


async def test_connection_uses_wal(conn):
    assert (await (await conn.execute("pragma journal_mode")).fetchone())[0] == "wal"


def test_env_reads_file(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("# c\nABLIT_API_KEY=ak_test\nQUOTED=\"q v\"\n\nBROKEN LINE\n")
    monkeypatch.setenv("HUNTERAI_ENV_FILE", str(f))
    assert config.env("ABLIT_API_KEY") == "ak_test"
    assert config.env("QUOTED") == "q v"
    assert config.env("NOPE") is None


def test_env_does_not_export_file_into_process(tmp_path, monkeypatch):
    import os
    f = tmp_path / ".env"
    f.write_text("OTHERTOOL_LLM=x\n")
    monkeypatch.setenv("HUNTERAI_ENV_FILE", str(f))
    config.env("OTHERTOOL_LLM")
    assert "OTHERTOOL_LLM" not in os.environ


def test_process_env_wins(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("OLLAMA_HOST=from_file\n")
    monkeypatch.setenv("HUNTERAI_ENV_FILE", str(f))
    monkeypatch.setenv("OLLAMA_HOST", "from_proc")
    assert config.env("OLLAMA_HOST") == "from_proc"


def test_home_is_created_and_overridable(isolated_home):
    assert config.home() == isolated_home and isolated_home.is_dir()
    assert config.db_path().parent == isolated_home
    assert config.uploads_dir().is_dir()
