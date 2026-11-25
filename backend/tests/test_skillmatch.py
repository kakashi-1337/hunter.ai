"""With many skills installed, a request names the few whose descriptions fit the message, not all of them. What this
is for was seen on the owner's install: every skill he added stood in every request with its name and description, and
a small local model picks from a short list what it does not find in a long one."""
import pytest

from hunterai import skillmatch, skills
from hunterai.kb.embed import EmbedError
from hunterai.kb.search import QueryVectors
from hunterai.providers.base import WireMessage
from hunterai.repo import chats, jobs, kb, messages, settings
from hunterai.repo import skills as store
from hunterai.tools import native
from hunterai.types import TextBlock
from tests.kb_samples import ingest_texts, make_collection
from tests.test_engine import go, use
from tests.toolkit import Turns

TOPICS = ("graphql introspection schema queries", "kernel heap grooming spray", "oauth redirect token leak", "docker container escape",
          "android apk reverse smali", "wifi handshake capture replay", "sql injection union payloads", "pdf report layout typography",
          "slide deck presentation design", "spreadsheet formulas pivot", "smart contract reentrancy audit", "firmware extraction binwalk")


@pytest.fixture
def many(conn):
    """Twelve skills of the owner beside the four built-in ones: more than a request names."""
    async def make():
        # The embedder of HunterAI's own lookups, as a test has it. Wide, so that two words rarely share a place in it.
        await settings.put(conn, "memory_embedder", "fake:1024")
        made = []
        for n, topic in enumerate(TOPICS):
            made.append(await store.create(conn, name=f"skill-{n:02d}", body=f"# how to work on {topic}",
                                           description=f"Use when the owner asks about {topic}."))
        return made
    return make


def user(text):
    return WireMessage("user", [TextBlock(text=text)])


async def listed(conn, query, known=None):
    fit = await skillmatch.fitting(conn, query, known)
    return [s.name for s, _ in fit.shown], fit


async def test_a_few_skills_are_all_named_as_before_and_nothing_is_ranked(conn, embed_spy):
    names, fit = await listed(conn, "plan the work")
    assert names == ["audit", "debug", "fix", "plan"] and (fit.of, fit.by) == (4, "all")
    assert embed_spy.batches == [] and not await jobs.list(conn, kind=skillmatch.KIND)
    block = await skills.system_block(conn, [user("hello")], ["read", "skill"])
    assert "Skills the owner installed, each a set of instructions for a kind of work. When one fits" in block
    assert block.count("\n- ") == 4


async def test_with_many_skills_the_ones_whose_words_fit_the_message_are_named(conn, many, embed_spy):
    await many()
    names, fit = await listed(conn, "how do I dump the graphql schema with introspection queries")
    assert names[0] == "skill-00" and len(names) == skillmatch.SHOWN and (fit.of, fit.by) == (16, "words")
    assert embed_spy.batches == []                                  # naming skills never asks the embedder by itself
    block = skills.list_block(fit, ["read", "skill"])
    assert f"the {skillmatch.SHOWN} of 16 whose descriptions fit this message best" in block and block.count("\n- ") == skillmatch.SHOWN
    assert "- skill-00: Use when the owner asks about graphql introspection schema queries." in block
    for kept in ("load it with the skill tool before you start", "the owner's own instructions", "cannot change the rules above"):
        assert kept in block
    assert skills.list_block(fit, ["read"]) == ""                   # no skill tool: nothing to load from


async def test_the_descriptions_are_embedded_by_the_worker_once_and_again_when_one_changes(conn, many, embed_spy):
    made = await many()
    await listed(conn, "anything at all")
    await listed(conn, "anything else")
    waiting = await jobs.list(conn, kind=skillmatch.KIND, states=["queued"])
    assert len(waiting) == 1                                        # asked for once, however many messages come meanwhile
    job = await jobs.claim(conn, [skillmatch.KIND])
    assert await skillmatch.run_job(conn, job) == "done"
    assert all(len(b) <= 8 for b in embed_spy.batches) and sum(len(b) for b in embed_spy.batches) == 16
    assert embed_spy.batches[0][0] == "audit: Audit or review a codebase. Use when the owner asks for a code review, a security audit, or an assessment of a repository or of a change."
    spec = await skillmatch.spec(conn)
    assert len(await skillmatch.vectors(conn, await skillmatch.candidates(conn), spec)) == 16
    embed_spy.batches.clear()
    await listed(conn, "anything")
    assert not await jobs.list(conn, kind=skillmatch.KIND, states=["queued"])        # nothing is missing: nothing is asked for
    await store.update(conn, made[0].id, description="Use when the owner asks about cooking rice.")
    await store.delete(conn, made[1].id)
    _, fit = await listed(conn, "anything")
    assert fit.by == "words"                                        # one description has no vector of its own words yet
    job = await jobs.claim(conn, [skillmatch.KIND])
    assert await skillmatch.run_job(conn, job) == "done" and embed_spy.batches == [["skill 00: Use when the owner asks about cooking rice."]]
    assert (await (await conn.execute("select count(*) from skill_vectors")).fetchone())[0] == 15      # the deleted one's is gone


