from hunterai.providers.base import TextDelta, ToolCall
from hunterai.tools import loopguard, registry
from hunterai.tools.base import ToolResult
from tests.test_engine import go, setup, use  # noqa: F401 (setup is a fixture)
from tests.toolkit import Turns, fake_tool


def tc(n, name="web_fetch", **args):
    return ToolCall(f"c{n}", name, args)


def test_the_same_failing_call_is_refused_the_third_time():
    g = loopguard.Guard()
    call = dict(url="https://demo.test/a")
    for n in (1, 2):
        assert g.before(tc(n, **call)) is None
        g.after(tc(n, **call), "HTTP 503 https://demo.test/a\n\nApplication Error", False)
    said = g.before(tc(3, **call))
    assert said and "2 times" in said and "same" in said and "do not repeat" in said.lower()
    # A different call is not held up, and neither is the same call once it has worked.
    assert g.before(tc(4, url="https://demo.test/b")) is None
    g2 = loopguard.Guard()
    g2.after(tc(1, **call), "HTTP 503 x", False); g2.after(tc(2, **call), "HTTP 200 x\n\nok", False)
    assert g2.before(tc(3, **call)) is None


def test_a_call_that_keeps_succeeding_may_be_repeated():
    g = loopguard.Guard()
    for n in range(6):                                        # polling a process, reading a page again: not a loop of failures
        assert g.before(tc(n, "process_output", id="p1")) is None
        g.after(tc(n, "process_output", id="p1"), "", False)


def test_a_host_that_keeps_failing_is_named_and_then_left_alone():
    g = loopguard.Guard()
    notes = []
    for n, path in enumerate("abcdefgh"):
        call = tc(n, url=f"https://demo.test/{path}")
        assert g.before(call) is None
        notes.append(g.after(call, "Status: 503\nApplication Error" if n % 2 else "HTTP 503 https://demo.test/x", False))
    assert notes[:3] == ["", "", ""] and "demo.test has failed 4 times in a row" in notes[3] and "8 times" in notes[7]
    said = g.before(tc(9, "browser_navigate", url="https://demo.test/again"))
    assert said and "demo.test" in said and "not run" in said.lower()
    assert g.before(tc(10, url="https://other.test/")) is None                   # another host is unaffected
    # One answer from the host and it is trusted again.
    g = loopguard.Guard()
    for n in range(5):
        g.after(tc(n, url=f"https://demo.test/{n}"), "HTTP 502 x", False)
    g.after(tc(6, url="https://demo.test/ok"), "HTTP 200 https://demo.test/ok\n\nhello", False)
    assert g.after(tc(7, url="https://demo.test/z"), "HTTP 503 x", False) == ""


def test_what_counts_as_a_failure():
    for text, error in (("HTTP 503 https://a.test/", False), ("Status: 502\n", False), ("Recorded in Caido as request #9.\nHTTP/1.1 503 Service Unavailable", False),
                        ("HTTP 429 https://a.test/", False), ("request to a.test timed out", True), ("anything", True)):
        assert loopguard.failed(text, error) is True, text
    for text in ("HTTP 200 https://a.test/\n\nthe page says 503 somewhere", "Status: 404\n", "HTTP 403 x", ""):
        assert loopguard.failed(text, False) is False, text
    assert loopguard.host_of({"url": "https://Demo.Test:8443/a?b"}) == "demo.test" and loopguard.host_of({"host": "A.test", "raw": "GET / HTTP/1.1"}) == "a.test"
    assert loopguard.host_of({"command": "curl https://a.test"}) is None and loopguard.host_of({"url": "not a url"}) is None


async def test_a_run_does_not_hammer_a_dead_host(conn, setup):
    tool, ran = fake_tool("web_fetch", risk="read", text="HTTP 503 https://demo.test/a\n\nApplication Error",
                          schema={"type": "object", "properties": {"url": {"type": "string"}}})
    registry.register(tool)
    same = ToolCall("c", "web_fetch", {"url": "https://demo.test/a"})
    import dataclasses
    p = Turns("abliteration", *[[dataclasses.replace(same, id=f"c{i}")] for i in range(4)], [TextDelta("The demo is down.")])
    use(abliteration=p)
    run, msg = await go(conn, *await setup())
    assert run.status == "done" and len(ran) == 2                                # the third and fourth were not sent
    results = [b for b in msg.blocks if b.type == "tool_result"]
    assert [r.is_error for r in results] == [False, False, True, True] and "same" in results[2].content
