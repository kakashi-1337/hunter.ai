"""The retrieval test: what the search of a knowledge collection brings for things that stand in one passage only.

Two kinds of things are asked. Identifiers (a CVE number, a path, a parameter name) that occur in exactly one passage,
which need no model. And questions the decision model wrote about single passages. Each is asked of the real search,
and the test says where the passage it came from stood: first, among the eight a message gets, among the forty the
search chooses from, or nowhere. It is kept, so that a change to the search can be measured with the same questions."""
import json
from pathlib import Path

from hunterai import config
from hunterai.kb import ragtest, search
from hunterai.providers import registry
from hunterai.repo import kb, settings
from tests.kb_samples import StubStore, ingest_texts
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
    assert got["asked"] == len(test["items"]) > 0 and got["first"] == got["sent"] == got["pool"] == got["asked"] and got["missed"] == 0
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
    assert result["kinds"]["identifier"] == {"asked": 1, "first": 1, "sent": 1, "pool": 1, "missed": 0}
    assert result["kinds"]["question"] == {"asked": 1, "first": 0, "sent": 0, "pool": 0, "missed": 1}
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


# ---- what the first real run showed

def hit(chunk_id, text, dense=1, keyword=None):
    from hunterai.types import Hit
    return Hit(chunk_id=chunk_id, document_id="d", collection_id="c", source_uri=f"{chunk_id}.md", ordinal=0, text=text,
               line_start=1, line_end=2, score=0.5, dense_rank=dense, keyword_rank=keyword)


async def test_in_the_message_means_what_fits_a_message_not_the_first_eight(conn, monkeypatch):
    """A message gets 8 passages at most and 6,000 characters of whole passages: of passages of full size, about
    three. The run counted "among the first 8", which the second of two long passages is, and a message does not get it."""
    async def found(conn, collections, query, limit=8, **kw):
        return [hit("long", "x" * 5000), hit("wanted", "y" * 2000, dense=2)]
    monkeypatch.setattr(ragtest.search, "search", found)
    test = {"collection": "c", "name": "n", "created": "t", "written_by": None,
            "items": [{"kind": "identifier", "query": "q", "chunk_id": "wanted", "source": "wanted.md"},
                      {"kind": "identifier", "query": "q", "chunk_id": "long", "source": "long.md"}]}
    result = await ragtest.run(conn, test)
    assert result["kinds"]["identifier"] == {"asked": 2, "first": 1, "sent": 1, "pool": 2, "missed": 0}
    said = ragtest.report(test, result)
    assert "in the message" in said and "1 stood among the 40 but not in the message" in said


async def test_a_decision_model_that_writes_no_question_is_named_with_its_reason_and_not_asked_thirty_times(conn, col, tmp_path):
    """The first real run asked a model thirty times, got nothing usable, and said nothing of it."""
    from hunterai.providers.base import ProviderError
    await ingest_texts(conn, col, tmp_path, TEXTS)
    await settings.put(conn, "decision_provider", "decider")
    await settings.put(conn, "decision_model", "d")
    d = Json("decider", *[ProviderError("unavailable", "the model did not answer")] * 6)
    registry.override("decider", d)
    test = await ragtest.build(conn, col, identifiers=0, questions=4)
    assert test["items"] == [] and len(d.calls) == 3                    # three in a row with none written: it stops asking
    assert test["no_questions"]["asked"] == 3 and "the model did not answer" in test["no_questions"]["why"]
    assert test["no_questions"]["by"] == "decider/d"
    said = ragtest.report({**test, "name": col.name}, {"at": "t", "kinds": {}, "found_by": {"both": 0, "vectors": 0, "keywords": 0}, "misses": []})
    assert "No question was written" in said and "decider/d" in said and "the model did not answer" in said and "--new" in said


