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


# ---- commands. Reported: a local model that cannot get a command to work sends the same command again and
# again. A command that ends with an error code was no failure to the guard (the tool had run), and no two of its
# results were the same, because each says how long it took.

def ran(n, seconds, code=2, out="make: *** No targets specified and no makefile found.  Stop."):
    """A result of `execute`, as tools/shell.py writes it."""
    return tc(n, "execute", command="make"), f"exit code: {code}\nduration: {seconds}s\ncwd: /work\n\n{out}", code


def test_a_command_that_ends_with_an_error_code_twice_is_not_run_a_third_time():
    g = loopguard.Guard()
    for n, seconds in ((1, "0.1"), (2, "0.4")):
        call, text, code = ran(n, seconds)
        assert g.before(call) is None
        assert g.after(call, text, False, code) == ""
    said = g.before(tc(3, "execute", command="make"))
    assert said and "2 times" in said and "do not repeat" in said.lower()
    assert g.before(tc(4, "execute", command="make -n")) is None                 # another command is not held up


def test_a_command_that_ends_well_or_says_something_else_is_no_repeated_failure():
    g = loopguard.Guard()
    for n in range(1, 6):                                                        # code 0: it may be run as often as the model likes
        call, text, code = ran(n, "0.2", code=0, out="nothing to be done")
        assert g.before(call) is None
        g.after(call, text, False, code)
    g = loopguard.Guard()
    for n in range(1, 6):                                                        # an error each time, and each time another
        call, text, code = ran(n, "0.2", out=f"attempt {n} failed")
        assert g.before(call) is None, n
        g.after(call, text, False, code)
    assert loopguard.failed("exit code: 1\nduration: 0.1s", False, 1) and not loopguard.failed("exit code: 0\n", False, 0)
    assert not loopguard.failed("killed", False, None) and loopguard.failed("x", True, 0)


def test_two_failing_calls_taken_in_turns_are_each_refused_the_third_time():
    """A, B, A, B: neither is the call before it, and each has failed the same way twice."""
    g = loopguard.Guard()
    a, b = dict(command="make"), dict(command="./configure")
    for n in range(2):
        for call, out in ((a, "no makefile"), (b, "not found")):
            assert g.before(tc(n, "execute", **call)) is None
            g.after(tc(n, "execute", **call), f"exit code: 1\nduration: 0.{n}s\n\n{out}", False, 1)
    assert g.before(tc(5, "execute", **a)) and g.before(tc(6, "execute", **b))
    # A call that worked in between: something has changed, and both may be tried again.
    g.after(tc(7, "write_file", path="Makefile"), "written", False)
    assert g.before(tc(8, "execute", **a)) is None and g.before(tc(9, "execute", **b)) is None


def test_a_model_that_keeps_sending_what_was_refused_is_stuck():
    g = loopguard.Guard()
    for n, seconds in ((1, "0.1"), (2, "0.2")):
        call, text, code = ran(n, seconds)
        g.after(call, text, False, code)
    assert g.stuck() is None
    for n in range(loopguard.GIVE_UP):
        assert g.stuck() is None, n
        assert g.before(tc(10 + n, "execute", command="make")) is not None
    said = g.stuck()
    assert said and "execute" in said and "make" in said and str(loopguard.GIVE_UP) in said
    # A call that runs in between is a sign of life, also when it fails: the refusals are counted from there again.
    g = loopguard.Guard()
    for n, seconds in ((1, "0.1"), (2, "0.2")):
        call, text, code = ran(n, seconds)
        g.after(call, text, False, code)
    assert g.before(tc(3, "execute", command="make")) and g.before(tc(4, "execute", command="make"))
    g.after(tc(5, "execute", command="ls build"), "exit code: 2\nduration: 0.1s\n\nls: cannot access 'build'", False, 2)
    assert g.before(tc(6, "execute", command="make")) and g.before(tc(7, "execute", command="make"))
    assert g.stuck() is None
    assert g.before(tc(8, "execute", command="make")) and g.stuck()


async def test_a_run_ends_when_the_model_keeps_sending_a_command_that_fails(conn, setup):
    """The whole of it: eight times the same command. It is run twice, refused three times, and the run ends
    there with a word for the owner, instead of going on to the limit of turns."""
    import dataclasses
    ran_times = []

    async def fails(ctx, args):
        ran_times.append(args)
        return ToolResult(f"exit code: 127\nduration: 0.{len(ran_times)}s\ncwd: /work\n\nsh: 1: frobnicate: not found",
                          meta={"exit_code": 127})
    tool, _ = fake_tool("execute", risk="read", schema={"type": "object", "properties": {"command": {"type": "string"}}})
    registry.register(dataclasses.replace(tool, handler=fails))
    same = ToolCall("c", "execute", {"command": "frobnicate --all"})
    p = Turns("abliteration", *[[dataclasses.replace(same, id=f"c{i}")] for i in range(8)], [TextDelta("never said")])
    use(abliteration=p)
    run, msg = await go(conn, *await setup())
    assert len(ran_times) == 2 and len(p.calls) == 5                             # two that ran, three that were refused
    results = [b for b in msg.blocks if b.type == "tool_result"]
    assert [r.is_error for r in results] == [False, False, True, True, True] and "Not run" in results[2].content
    assert run.status == "error" and "frobnicate --all" in run.error and "refused" in run.error