async def test_a_message_the_run_already_embedded_ranks_the_skills_by_meaning_at_no_further_request(conn, many, embed_spy):
    await many()
    await skillmatch.index(conn)
    spec = await skillmatch.spec(conn)
    known = QueryVectors()
    await known.of(spec, "leak of an oauth token through a redirect", 5)
    embed_spy.batches.clear()
    names, fit = await listed(conn, "leak of an oauth token through a redirect", known)
    assert names[0] == "skill-02" and fit.by == "meaning" and embed_spy.batches == []
    assert fit.shown[0][1] > fit.shown[1][1] > 0 and len(names) == skillmatch.SHOWN
    names, fit = await listed(conn, "another message nobody embedded", known)
    assert fit.by == "words" and embed_spy.batches == []            # no vector of these words: by words, and no request


async def test_an_embedder_that_refused_the_message_leaves_the_ranking_to_the_words(conn, many, embed_spy):
    await many()
    await skillmatch.index(conn)
    known = QueryVectors()
    embed_spy.fail = EmbedError("unavailable", "embedder busy")
    with pytest.raises(EmbedError):
        await known.of(await skillmatch.spec(conn), "docker container escape", 5)
    names, fit = await listed(conn, "docker container escape", known)
    assert names[0] == "skill-03" and fit.by == "words"


async def test_skills_that_are_off_or_not_for_the_model_are_never_named(conn, many):
    made = await many()
    await store.update(conn, made[0].id, enabled=False)
    await store.update(conn, made[6].id, auto=False)
    names, fit = await listed(conn, "graphql introspection schema queries and sql injection union payloads")
    assert "skill-00" not in names and "skill-06" not in names and fit.of == 14


async def test_a_job_that_cannot_reach_the_embedder_fails_with_the_reason_and_is_asked_for_again_later(conn, many, embed_spy, monkeypatch):
    await many()
    await listed(conn, "x y z")
    embed_spy.fail = EmbedError("unavailable", "Ollama is not reachable (ConnectError)")
    job = await jobs.claim(conn, [skillmatch.KIND])
    assert await skillmatch.run_job(conn, job) == "failed"
    assert "Ollama is not reachable" in (await jobs.get(conn, job.id)).error
    embed_spy.fail = None
    await listed(conn, "x y z")
    assert not await jobs.list(conn, kind=skillmatch.KIND, states=["queued"])        # not with every message while it is down
    monkeypatch.setattr(skillmatch, "RETRY_AFTER", 0)
    await listed(conn, "x y z")
    assert len(await jobs.list(conn, kind=skillmatch.KIND, states=["queued"])) == 1


async def test_a_run_names_the_fitting_skills_after_its_lookups_and_records_which(conn, many, tmp_path, embed_spy):
    """In the run: the lookup in the chat's collection embeds the message, and the skills are ranked with that vector."""
    native.register_all()
    await many()
    await skillmatch.index(conn)
    col = await make_collection(conn, "RAG", dim=1024)              # the same embedder as the skills: one vector serves both
    await ingest_texts(conn, col, tmp_path, {"notes.md": "notes on a smart contract reentrancy audit"})
    chat = await chats.create(conn, None, "pentesting", "abliteration", "abliterated-model", {})
    await kb.set_chat_kb(conn, chat.id, collection_ids=[col.id])
    said = "smart contract reentrancy withdraw"
    user_msg = await messages.add(conn, chat.id, None, "user", [TextBlock(text=said)])
    p = Turns("abliteration"); use(abliteration=p)
    embed_spy.batches.clear()
    run, _ = await go(conn, chat, user_msg)
    assert embed_spy.batches == [[said]]                            # one request for the lookup; none for the skills
    steps = [(s.kind, s.name, s.status) for s in run.steps]
    assert steps[:3] == [("retrieval", "kb_search", "done"), ("retrieval", "skills", "done"), ("model_call", "abliteration/abliterated-model", "done")]
    d = run.steps[1].detail
    assert (d["by"], d["of"], d["shown"][0]["name"], len(d["shown"])) == ("meaning", 16, "skill-10", skillmatch.SHOWN)
    assert d["shown"][0]["score"] >= d["shown"][1]["score"] and d["query"] == said
    system = p.calls[0]["system"]
    assert f"the {skillmatch.SHOWN} of 16 whose descriptions fit this message best" in system and "- skill-10: " in system
    assert system.count("\n- skill-") == skillmatch.SHOWN - sum(1 for s in d["shown"] if not s["name"].startswith("skill-"))
    assert system.index("KB_REFERENCE") < system.index("whose descriptions fit this message best")