async def test_a_reply_without_a_question_in_it_is_a_reason_too(conn, col, tmp_path):
    await ingest_texts(conn, col, tmp_path, TEXTS)
    await settings.put(conn, "decision_provider", "decider")
    await settings.put(conn, "decision_model", "d")
    registry.override("decider", Json("decider", "{}", "not json", json.dumps({"question": "  "})))
    test = await ragtest.build(conn, col, identifiers=0, questions=3)
    assert test["no_questions"]["asked"] == 3 and "no question in its reply" in test["no_questions"]["why"]


def test_a_run_from_before_the_message_was_counted_is_not_compared_with():
    test = {"name": "n", "written_by": None, "items": []}
    now = {"at": "t", "kinds": {"identifier": {"asked": 30, "first": 9, "sent": 12, "pool": 25, "missed": 5}},
           "found_by": {"both": 9, "vectors": 0, "keywords": 16}, "misses": []}
    old = {"at": "t", "kinds": {"identifier": {"asked": 30, "first": 9, "top": 25, "pool": 25, "missed": 5}}}
    assert "counted the first 8" in ragtest.report(test, now, old)
    same = ragtest.report(test, now, now)
    assert "the same as in the run before" in same


async def test_a_passage_put_first_for_its_spelling_is_counted_as_that(conn, col, tmp_path, monkeypatch):
    """Fifty passages say "wait for"; one has `wait_for`. Neither half of the search brings that one."""
    texts = {f"n{i:02}.txt": f"Then wait for the page to load, and wait for the next one. Note {i}." for i in range(50)}
    texts["api.txt"] = "Call wait_for with a selector before reading the table."
    docs = await ingest_texts(conn, col, tmp_path, texts)
    test = {"collection": col.id, "name": col.name, "written_by": None,
            "items": [{"kind": "identifier", "query": "wait_for", "chunk_id": kb.chunk_id(docs["api.txt"].id, 0), "source": "api.txt"}]}
    monkeypatch.setattr(search, "get_store", lambda: StubStore({}))        # the vectors bring nothing here
    result = await ragtest.run(conn, test)
    assert result["kinds"]["identifier"] == {"asked": 1, "first": 1, "sent": 1, "pool": 1, "missed": 0}
    assert result["exact"] == 1 and result["found_by"] == {"both": 0, "vectors": 0, "keywords": 0, "spelling": 1}
    said = ragtest.report(test, result)
    assert "1 stood first for the exact spelling of an identifier" in said and "By its exact spelling only: 1." in said


def test_the_report_says_how_the_first_places_changed_and_reads_a_run_from_before_the_spelling_was_looked_for():
    test = {"name": "n", "written_by": None, "items": []}
    old = {"at": "t", "kinds": {"identifier": {"asked": 30, "first": 9, "sent": 22, "pool": 25, "missed": 5}},
           "found_by": {"both": 9, "vectors": 0, "keywords": 16}, "misses": []}
    now = {"at": "t", "kinds": {"identifier": {"asked": 30, "first": 30, "sent": 30, "pool": 30, "missed": 0}},
           "found_by": {"both": 9, "vectors": 0, "keywords": 16, "spelling": 5}, "exact": 30, "misses": []}
    said = ragtest.report(test, now, old)
    assert "identifiers in the message: 30, 22 in the run before" in said
    assert "identifiers found first: 30, 9 in the run before" in said
    assert "By its exact spelling only: 5." in said and "30 stood first for the exact spelling of an identifier" in said
    assert "exact spelling" not in ragtest.report(test, old)            # an old run has nothing of it, and says nothing


def test_the_test_and_the_search_agree_on_what_an_identifier_is():
    assert ragtest._FORMS is search.IDENTIFIER_FORMS


