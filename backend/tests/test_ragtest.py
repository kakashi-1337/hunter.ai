"""The retrieval test: what the search of a knowledge collection brings for things that stand in one passage only.

Two kinds of things are asked. Identifiers (a CVE number, a path, a parameter name) that occur in exactly one passage,
which need no model. And questions the decision model wrote about single passages. Each is asked of the real search,
and the test says where the passage it came from stood: first, among the eight a message gets, among the forty the
search chooses from, or nowhere. It is kept, so that a change to the search can be measured with the same questions."""
import json

from hunterai import config
from hunterai.kb import ragtest
from hunterai.providers import registry
from hunterai.repo import kb, settings
from tests.kb_samples import ingest_texts
from tests.test_decision_engine import Json

FILL = " This note is long enough to be worth a question: it goes on about how the issue was found, what was tried first, " \
       "and what the owner of the program answered when it was reported to them last spring."
TEXTS = {
    "sqli.md": "The login form at /app/auth/login is injectable in the user_name parameter. CVE-2024-11111 is the same bug "
               "in the vendor's product." + FILL,
    "xss.md": "Stored XSS on the profile page: the displayName field is rendered unescaped, as CVE-2023-22222 describes." + FILL,
    "both.md": "This note mentions /shared/path/here and shared_name, and so does the next one." + FILL,
    "other.md": "Another note with /shared/path/here and shared_name in it, on a different subject." + FILL,
}


def test_only_identifiers_that_stand_in_one_passage_are_taken():
    found = ragtest.identifiers([("a", TEXTS["sqli.md"]), ("b", TEXTS["xss.md"]), ("c", TEXTS["both.md"]), ("d", TEXTS["other.md"])])
    assert found["CVE-2024-11111"] == "a" and found["/app/auth/login"] == "a" and found["user_name"] == "a"
    assert found["CVE-2023-22222"] == "b" and found["displayName"] == "b"
    assert "/shared/path/here" not in found and "shared_name" not in found      # in two passages: no single right answer


def test_an_identifier_that_is_part_of_a_longer_one_elsewhere_is_not_taken():
    found = ragtest.identifiers([("a", "see /api/v1/users for the list"), ("b", "and /api/v1/users/42 for one of them")])
    assert "/api/v1/users" not in found and found["/api/v1/users/42"] == "b"


async def test_a_test_of_identifiers_needs_no_model_and_says_where_the_search_put_each(conn, col, tmp_path):
    await ingest_texts(conn, col, tmp_path, TEXTS)
    test = await ragtest.build(conn, col, identifiers=10, questions=0)
    assert test["written_by"] is None and {i["kind"] for i in test["items"]} == {"identifier"}
    assert {"CVE-2024-11111", "CVE-2023-22222"} <= {i["query"] for i in test["items"]}
    assert all(i["source"].endswith(".md") and i["chunk_id"] for i in test["items"])
    result = await ragtest.run(conn, test)
    got = result["kinds"]["identifier"]
    assert got["asked"] == len(test["items"]) > 0 and got["first"] == got["top"] == got["pool"] == got["asked"] and got["missed"] == 0
    assert result["misses"] == [] and sum(result["found_by"].values()) == got["asked"]


async def test_questions_are_written_by_the_decision_model_one_for_a_passage(conn, col, tmp_path):
    await ingest_texts(conn, col, tmp_path, TEXTS)
    await settings.put(conn, "decision_provider", "decider")
    await settings.put(conn, "decision_model", "d")
    d = Json("decider", json.dumps({"question": "Which field of the login form can be injected?"}),
             json.dumps({"question": "Where is the stored XSS?"}))
    registry.override("decider", d)
    test = await ragtest.build(conn, col, identifiers=0, questions=2)
    asked = [i for i in test["items"] if i["kind"] == "question"]
    assert [i["query"] for i in asked] == ["Which field of the login form can be injected?", "Where is the stored XSS?"]
    assert test["written_by"] == "decider/d" and len({i["chunk_id"] for i in asked}) == 2
    shown = [c["messages"][0].blocks[0].text for c in d.calls]
    assert len(shown) == 2 and all(FILL.strip()[:40] in s for s in shown)           # each was shown one passage


async def test_a_model_that_writes_no_question_costs_that_passage_only(conn, col, tmp_path):
    await ingest_texts(conn, col, tmp_path, TEXTS)
    await settings.put(conn, "decision_provider", "decider")
    await settings.put(conn, "decision_model", "d")
    registry.override("decider", Json("decider", "not json at all", json.dumps({"question": "Where is the stored XSS?"})))
    test = await ragtest.build(conn, col, identifiers=0, questions=2)
    assert [i["query"] for i in test["items"]] == ["Where is the stored XSS?"]


async def test_a_passage_the_search_does_not_bring_is_counted_and_named(conn, col, tmp_path):
    docs = await ingest_texts(conn, col, tmp_path, TEXTS)
    test = {"collection": col.id, "name": col.name, "created": "t", "written_by": None, "items": [
        {"kind": "identifier", "query": "CVE-2024-11111", "chunk_id": kb.chunk_id(docs["sqli.md"].id, 0), "source": "sqli.md"},
        {"kind": "question", "query": "something no passage is about", "chunk_id": "f" * 32, "source": "gone.md"}]}
    result = await ragtest.run(conn, test)
    assert result["kinds"]["identifier"] == {"asked": 1, "first": 1, "top": 1, "pool": 1, "missed": 0}
    assert result["kinds"]["question"] == {"asked": 1, "first": 0, "top": 0, "pool": 0, "missed": 1}
    assert result["misses"] == [{"kind": "question", "query": "something no passage is about", "source": "gone.md"}]
    said = ragtest.report(test, result)
    assert "identifiers" in said and "questions" in said and "gone.md" in said and "something no passage is about" in said


async def test_the_command_builds_a_test_once_keeps_it_and_compares_the_next_run_with_the_one_before(conn, col, tmp_path, capsys):
    await ingest_texts(conn, col, tmp_path, TEXTS)
    assert await ragtest.main([]) == 0 and col.name in capsys.readouterr().out        # no name: the collections are listed
    assert await ragtest.main([col.name, "--questions", "0"]) == 0
    first = capsys.readouterr().out
    assert "identifiers" in first and "found first" in first and "run before" not in first
    kept = config.home() / "rag-tests" / f"{col.id}.json"
    built = json.loads(kept.read_text())
    assert len(built["runs"]) == 1
    assert await ragtest.main([col.name]) == 0                                         # the kept test, asked again
    second = capsys.readouterr().out
    again = json.loads(kept.read_text())
    assert "the same as in the run before" in second and len(again["runs"]) == 2
    assert again["test"] == built["test"]                                              # not built anew
    assert await ragtest.main(["no such collection"]) == 1 and "no such collection" in capsys.readouterr().out


async def test_in_a_data_folder_that_was_never_used_the_command_says_there_is_no_collection(capsys):
    """As the other commands do, it sets the database up before it reads from it: no "no such table"."""
    assert await ragtest.main([]) == 0 and "none yet" in capsys.readouterr().out