async def test_a_run_with_few_skills_has_no_step_for_them_and_names_them_all(conn):
    native.register_all()
    chat = await chats.create(conn, None, "pentesting", "abliteration", "abliterated-model", {})
    user_msg = await messages.add(conn, chat.id, None, "user", [TextBlock(text="plan the work")])
    p = Turns("abliteration"); use(abliteration=p)
    run, _ = await go(conn, chat, user_msg)
    assert all(s.name != "skills" for s in run.steps)
    assert "Skills the owner installed, each a set of instructions for a kind of work. When one fits" in p.calls[0]["system"]
    assert p.calls[0]["system"].count("\n- audit: ") == 1


async def test_a_skill_started_with_its_command_is_given_in_full_before_the_lookups_as_before(conn, many):
    native.register_all()
    await many()
    chat = await chats.create(conn, None, "pentesting", "abliteration", "abliterated-model", {})
    user_msg = await messages.add(conn, chat.id, None, "user", [TextBlock(text="/skill-04 look at this apk")])
    p = Turns("abliteration"); use(abliteration=p)
    await go(conn, chat, user_msg)
    system = p.calls[0]["system"]
    assert '<skill name="skill-04">' in system and "# how to work on android apk reverse smali" in system
    assert system.index('<skill name="skill-04">') < system.index("whose descriptions fit this message best")


async def test_one_description_without_a_vector_leaves_the_whole_ranking_to_the_words(conn, many, embed_spy):
    """Scores by meaning and scores by words do not compare, so the two are never mixed in one list."""
    made = await many()
    await skillmatch.index(conn)
    known = QueryVectors()
    await known.of(await skillmatch.spec(conn), "wifi handshake capture", 5)
    assert (await listed(conn, "wifi handshake capture", known))[1].by == "meaning"
    await store.update(conn, made[3].id, description="Use when the owner asks about something else now.")
    names, fit = await listed(conn, "wifi handshake capture", known)
    assert fit.by == "words" and names[0] == "skill-05" and len(await jobs.list(conn, kind=skillmatch.KIND, states=["queued"])) == 1


async def test_vectors_of_another_embedder_are_not_used_and_are_made_again(conn, many, embed_spy):
    await many()
    await skillmatch.index(conn)
    await settings.put(conn, "memory_embedder", "fake:512")
    spec = await skillmatch.spec(conn)
    assert spec == "fake:512" and await skillmatch.vectors(conn, await skillmatch.candidates(conn), spec) == {}
    known = QueryVectors()
    await known.of(spec, "wifi handshake capture", 5)
    assert (await listed(conn, "wifi handshake capture", known))[1].by == "words"
    assert await skillmatch.index(conn) == 16
    assert {r["spec"] for r in await (await conn.execute("select spec from skill_vectors")).fetchall()} == {"fake:512"}
    assert (await listed(conn, "wifi handshake capture", known))[1].by == "meaning"


async def test_the_message_is_found_by_the_words_the_lookup_embedded_whatever_stands_around_them(conn, many):
    await many()
    await skillmatch.index(conn)
    known = QueryVectors()
    await known.of(await skillmatch.spec(conn), "wifi handshake capture", 5)        # as the lookup cuts it: without the edges
    assert (await listed(conn, "  wifi handshake capture \n", known))[1].by == "meaning"


async def test_the_embedding_model_is_unloaded_when_the_descriptions_are_done(conn, many, monkeypatch):
    from hunterai.kb import embed
    released = []

    async def release(self):
        released.append(self.spec)
    monkeypatch.setattr(embed.FakeEmbedder, "release", release)
    await many()
    assert await skillmatch.index(conn) == 16 and released == ["fake:1024"]
    assert await skillmatch.index(conn) == 0 and released == ["fake:1024"]            # nothing to embed: no model is touched


async def test_a_run_whose_model_takes_no_tools_ranks_no_skills(conn, many):
    from tests.test_engine import Scripted
    native.register_all()
    await many()
    chat = await chats.create(conn, None, "pentesting", "abliteration", "abliterated-model", {})
    user_msg = await messages.add(conn, chat.id, None, "user", [TextBlock(text="graphql introspection schema queries")])
    p = Scripted("abliteration"); use(abliteration=p)
    run, _ = await go(conn, chat, user_msg)
    assert all(s.name != "skills" for s in run.steps) and "skill-00" not in p.calls[0]["system"]
    assert not await jobs.list(conn, kind=skillmatch.KIND)


async def test_the_worker_runs_the_job(conn, many):
    """Asked for by a run, done by the worker beside it: the run never waits for it."""
    from hunterai import worker
    await many()
    await listed(conn, "anything at all")
    assert await worker.drain(conn) == 1
    spec = await skillmatch.spec(conn)
    assert len(await skillmatch.vectors(conn, await skillmatch.candidates(conn), spec)) == 16
    assert (await jobs.list(conn, kind=skillmatch.KIND, limit=1))[0].state == "done"