async def test_an_identifier_is_asked_only_when_one_passage_of_the_whole_collection_holds_it(conn, col, tmp_path, monkeypatch):
    """A test is built from a part of a large collection. An identifier that stands once in that part and again
    outside it has no single right passage: it was asked all the same, and counted as not found."""
    half = 4
    texts = {f"n{i}.md": f"Note {i} has own_name_{i} for itself, and pair_name_{i // 2} with the note beside it." + FILL for i in range(2 * half)}
    await ingest_texts(conn, col, tmp_path, texts)
    monkeypatch.setattr(ragtest, "SCAN", half)                      # the test sees every second of the eight passages
    seen = [text for _, text, _ in await ragtest._passages(conn, col.id)]
    assert len(seen) == half and sum("pair_name_0" in t for t in seen) == 1     # one of a pair is in that part, one is not
    test = await ragtest.build(conn, col, questions=0)
    asked = [i["query"] for i in test["items"]]
    assert asked and all(q.startswith("own_name_") for q in asked)
    assert (test["passages"], test["of"]) == (half, 2 * half)       # and it says how much of the collection it saw
    said = ragtest.report(test, await ragtest.run(conn, test))
    assert "built from 4 of the 8 passages" in said


async def test_a_miss_says_in_how_many_passages_the_identifier_stands(conn, col, tmp_path, monkeypatch):
    texts = {f"n{i:02}.txt": f"Then wait for the page to load, and wait for the next one. Note {i}." for i in range(50)}
    texts |= {f"api{i}.txt": f"Call wait_for with a selector, variant {i}." for i in range(search.RARE + 3)}
    docs = await ingest_texts(conn, col, tmp_path, texts)
    monkeypatch.setattr(search, "get_store", lambda: StubStore({}))
    item = lambda query, name: {"kind": "identifier", "query": query, "chunk_id": kb.chunk_id(docs[name].id, 0), "source": name}  # noqa: E731
    test = {"collection": col.id, "name": col.name, "written_by": None,
            "items": [item("wait_for", "api7.txt"), item("gone_name", "api0.txt")]}
    result = await ragtest.run(conn, test)
    assert [(m["query"], m["holders"]) for m in result["misses"]] == [("wait_for", search.RARE + 3), ("gone_name", 0)]
    said = ragtest.report(test, result)
    assert "identifier: wait_for   (from api7.txt): it stands in 8 passages of the collection, so there is no single right passage" in said
    assert "identifier: gone_name   (from api0.txt): no passage of the collection holds it any more" in said
    assert "1 of the identifiers that were not found stands in more than one passage" in said and "--new" in said


def test_a_miss_of_an_identifier_that_one_passage_holds_is_called_what_it_is():
    test = {"name": "n", "written_by": None, "items": []}
    result = {"at": "t", "kinds": {"identifier": {"asked": 1, "first": 0, "sent": 0, "pool": 0, "missed": 1}},
              "found_by": {"both": 0, "vectors": 0, "keywords": 0}, "misses": [{"kind": "identifier", "query": "only_here", "source": "a.md", "holders": 1}]}
    said = ragtest.report(test, result)
    assert "identifier: only_here   (from a.md): one passage holds it, and the search did not bring it" in said
    assert "more than one passage" not in said
    # A run from before the passages were counted says nothing about them.
    result["misses"] = [{"kind": "identifier", "query": "only_here", "source": "a.md"}]
    assert "- identifier: only_here   (from a.md)\n" in ragtest.report(test, result) + "\n"


async def test_a_test_of_a_large_collection_is_built_from_passages_spread_over_all_of_it(conn, col, tmp_path, monkeypatch):
    """Its first 5,000 passages are what was indexed last: of 670,000, one book and one repository."""
    await ingest_texts(conn, col, tmp_path, {f"n{i:02}.md": f"Note {i} stands alone." + FILL for i in range(12)})
    monkeypatch.setattr(ragtest, "SCAN", 4)
    where = sorted(int(Path(uri).stem[1:]) for _, _, uri in await ragtest._passages(conn, col.id))
    assert len(where) == 4 and where[0] <= 2 and where[-1] >= 9 and all(b - a == 3 for a, b in zip(where, where[1:]))
    monkeypatch.setattr(ragtest, "SCAN", 5000)          # a collection that fits is looked at whole
    assert len(await ragtest._passages(conn, col.id)) == 12
