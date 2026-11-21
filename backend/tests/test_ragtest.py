"""The retrieval test: what the search of a knowledge collection brings for things that stand in one passage only.

Two kinds of things are asked. Identifiers (a CVE number, a path, a parameter name) that occur in exactly one passage,
which need no model. And questions the decision model wrote about single passages. Each is asked of the real search,
and the test says where the passage it came from stood: first, among the eight a message gets, among the forty the
search chooses from, or nowhere. It is kept, so that a change to the search can be measured with the same questions."""
import json
from pathlib import Path

import pytest

from hunterai import config
from hunterai.kb import ragtest, search
from hunterai.providers import registry
from hunterai.repo import kb, settings
from tests.kb_samples import StubStore, ingest_texts
from tests.test_decision_engine import Json

FILL = " This note is long enough to be worth a question: it goes on about how the issue was found, what was tried first, " \
       "and what the owner of the program answered when it was reported to them last spring."
# Each note says something of its own at length: notes that share most of their wording have no single right passage,
# and a test writes no question from them.
TEXTS = {
    "sqli.md": "The login form at /app/auth/login is injectable in the user_name parameter. CVE-2024-11111 is the same bug "
               "in the vendor's product. It turned up while the search box was being tested for something else, with a payload "
               "that closed the quote and added a sleep of five seconds: the answer took that much longer every time it was sent "
               "again that afternoon." + FILL,
    "xss.md": "Stored XSS on the profile page: the displayName field is rendered unescaped, as CVE-2023-22222 describes. A visitor "
              "who opens the profile of the attacker runs the script at once, and so does every moderator who reviews the account, "
              "which is how a session of a member of staff could be taken over during the demonstration for the client." + FILL,
    "both.md": "This note mentions /shared/path/here and shared_name, and so does the next one. It exists to show that two files "
               "can talk about one and the same thing in nearly one breath, which happens all the time in real folders of notes, "
               "where a colleague copied a line from a ticket into two places and forgot about the second." + FILL,
    "other.md": "Another note with /shared/path/here and shared_name in it, on a different subject. Its real topic is the day of "
                "the conference in the harbour city: the long queue for coffee before the keynote, the room that was far too "
                "small for the workshop, and the train home that left twenty minutes late and arrived early." + FILL,
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


async def test_a_passage_the_search_does_not_bring_is_counted_and_named(conn, col, tmp_path, monkeypatch):
    docs = await ingest_texts(conn, col, tmp_path, TEXTS)
    monkeypatch.setattr(ragtest, "POOL", 1)             # the search is asked for one passage: in four notes, all would be found
    first = (await search.search(conn, [col.id], "something no passage is about", limit=1))[0]
    wanted = next(name for name, doc in docs.items() if doc.id != first.document_id)       # a passage it did not bring
    test = {"collection": col.id, "name": col.name, "created": "t", "written_by": None, "items": [
        {"kind": "identifier", "query": "CVE-2024-11111", "chunk_id": kb.chunk_id(docs["sqli.md"].id, 0), "source": "sqli.md"},
        {"kind": "question", "query": "something no passage is about", "chunk_id": kb.chunk_id(docs[wanted].id, 0), "source": wanted}]}
    result = await ragtest.run(conn, test)
    assert result["kinds"]["identifier"] == {"asked": 1, "first": 1, "sent": 1, "pool": 1, "missed": 0}
    assert result["kinds"]["question"] == {"asked": 1, "first": 0, "sent": 0, "pool": 0, "missed": 1}
    assert result["misses"] == [{"kind": "question", "query": "something no passage is about", "source": wanted,
                                 "brought": first.source_uri}]
    said = ragtest.report(test, result)
    assert "identifiers" in said and "questions" in said and wanted in said and "something no passage is about" in said


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

def hit(chunk_id, text, dense=1, keyword=None, source=None, document="d"):
    from hunterai.types import Hit
    return Hit(chunk_id=chunk_id, document_id=document, collection_id="c", source_uri=source or f"{chunk_id}.md", ordinal=0, text=text,
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


# ---- what a collection of 670,000 passages showed: half of the questions had no single right passage

BENCH = ("Ensure that audit logging is enabled for the {product} service. Rationale: without audit logs an administrator cannot "
         "tell who changed a setting or when. Audit: open the {product} console, choose Settings, then Logging, and confirm that "
         "the audit trail is switched on for every region. Remediation: switch the audit trail on and choose a bucket that only "
         "the security team can read. Default value: the audit trail is off when a {product} account is created.")
OWN = ("The pentest of the staging site started on a Monday. The first finding was an open redirect in the logout link, which "
       "the team fixed the same week. The second was a session cookie without the secure flag, reported two days later and "
       "still open when the retest began in the following month, as the notes of the retest say in their first paragraph.")


def test_a_twin_shares_most_of_a_passages_wording():
    one, other = BENCH.format(product="Relational Database"), BENCH.format(product="Object Storage")
    assert ragtest.twin(one, other) and ragtest.twin(other, one)                # the same text for another product
    assert ragtest.twin(one, "## 4.2 Logging\n\n" + one + "\n\nSee also the next control.")        # it stands whole inside a longer one
    assert not ragtest.twin(one, OWN)
    assert not ragtest.twin(one, "Audit logging should be on for the Relational Database service, so that changes can be traced "
                                 "to a person. Check it in the console under Settings and Logging; turn it on if it is off.")   # same subject, other words
    assert not ragtest.twin(one, one[:160])                                     # a piece of it does not answer what the whole answers
    assert not ragtest.twin("", one) and not ragtest.twin(one, "") and not ragtest.twin("two words", "two words")


def test_notes_that_share_a_closing_paragraph_are_no_twins():
    """Seen in this file's own notes: the same forty words at the end of each, and before them a subject of its own.
    Between "no twin" and "twin" stands a passage that shares most of its wording: no question is written from it."""
    shares = [ragtest.shared(a, b) for a in TEXTS.values() for b in TEXTS.values() if a is not b]
    assert 0.2 < min(shares) and max(shares) < ragtest.NEAR < ragtest.TWIN
    one, other = BENCH.format(product="Relational Database"), BENCH.format(product="Object Storage")
    assert ragtest.shared(one, other) >= ragtest.TWIN and ragtest.shared(one, one) == 1.0 and ragtest.shared("", one) == 0.0
    words = one.split()
    some_changed = " ".join(f"other{i}" if i % 8 == 0 else w for i, w in enumerate(words))        # every eighth word is another
    assert ragtest.NEAR <= ragtest.shared(one, some_changed) < ragtest.TWIN and not ragtest.twin(one, some_changed)


@pytest.mark.parametrize("uri", [
    "/home/u/notes/report.md", "/home/u/notes/readme.txt", "/home/u/site/page.html", "/home/u/books/guide.pdf", "/home/u/notes/plan.docx",
    "git:github.com/o/r/docs/intro.md", "git:github.com/o/r/docs/api.rst", "upload:notes/findings.md", "/x/docs.zip!/docs/a.md",
    "https://docs.example.com/guide/start", "https://example.com/llms-full.txt", "https://example.com/post.html?page=2",
])
def test_a_document_meant_to_be_read(uri):
    assert ragtest.readable(uri)


@pytest.mark.parametrize("uri", [
    "/home/u/web/src/app.js", "/home/u/web/src/types.d.ts", "/home/u/web/i18n/locales/ar.ts", "git:github.com/o/r/src/main.py",
    "/home/u/web/package.json", "/home/u/data/rows.csv", "/home/u/db/schema.sql", "/home/u/web/config.yaml", "upload:app.min.js",
    "/home/u/web/.next/server/pages/index.html",            # a page a build made: left out of a collection today
    "/home/u/web/.next/static/chunks/main.js.map", "/x/pkg.tgz!/package/dist/client.d.ts", "/x/app.bundle.js.gz!/app.bundle.js",
    "/home/u/web/yarn.lock", "/home/u/web/LICENSE", "https://example.com/api/data.json", "https://example.com/feed.xml",
])
def test_a_document_that_is_code_or_data_or_machine_made(uri):
    assert not ragtest.readable(uri)


async def decider(conn, *questions):
    await settings.put(conn, "decision_provider", "decider")
    await settings.put(conn, "decision_model", "d")
    d = Json("decider", *[json.dumps({"question": q}) for q in questions])
    registry.override("decider", d)
    return d


async def test_questions_are_written_from_documents_meant_to_be_read(conn, col, tmp_path):
    """Asked about a line of a bundle or a table of translations, a model writes a question no one would ask."""
    code = "def check(value):\n    '''" + FILL + FILL + "'''\n    return value\n"
    await ingest_texts(conn, col, tmp_path, {"report.md": OWN, "check.py": code, "table.json": json.dumps({"rows": [FILL] * 4})})
    d = await decider(conn, "When did the pentest of the staging site start?", "unused", "unused")
    test = await ragtest.build(conn, col, identifiers=0, questions=3, code=0)
    assert [(i["query"], Path(i["source"]).name) for i in test["items"]] == [("When did the pentest of the staging site start?", "report.md")]
    assert len(d.calls) == 1 and test["prose"] == 1
    said = ragtest.report(test, await ragtest.run(conn, test))
    assert "from documents meant to be read" in said


async def test_a_collection_of_code_and_data_gets_no_questions_and_says_why(conn, col, tmp_path):
    code = "def check(value):\n    '''" + FILL + FILL + "'''\n    return value\n"
    await ingest_texts(conn, col, tmp_path, {"check.py": code + "token_name_one = 1\n", "other.py": code + "token_name_two = 2\n"})
    d = await decider(conn, "unused")
    test = await ragtest.build(conn, col, identifiers=2, questions=3, code=0)
    assert d.calls == [] and {i["kind"] for i in test["items"]} == {"identifier"} and test["prose"] == 0
    said = ragtest.report(test, await ragtest.run(conn, test))
    assert "No question was written: no passage of this collection comes from a document meant to be read" in said


async def test_a_passage_that_has_a_twin_gets_no_question(conn, col, tmp_path):
    """The same control in the benchmark of every product: whichever of them the search brings answers the question."""
    await ingest_texts(conn, col, tmp_path, {"db.md": BENCH.format(product="Relational Database"),
                                             "storage.md": BENCH.format(product="Object Storage"), "report.md": OWN})
    d = await decider(conn, "When did the pentest of the staging site start?", "unused", "unused")
    test = await ragtest.build(conn, col, identifiers=0, questions=3)
    assert [Path(i["source"]).name for i in test["items"]] == ["report.md"] and len(d.calls) == 1
    assert (test["prose"], test["near"]) == (3, 2)
    said = ragtest.report(test, await ragtest.run(conn, test))
    assert "2 passages were passed over because another passage shares most of their wording" in said


async def test_a_question_that_a_twin_of_its_passage_answers_is_found(conn, col, tmp_path, monkeypatch):
    """A test that was built before, or a collection that grew: the twin stands first and the passage itself nowhere."""
    docs = await ingest_texts(conn, col, tmp_path, {"db.md": BENCH.format(product="Relational Database"),
                                                    "storage.md": BENCH.format(product="Object Storage"), "report.md": OWN})
    ids = {name: kb.chunk_id(doc.id, 0) for name, doc in docs.items()}
    texts = {name: (await kb.get_chunk(conn, cid)).text for name, cid in ids.items()}

    async def found(conn, collections, query, limit=8, **kw):
        return [hit(ids["storage.md"], texts["storage.md"]), hit(ids["report.md"], texts["report.md"], dense=2)]
    monkeypatch.setattr(ragtest.search, "search", found)
    test = {"collection": col.id, "name": col.name, "created": "t", "written_by": "m", "items": [
        {"kind": "question", "query": "How is audit logging checked?", "chunk_id": ids["db.md"], "source": "db.md"},
        {"kind": "question", "query": "When did the pentest start?", "chunk_id": ids["report.md"], "source": "report.md"}]}
    result = await ragtest.run(conn, test)
    assert result["kinds"]["question"] == {"asked": 2, "first": 1, "sent": 2, "pool": 2, "missed": 0}
    assert result["twins"] == 1 and result["misses"] == []
    said = ragtest.report(test, result)
    assert "1 question was answered by a twin of its passage" in said


async def test_an_identifier_is_never_answered_by_a_twin(conn, col, tmp_path, monkeypatch):
    docs = await ingest_texts(conn, col, tmp_path, {"db.md": BENCH.format(product="Relational Database") + " See rds_audit_flag.",
                                                    "storage.md": BENCH.format(product="Object Storage")})
    ids = {name: kb.chunk_id(doc.id, 0) for name, doc in docs.items()}

    async def found(conn, collections, query, limit=8, **kw):
        return [hit(ids["storage.md"], BENCH.format(product="Object Storage"))]
    monkeypatch.setattr(ragtest.search, "search", found)
    test = {"collection": col.id, "name": col.name, "created": "t", "written_by": None, "items": [
        {"kind": "identifier", "query": "rds_audit_flag", "chunk_id": ids["db.md"], "source": "db.md"}]}
    result = await ragtest.run(conn, test)
    assert result["kinds"]["identifier"] == {"asked": 1, "first": 0, "sent": 0, "pool": 0, "missed": 1} and result["twins"] == 0


async def test_what_the_collection_no_longer_holds_is_not_asked_about(conn, col, tmp_path):
    """After a collection was cleaned, a kept test still names passages that are gone. Those are no misses of the search."""
    docs = await ingest_texts(conn, col, tmp_path, TEXTS)
    test = {"collection": col.id, "name": col.name, "created": "t", "written_by": "m", "items": [
        {"kind": "identifier", "query": "CVE-2024-11111", "chunk_id": kb.chunk_id(docs["sqli.md"].id, 0), "source": "sqli.md"},
        {"kind": "identifier", "query": "gone_name_here", "chunk_id": "e" * 32, "source": "bundle.js"},
        {"kind": "question", "query": "What did the bundle say?", "chunk_id": "f" * 32, "source": "bundle.js"}]}
    result = await ragtest.run(conn, test)
    assert result["kinds"] == {"identifier": {"asked": 1, "first": 1, "sent": 1, "pool": 1, "missed": 0}}
    assert result["gone"] == 2 and result["misses"] == []
    said = ragtest.report(test, result)
    assert "2 of the 3 things this test asks are about passages the collection no longer holds" in said and "--new" in said
    assert "asked 1 identifier" in said


async def test_a_miss_says_when_the_search_brought_a_passage_that_shares_most_of_the_wording(conn, col, tmp_path, monkeypatch):
    """Not a twin, so it stays a miss; but whoever reads the list should know how close the search came."""
    words = BENCH.format(product="Relational Database").split()
    close = " ".join(f"other{i}" if i % 8 == 0 else w for i, w in enumerate(words))
    docs = await ingest_texts(conn, col, tmp_path, {"db.md": BENCH.format(product="Relational Database"), "close.md": close, "report.md": OWN})
    ids = {name: kb.chunk_id(doc.id, 0) for name, doc in docs.items()}
    texts = {name: (await kb.get_chunk(conn, cid)).text for name, cid in ids.items()}

    async def found(conn, collections, query, limit=8, **kw):
        return [hit(ids["report.md"], texts["report.md"]), hit(ids["close.md"], texts["close.md"], dense=2, source="close.md")]
    monkeypatch.setattr(ragtest.search, "search", found)
    test = {"collection": col.id, "name": col.name, "created": "t", "written_by": "m", "items": [
        {"kind": "question", "query": "How is audit logging checked?", "chunk_id": ids["db.md"], "source": "db.md"}]}
    result = await ragtest.run(conn, test)
    assert result["kinds"]["question"]["missed"] == 1 and result["twins"] == 0
    share = ragtest.shared(texts["db.md"], texts["close.md"])
    assert result["misses"] == [{"kind": "question", "query": "How is audit logging checked?", "source": "db.md",
                                 "near": {"share": round(share, 2), "place": 2, "source": "close.md"},
                                 "brought": f'{ids["report.md"]}.md'}]
    said = ragtest.report(test, result)
    assert f"the passage at place 2 (from close.md) shares {round(100 * share)}% of its wording" in said


# ---- what the ten misses of a real run showed: six questions did not say what they were about

async def test_the_writer_is_told_where_a_passage_comes_from(conn, col, tmp_path):
    """A passage seldom names what it is about: that stands in the title or the address of its document. Shown the
    passage alone, a model asks about "this tool" and "the product", and no search can know which one is meant."""
    await ingest_texts(conn, col, tmp_path, {"blog/what-is-acme-paint-3.md": OWN})
    d = await decider(conn, "When did the pentest of the Acme Paint 3 staging site start?")
    test = await ragtest.build(conn, col, identifiers=0, questions=1)
    shown, system = d.calls[0]["messages"][0].blocks[0].text, d.calls[0]["system"]
    assert "blog/what-is-acme-paint-3.md" in shown and shown.index("FROM:") < shown.index("PASSAGE:") and OWN[:60] in shown
    assert str(tmp_path) not in shown                                    # no more of a path on this machine than its last parts
    assert "where it comes from" in system and '"this tool"' in system and "name what" in system
    assert [i["query"] for i in test["items"]] == ["When did the pentest of the Acme Paint 3 staging site start?"]


@pytest.mark.parametrize("uri, shown", [
    ("/home/dave/artex/web/src/components/Chat.tsx", "artex/web/src/components/Chat.tsx"),
    ("/home/dave/a.md", "home/dave/a.md"),
    ("git:github.com/usestrix/strix/strix/tools/browser.py", "github.com/usestrix/strix/strix/tools/browser.py"),
    ("upload:notes/findings.md", "notes/findings.md"),
    ("https://www.example.com/blog/what-is-acme-paint-3?utm=1#top", "www.example.com/blog/what-is-acme-paint-3"),
    ("/srv/a/b/c/d/docs.zip!/docs/a.md", "a/b/c/d/docs.zip!/docs/a.md"),
])
def test_where_a_passage_comes_from_as_the_writer_is_shown_it(uri, shown):
    assert ragtest.named(uri) == shown


async def test_a_miss_says_what_the_search_brought_and_where_the_document_stood(conn, col, tmp_path, monkeypatch):
    """Whoever reads the list of misses wants to know: did the search bring something as good, another part of the
    same document, or something else altogether? That took a script of its own before."""
    docs = await ingest_texts(conn, col, tmp_path, {"db.md": BENCH.format(product="Relational Database"), "report.md": OWN})
    ids = {name: kb.chunk_id(doc.id, 0) for name, doc in docs.items()}

    async def found(conn, collections, query, limit=8, **kw):
        return [hit(ids["report.md"], OWN, source="report.md"),
                hit("another", "Another part of the same document.", dense=2, source="db.md", document=docs["db.md"].id)]
    monkeypatch.setattr(ragtest.search, "search", found)
    test = {"collection": col.id, "name": col.name, "created": "t", "written_by": "m", "items": [
        {"kind": "question", "query": "How is audit logging checked?", "chunk_id": ids["db.md"], "source": "db.md"},
        {"kind": "identifier", "query": "audit_flag_name", "chunk_id": ids["db.md"], "source": "db.md"}]}
    result = await ragtest.run(conn, test)
    assert [(m["kind"], m["brought"], m["same"]) for m in result["misses"]] == [
        ("question", "report.md", {"place": 2, "sent": True}), ("identifier", "report.md", {"place": 2, "sent": True})]
    said = ragtest.report(test, result)
    assert said.count("the search brought first: report.md. Another passage of the same document stood at place 2, in the message.") == 2


async def test_a_miss_of_which_nothing_of_the_document_was_brought_says_so(conn, col, tmp_path, monkeypatch):
    docs = await ingest_texts(conn, col, tmp_path, {"db.md": BENCH.format(product="Relational Database"), "report.md": OWN})

    async def found(conn, collections, query, limit=8, **kw):
        return [hit(kb.chunk_id(docs["report.md"].id, 0), OWN, source="report.md")]
    monkeypatch.setattr(ragtest.search, "search", found)
    test = {"collection": col.id, "name": col.name, "created": "t", "written_by": "m", "items": [
        {"kind": "question", "query": "How is audit logging checked?", "chunk_id": kb.chunk_id(docs["db.md"].id, 0), "source": "db.md"}]}
    result = await ragtest.run(conn, test)
    assert "same" not in result["misses"][0]
    assert "the search brought first: report.md. No other passage of the same document was among the 40." in ragtest.report(test, result)


async def test_which_half_of_the_search_found_them_is_said_for_each_kind(conn, col, tmp_path):
    """Asked for a bare identifier the vectors find little, and that is no weakness; for a question they should find
    most. One sum for both kinds hides which of the two it is."""
    docs = await ingest_texts(conn, col, tmp_path, TEXTS)
    test = {"collection": col.id, "name": col.name, "created": "t", "written_by": "m", "items": [
        {"kind": "identifier", "query": "CVE-2024-11111", "chunk_id": kb.chunk_id(docs["sqli.md"].id, 0), "source": "sqli.md"},
        {"kind": "question", "query": "Which page renders a field unescaped?", "chunk_id": kb.chunk_id(docs["xss.md"].id, 0), "source": "xss.md"}]}
    result = await ragtest.run(conn, test)
    assert set(result["halves"]) == {"identifier", "question"} and all(sum(h.values()) == 1 for h in result["halves"].values())
    assert {k: sum(h[k] for h in result["halves"].values()) for k in result["found_by"]} == result["found_by"]
    said = ragtest.report(test, result)
    assert "Identifiers: found by both halves of the search: " in said and "Questions: found by both halves of the search: " in said


# ---- features are in the code: questions about source code somebody wrote

@pytest.mark.parametrize("uri", [
    "git:github.com/usestrix/strix/strix/tools/browser.py", "/home/u/artex/server/src/agent/loop.ts", "/home/u/app/web/src/components/Chat.tsx",
    "upload:tool/main.go", "/x/src.zip!/src/lib.rs", "git:github.com/o/r/scripts/build.sh",
])
def test_source_code_somebody_wrote(uri):
    assert ragtest.source_code(uri) and not ragtest.readable(uri)


@pytest.mark.parametrize("uri", [
    "/home/u/web/src/types.d.ts", "/x/pkg.tgz!/package/dist/client.d.mts",                 # declarations a compiler wrote
    "/home/u/web/i18n/locales/ar.ts", "git:github.com/o/r/src/translations/de.js",       # tables of translations
    "/home/u/web/package.json", "/home/u/web/config.yaml", "/home/u/db/schema.sql", "/home/u/data/rows.csv",      # data and settings
    "/home/u/web/.next/server/app/page.js", "/home/u/web/public/app.min.js",              # left out of a collection today
    "/home/u/notes/report.md", "https://example.com/static/app.js",                        # no code, and a page of a crawl
])
def test_what_is_no_source_code_to_ask_about(uri):
    assert not ragtest.source_code(uri)


CODE = ('def take_screenshot(page, full=False):\n    """Take a picture of the page the agent is looking at and give it back as PNG bytes. '
        'With full set, the whole page is taken and not only the part in view, which costs more of the context of the model that '
        'reads it and is therefore off unless the caller asks for it."""\n    return page.screenshot(full_page=full)\n')


async def test_questions_about_code_are_written_from_source_code_and_counted_apart(conn, col, tmp_path):
    table = "export default {\n" + "".join(f"  greeting_{i}: 'A translated line of the interface, number {i}.',\n" for i in range(12)) + "}\n"
    await ingest_texts(conn, col, tmp_path, {"report.md": OWN, "strix/tools/browser.py": CODE, "i18n/locales/en.ts": table,
                                             "types.d.ts": "export declare function shot(page: Page, full?: boolean): Promise<Buffer>;\n" * 8})
    d = await decider(conn, "When did the pentest of the staging site start?", "How does the browser tool of strix take a picture of a page?", "unused")
    test = await ragtest.build(conn, col, identifiers=0, questions=1, code=2)
    assert [(i["kind"], Path(i["source"]).name) for i in test["items"]] == [("question", "report.md"), ("code", "browser.py")]
    assert len(d.calls) == 2 and "strix/tools/browser.py" in d.calls[1]["messages"][0].blocks[0].text
    assert (test["prose"], test["code"]) == (1, 1)
    result = await ragtest.run(conn, test)
    assert (result["kinds"]["question"]["asked"], result["kinds"]["code"]["asked"], result["kinds"]["code"]["pool"]) == (1, 1, 1)
    said = ragtest.report(test, result)
    assert "asked 1 questions and 1 questions about code" in said and "\nquestions about code " in said
    assert "and from source code somebody wrote" in said


async def test_a_collection_of_code_only_gets_questions_about_code(conn, col, tmp_path):
    await ingest_texts(conn, col, tmp_path, {"strix/tools/browser.py": CODE})
    d = await decider(conn, "How does the browser tool of strix take a picture of a page?")
    test = await ragtest.build(conn, col, identifiers=0, questions=2)                     # half as many about code, unless said otherwise
    assert [i["kind"] for i in test["items"]] == ["code"] and len(d.calls) == 1 and (test["prose"], test["code"]) == (0, 1)
    said = ragtest.report(test, await ragtest.run(conn, test))
    assert "the questions are about source code only" in said and "No question was written" not in said


async def test_no_questions_means_no_model_is_asked_about_code_either(conn, col, tmp_path, capsys):
    await ingest_texts(conn, col, tmp_path, {"strix/tools/browser.py": CODE + "screenshot_tool_name = 1\n"})
    d = await decider(conn, "unused")
    assert await ragtest.main([col.name, "--new", "--questions", "0"]) == 0
    assert d.calls == [] and "identifiers" in capsys.readouterr().out
    assert await ragtest.main([col.name, "--new", "--questions", "0", "--code", "1"]) == 0     # unless that is asked for
    assert len(d.calls) == 1 and "questions about code" in capsys.readouterr().out


async def test_a_question_about_code_is_answered_by_a_twin_as_any_question_is(conn, col, tmp_path, monkeypatch):
    docs = await ingest_texts(conn, col, tmp_path, {"a/browser.py": CODE, "b/browser.py": CODE.replace("PNG", "JPEG")})
    ids = {name: kb.chunk_id(doc.id, 0) for name, doc in docs.items()}
    text = (await kb.get_chunk(conn, ids["b/browser.py"])).text

    async def found(conn, collections, query, limit=8, **kw):
        return [hit(ids["b/browser.py"], text, source="b/browser.py")]
    monkeypatch.setattr(ragtest.search, "search", found)
    test = {"collection": col.id, "name": col.name, "created": "t", "written_by": "m", "items": [
        {"kind": "code", "query": "How is a picture of the page taken?", "chunk_id": ids["a/browser.py"], "source": "a/browser.py"}]}
    result = await ragtest.run(conn, test)
    assert result["kinds"]["code"] == {"asked": 1, "first": 1, "sent": 1, "pool": 1, "missed": 0} and result["twins"] == 1
