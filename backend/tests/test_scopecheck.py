"""Which line of a target's scope lists decides for one host, address or URL, in words for the owner.

A call is asked about with "x is not in this target's in-scope list" or "x is on this target's out-of-scope list", and
the owner cannot see which line did that. The target form answers it for a host he types: the verdict the permission
check itself gives (tools/resolve.py), the line that decides, and what stands in the way. It reads the two lists
only: nothing is saved, and no name is looked up (conftest fails any test that reaches the resolver)."""
import functools
import itertools

import pytest

from hunterai import scopecheck as sc
from hunterai.tools import resolve
from hunterai.tools.base import HostUse
from hunterai.types import Target


def target(ins, outs) -> Target:
    return Target(id="t", name="t", notes="", scope={"in": list(ins), "out": list(outs)}, created_at="")


def told(e) -> tuple:
    return e.verdict, e.line


IN_A_COMMAND = "A command is checked without the path of an address it names, so a line with a path counts for the whole host there."


# ---------------------------------------------------------------- in scope

def test_a_name_under_a_wildcard_is_in_scope_and_the_line_is_named():
    e = sc.explain(["api.example.com", "*.example.com"], [], "shop.example.com")
    assert told(e) == ("in", "*.example.com")
    assert e.says == "shop.example.com is in scope: the In scope line *.example.com covers it."
    assert e.hints == [] and e.unreadable == []


def test_a_line_that_names_the_host_is_shown_before_a_wildcard_that_covers_it():
    e = sc.explain(["*.example.com", "shop.example.com"], [], "shop.example.com")
    assert told(e) == ("in", "shop.example.com")
    assert e.says == "shop.example.com is in scope: the In scope line shop.example.com names it."


def test_of_two_lines_that_cover_a_host_alike_the_first_is_shown():
    assert told(sc.explain(["*.example.com", "*.shop.example.com"], [], "a.shop.example.com")) == ("in", "*.example.com")
    assert told(sc.explain([], ["*.example.com", "*.shop.example.com"], "a.shop.example.com")) == ("out", "*.example.com")
    lifted = sc.explain(["a.shop.example.com"], ["*.shop.example.com/admin/*", "*.shop.example.com", "*.example.com"], "a.shop.example.com")
    assert lifted.hints[0].startswith("The Out of scope line *.shop.example.com covers it as well.")


def test_the_line_shown_for_a_path_is_one_that_covers_it_as_written():
    """Out of scope reads a path loosely, so that a near miss still warns. In scope does not."""
    e = sc.explain(["shop.example.com/Admin", "*.example.com/admin"], [], "https://shop.example.com/admin/x")
    assert told(e) == ("in", "*.example.com/admin")


def test_a_url_is_read_as_its_host_port_and_path():
    e = sc.explain(["api.example.com/v2/*"], [], "https://API.example.com/v2/users?id=1#x")
    assert (e.verdict, e.line, e.host, e.port, e.path) == ("in", "api.example.com/v2/*", "api.example.com", 443, "/v2/users")
    assert e.hints == []
    e = sc.explain(["api.example.com/v2/*"], [], "http://user:secret@api.example.com/v2")
    assert (e.verdict, e.host, e.port, e.path) == ("in", "api.example.com", 80, "/v2")


def test_a_host_typed_without_a_path_is_covered_by_a_line_with_a_path_and_told_what_that_line_leaves_out():
    e = sc.explain(["api.example.com/v2/*"], [], "api.example.com")
    assert told(e) == ("in", "api.example.com/v2/*")
    assert e.hints == [("That line is only for /v2 and what is under it: a full address of api.example.com with another path "
                        "is asked about.")]
    whole = sc.explain(["api.example.com/v2/*", "api.example.com"], [], "api.example.com")
    assert told(whole) == ("in", "api.example.com") and whole.hints == []


def test_a_host_named_in_scope_goes_before_a_wider_out_of_scope_line():
    e = sc.explain(["*.example.com", "shop.example.com"], ["*.example.com"], "shop.example.com")
    assert told(e) == ("in", "shop.example.com")
    assert e.says == "shop.example.com is in scope: the In scope line shop.example.com names it."
    assert e.hints == [("The Out of scope line *.example.com covers it as well. A host that In scope names goes before a wider "
                        "Out of scope line.")]


def test_the_line_that_lifted_a_host_is_shown_even_where_a_wildcard_covers_more_of_it():
    e = sc.explain(["*.example.com", "shop.example.com/app"], ["*.example.com"], "shop.example.com")
    assert told(e) == ("in", "shop.example.com/app")
    assert e.says == "shop.example.com is in scope: the In scope line shop.example.com/app names it."
    assert told(sc.explain(["*.example.com", "shop.example.com/app"], ["*.example.com"], "https://shop.example.com/other")) == \
        ("out", "*.example.com")


# ---------------------------------------------------------------- not on the list

def test_the_bare_domain_is_not_under_its_wildcard_and_the_hint_says_what_to_write():
    e = sc.explain(["*.example.com"], [], "example.com")
    assert told(e) == ("not_in", None)
    assert e.says == "example.com is not on the In scope list, so a call that names it is asked about."
    assert e.hints == [("The In scope line *.example.com covers the names under example.com, not example.com itself. If the "
                        "program covers example.com too, add it on a line of its own.")]


def test_a_name_under_a_host_that_is_listed_alone_is_not_covered():
    for ask in ("www.example.com", "a.b.example.com"):
        e = sc.explain(["example.com"], [], ask)
        assert told(e) == ("not_in", None)
        assert e.hints == [("The In scope line example.com names that one host. If the program covers the names under it too, "
                            "add *.example.com on a line of its own.")]


def test_a_host_that_only_ends_like_a_listed_one_gets_no_advice():
    assert sc.explain(["example.com"], [], "notexample.com").hints == []
    assert sc.explain(["*.example.com"], [], "notexample.com").hints == []
    assert sc.explain(["example.com"], [], "example.com.evil.test").hints == []


def test_another_path_than_a_line_allows_is_not_covered():
    e = sc.explain(["api.example.com/v2/*"], [], "https://api.example.com/v1/users")
    assert told(e) == ("not_in", None)
    assert e.hints == [("The In scope line api.example.com/v2/* is only for /v2 and what is under it, and this has the path "
                        "/v1/users."), IN_A_COMMAND]
    assert e.command == {"verdict": "in", "says": "In a command, api.example.com is in scope: the In scope line api.example.com/v2/* names it."}


def test_a_line_with_a_port_is_only_for_that_port():
    e = sc.explain(["example.com:8443"], [], "https://example.com/")
    assert told(e) == ("not_in", None)
    assert e.hints == ["The In scope line example.com:8443 is only for port 8443, and this goes to port 443."]
    bare = sc.explain(["example.com:8443"], [], "example.com")
    assert bare.verdict == "not_in"
    assert bare.hints == ["The In scope line example.com:8443 is only for port 8443, and this names no port."]
    assert sc.explain(["example.com:8443"], [], "https://example.com:8443/").verdict == "in"
    assert (typed := sc.explain(["example.com:8443"], [], "example.com:8443")).verdict == "in"
    assert (typed.host, typed.port, typed.path) == ("example.com", 8443, None)


def test_a_wrong_port_is_said_before_a_wrong_path():
    e = sc.explain(["example.com:8443/api/*"], [], "https://example.com/other")
    assert e.hints == ["The In scope line example.com:8443/api/* is only for port 8443, and this goes to port 443."]
    e = sc.explain(["example.com:8443/api/*"], [], "https://example.com:8443/other")
    assert e.hints[0] == ("The In scope line example.com:8443/api/* is only for /api and what is under it, and this has the "
                          "path /other.")


def test_many_lines_that_come_close_are_counted_after_the_first_three():
    lines = [f"api.example.com/v{n}/*" for n in range(1, 6)]
    e = sc.explain(lines, [], "https://api.example.com/v9/x")
    assert e.hints == [f"The In scope line api.example.com/v{n}/* is only for /v{n} and what is under it, and this has the "
                       "path /v9/x." for n in (1, 2, 3)] + ["2 more In scope lines come close in the same way.", e.hints[-1]]
    assert e.hints[-1] == IN_A_COMMAND and e.command["verdict"] == "in"
    e = sc.explain(lines[:4], [], "https://api.example.com/v9/x")
    assert e.hints[3:-1] == ["1 more In scope line comes close in the same way."]
    assert len(sc.explain(lines[:3], [], "https://api.example.com/v9/x").hints[:-1]) == 3
    assert len(sc.explain([lines[0], lines[0], lines[0], lines[0]], [], "https://api.example.com/v9/x").hints[:-1]) == 1   # one line, once


# ---------------------------------------------------------------- out of scope

def test_a_wider_out_of_scope_line_covers_a_host_and_the_hint_says_how_a_program_takes_one_out_of_it():
    e = sc.explain(["*.example.com"], ["*.internal.example.com"], "a.internal.example.com")
    assert told(e) == ("out", "*.internal.example.com")
    assert e.says == "a.internal.example.com is out of scope: the Out of scope line *.internal.example.com covers it."
    assert e.hints == [("If the program lists a.internal.example.com itself as in scope, write a.internal.example.com on a "
                        "line of its own in In scope: a host named there goes before a wider Out of scope line.")]


def test_a_host_named_on_both_lists_is_out():
    e = sc.explain(["shop.example.com"], ["*.example.com", "shop.example.com"], "shop.example.com")
    assert told(e) == ("out", "shop.example.com")
    assert e.says == "shop.example.com is out of scope: the Out of scope line shop.example.com names it."
    assert e.hints == ["The In scope line shop.example.com names it too. A host named on both lists is out."]


def test_an_out_of_scope_line_with_a_path_puts_the_whole_host_out_for_a_command():
    """A command names a host and no path, so the permission check cannot tell that it stays clear of /blog."""
    e = sc.explain(["*.acme.com"], ["www.acme.com/blog"], "www.acme.com")
    assert told(e) == ("out", "www.acme.com/blog")
    assert e.says == "www.acme.com is out of scope: the Out of scope line www.acme.com/blog names it."
    assert e.hints == [("That line is only for /blog and what is under it. A command names a host without a path, so every "
                        "command that names www.acme.com is asked about. To check one path, type the full address, like "
                        "https://www.acme.com/login.")]
    assert told(sc.explain(["*.acme.com"], ["www.acme.com/blog"], "https://www.acme.com/shop")) == ("in", "*.acme.com")
    near = sc.explain(["*.acme.com"], ["www.acme.com/blog"], "https://www.acme.com/Blog//post")    # a near miss still warns
    assert told(near) == ("out", "www.acme.com/blog") and near.hints == []


def test_the_example_address_for_an_ipv6_host_is_one_a_browser_would_take():
    e = sc.explain([], ["[2001:db8::1]/admin"], "2001:db8::1")
    assert told(e) == ("out", "[2001:db8::1]/admin")
    assert e.hints == [("That line is only for /admin and what is under it. A command names a host without a path, so every "
                        "command that names 2001:db8::1 is asked about. To check one path, type the full address, like "
                        "https://[2001:db8::1]/login.")]


def test_a_wildcard_with_a_path_on_the_out_list_gets_no_advice_that_would_lift_the_path_too():
    e = sc.explain(["*.acme.com"], ["*.acme.com/admin/*"], "a.acme.com")
    assert told(e) == ("out", "*.acme.com/admin/*")
    assert e.says == "a.acme.com is out of scope: the Out of scope line *.acme.com/admin/* covers it."
    assert e.hints == [("That line is only for /admin and what is under it. A command names a host without a path, so every "
                        "command that names a.acme.com is asked about. To check one path, type the full address, like "
                        "https://a.acme.com/login.")]
    assert sc.explain(["*.acme.com"], ["*.acme.com/admin/*"], "https://a.acme.com/admin/users").hints == []


def test_the_line_without_a_path_decides_where_one_with_a_path_hits_too():
    e = sc.explain([], ["*.acme.com/admin/*", "*.acme.com"], "a.acme.com")
    assert told(e) == ("out", "*.acme.com")
    assert e.hints == [("If the program lists a.acme.com itself as in scope, write a.acme.com on a line of its own in In scope: "
                        "a host named there goes before a wider Out of scope line.")]


def test_a_line_that_names_the_host_decides_before_a_wildcard_because_nothing_lifts_it():
    e = sc.explain([], ["*.acme.com", "www.acme.com/blog"], "www.acme.com")
    assert told(e) == ("out", "www.acme.com/blog")
    assert e.hints == []          # the wildcard covers every path, so the whole host is out whatever is typed


def test_a_host_that_is_out_and_not_listed_either_is_told_both():
    e = sc.explain(["*.acme.com"], ["acme.com/careers"], "acme.com")
    assert told(e) == ("out", "acme.com/careers")
    assert e.hints[-1] == "It is not on the In scope list either."
    assert len(e.hints) == 2
    assert "It is not on the In scope list either." not in sc.explain([], ["acme.com"], "acme.com").hints


def test_a_full_address_is_told_when_a_command_that_names_it_gets_another_answer():
    """web_fetch and the browser hand the check the path of the address. A command does not: the hosts it names are
    checked without one, so a line with a path counts for the whole host there."""
    e = sc.explain(["*.acme.com"], ["www.acme.com/blog"], "https://www.acme.com/shop")
    assert told(e) == ("in", "*.acme.com")
    assert e.hints == [IN_A_COMMAND]
    assert e.command == {"verdict": "out", "says": "In a command, www.acme.com is out of scope: the Out of scope line www.acme.com/blog names it."}
    lifted = sc.explain(["a.acme.com/app"], ["*.acme.com/admin/*"], "https://a.acme.com/admin/x")
    assert told(lifted) == ("out", "*.acme.com/admin/*")
    assert lifted.hints == ["It is not on the In scope list either.", IN_A_COMMAND]
    assert lifted.command == {"verdict": "in", "says": "In a command, a.acme.com is in scope: the In scope line a.acme.com/app names it."}
    same = sc.explain(["*.acme.com"], ["www.acme.com/blog"], "https://www.acme.com/blog/post")
    assert told(same) == ("out", "www.acme.com/blog") and same.hints == [] and same.command is None
    assert sc.explain(["*.acme.com"], ["www.acme.com/blog"], "www.acme.com/shop").command["verdict"] == "out"     # a path, no scheme
    assert sc.explain(["*.acme.com"], ["www.acme.com/blog"], "www.acme.com").command is None                      # a host alone is the command's way


# ---------------------------------------------------------------- no list, this machine, addresses

def test_an_empty_in_scope_list_leaves_only_the_out_list():
    e = sc.explain([], ["pay.example.com"], "shop.example.com")
    assert told(e) == ("no_list", None)
    assert e.says == "The In scope list is empty, so only the Out of scope list is checked, and shop.example.com is not on it."
    assert e.hints == [("Auto-approve answers only for hosts on an In scope list: while this one is empty, it asks you about "
                        "every call that names a host.")]
    assert told(sc.explain([], ["pay.example.com"], "pay.example.com")) == ("out", "pay.example.com")


@pytest.mark.parametrize("ask,host", [("localhost", "localhost"), ("http://127.0.0.1:8790/api", "127.0.0.1"),
                                      ("app.localhost", "app.localhost"), ("[::1]:8080", "::1")])
def test_this_machine_is_not_a_matter_of_the_in_scope_list(ask, host):
    e = sc.explain(["*.example.com"], [], ask)
    assert told(e) == ("local", None) and e.host == host
    assert e.says == f"{host} is this machine. The In scope list is not asked about it."
    assert e.hints == [("A call to this machine can still be asked about as a local address: that check is apart from the "
                        "scope lists.")]


def test_this_machine_is_still_out_when_the_out_list_names_it_and_in_when_the_in_list_does():
    assert told(sc.explain(["*.example.com"], ["localhost"], "localhost")) == ("out", "localhost")
    assert told(sc.explain(["localhost"], [], "localhost")) == ("in", "localhost")


def test_an_address_is_checked_against_addresses_and_networks():
    e = sc.explain(["10.0.0.0/24"], [], "10.0.0.7")
    assert told(e) == ("in", "10.0.0.0/24") and e.says == "10.0.0.7 is in scope: the In scope line 10.0.0.0/24 covers it."
    assert sc.explain(["10.0.0.7"], [], "10.0.0.7").says == "10.0.0.7 is in scope: the In scope line 10.0.0.7 names it."
    assert told(sc.explain(["10.0.0.0/24"], [], "http://10.0.1.7/")) == ("not_in", None)
    assert sc.explain(["10.0.0.0/24"], [], "http://10.0.1.7/").hints == []
    lifted = sc.explain(["10.0.0.9"], ["10.0.0.0/24"], "10.0.0.9")
    assert told(lifted) == ("in", "10.0.0.9")
    assert lifted.hints == [("The Out of scope line 10.0.0.0/24 covers it as well. A host that In scope names goes before a wider "
                             "Out of scope line.")]
    wide = sc.explain([], ["10.0.0.0/24"], "10.0.0.9")
    assert told(wide) == ("out", "10.0.0.0/24")
    assert wide.hints == [("If the program lists 10.0.0.9 itself as in scope, write 10.0.0.9 on a line of its own in In scope: "
                           "a host named there goes before a wider Out of scope line.")]
    assert sc.explain(["10.0.0.7:8080"], [], "10.0.0.7").hints == [
        "The In scope line 10.0.0.7:8080 is only for port 8080, and this names no port."]


def test_no_name_is_looked_up_and_the_hint_says_what_address_lines_do_with_a_name():
    e = sc.explain(["10.0.0.0/24", "203.0.113.5"], [], "intranet.example.com")
    assert told(e) == ("not_in", None)
    assert e.hints == [("In scope lines that are addresses or networks (10.0.0.0/24, 203.0.113.5) cover a call that names the "
                        "address. No name is looked up for them before you are asked.")]
    e = sc.explain(["*.example.com"], ["10.0.0.9", "192.168.0.0/16", "fd00::/8", "172.16.0.0/12", "10.9.9.9"], "intranet.example.com")
    assert told(e) == ("in", "*.example.com")
    assert e.hints == [("Out of scope lines that are addresses or networks (10.0.0.9, 192.168.0.0/16, fd00::/8 and 2 more) are "
                        "checked against the addresses of a name once a tool that fetches a URL has looked it up. No name is "
                        "looked up here, and none is for a command.")]
    three = sc.explain(["10.0.0.1", "10.0.0.2", "10.0.0.3", "10.0.0.3"], [], "intranet.example.com")       # a line twice is one line
    assert three.hints == [("In scope lines that are addresses or networks (10.0.0.1, 10.0.0.2, 10.0.0.3) cover a call that names "
                            "the address. No name is looked up for them before you are asked.")]
    empty = sc.explain([], ["10.0.0.9"], "intranet.example.com")
    assert empty.verdict == "no_list" and len(empty.hints) == 2 and empty.hints[1].startswith("Out of scope lines that are addresses")
    assert sc.explain(["*.example.com"], ["10.0.0.9"], "10.0.0.8").hints == []          # an address needs no lookup
    out = sc.explain(["10.0.0.0/24"], ["*.example.com", "10.0.0.9"], "a.example.com")          # out already: no lookup would change it
    assert out.verdict == "out" and not [x for x in out.hints if "addresses or networks" in x]


def test_lines_that_cannot_be_read_are_listed_with_what_is_wrong_with_them():
    e = sc.explain(["example.com (main site)", "*.example.com"], ["10.0.0.1-10.0.0.9"], "example.com")
    assert e.unreadable == [
        {"side": "in", "line": "example.com (main site)", "why": "one host, address or network on a line, with nothing after it"},
        {"side": "out", "line": "10.0.0.1-10.0.0.9", "why": "write a range of addresses as networks, like 10.0.0.0/26"}]
    assert e.verdict == "not_in"
    only = sc.explain(["example.*"], [], "example.com")
    assert only.verdict == "not_in" and only.hints == [] and [p["line"] for p in only.unreadable] == ["example.*"]


def test_the_lists_are_read_as_the_form_gives_them():
    e = sc.explain(["  *.example.com  ", "", "   ", None, 7], ["", " "], "  shop.example.com \n")
    assert told(e) == ("in", "*.example.com")
    assert sc.explain(["", "  "], [], "shop.example.com").verdict == "no_list"


# ---------------------------------------------------------------- what is typed

@pytest.mark.parametrize("ask", ["", "   ", "two words", "ftp://example.com/x", "http://", "exa mple.com", "*.example.com",
                                 "user@example.com", "example.com:99999", "example.com:65536", "example.com:0", "example.com:",
                                 "://x", "//example.com/x", "[::1]x:80", "https://shop.example.com/login and more",
                                 "shop.example.com is not in this target's in-scope list", None, 7])
def test_what_is_no_host_address_or_url_is_refused_with_an_example(ask):
    with pytest.raises(ValueError) as said:
        sc.explain(["*.example.com"], [], ask)
    assert str(said.value) == "Type one host, address or URL, like shop.example.com or https://shop.example.com/login."


def test_an_address_of_any_usual_length_is_read_and_a_longer_text_is_refused():
    longest = "https://shop.example.com/" + "a" * (sc.MAX_ASK - len("https://shop.example.com/"))
    assert len(longest) == sc.MAX_ASK == 2000
    assert sc.explain(["*.example.com"], [], longest).verdict == "in"
    with pytest.raises(ValueError, match="Type one host"):
        sc.explain(["*.example.com"], [], longest + "a")


def test_a_path_that_tools_refuse_is_named_and_the_host_can_be_checked_alone():
    with pytest.raises(ValueError) as said:
        sc.explain(["*.example.com"], [], "https://shop.example.com/a%2Fb")
    assert str(said.value) == ("The path of this address cannot be read (encoded dots or slashes in a URL path are not "
                               "supported). Type the host alone to check it.")
    with pytest.raises(ValueError, match="The path of this address cannot be read"):
        sc.explain(["*.example.com"], [], "shop.example.com/a%2e%2e/b")


@pytest.mark.parametrize("ask,host,port,path", [
    ("Shop.Example.COM.", "shop.example.com", None, None), ("shop.example.com/login?next=/x", "shop.example.com", None, "/login"),
    ("shop.example.com:8443/a/../b", "shop.example.com", 8443, "/b"), ("[2001:db8::1]:8443", "2001:db8::1", 8443, None),
    ("2001:db8::1", "2001:db8::1", None, None), ("[2001:db8::1]", "2001:db8::1", None, None), ("https://[2001:db8::1]/x", "2001:db8::1", 443, "/x"),
    ("bücher.example", "xn--bcher-kva.example", None, None), ("2130706433", "127.0.0.1", None, None), ("shop.example.com/", "shop.example.com", None, "/"),
    ("example.com:65535", "example.com", 65535, None), ("example.com:1", "example.com", 1, None), ("10.0.0.7:8080", "10.0.0.7", 8080, None),
    ("HTTPS://Shop.Example.COM/X", "shop.example.com", 443, "/X"), ("[2001:DB8::1]:8443", "2001:db8::1", 8443, None),
])
def test_a_host_is_read_in_the_forms_a_call_can_name_it(ask, host, port, path):
    e = sc.explain([], [], ask)
    assert (e.host, e.port, e.path) == (host, port, path)


# ---------------------------------------------------------------- the same verdict as the permission check, and advice that works

LINES = ["*.example.com", "example.com", "shop.example.com", "shop.example.com:8443", "shop.example.com/admin/*",
         "*.example.com/admin/*", "10.0.0.0/24", "10.0.0.9", "localhost", "not a line"]
ASKS = ["example.com", "shop.example.com", "a.shop.example.com", "shop.example.com:8443", "https://shop.example.com/",
        "https://shop.example.com/admin/x", "https://shop.example.com/Admin//x", "http://shop.example.com:8443/admin",
        "10.0.0.9", "http://10.0.1.1/x", "localhost", "other.test"]


def kind(v) -> str:
    return "out" if v.out else "asked" if v.not_in else "through"


def _lists():
    few = [(), *((x,) for x in LINES)]
    pairs = [*few, *itertools.combinations(LINES, 2)]
    yield from itertools.product(pairs, few)
    yield from itertools.product(few, pairs)


def test_every_answer_agrees_with_the_permission_check_and_every_piece_of_advice_works(monkeypatch):
    """Said twice would be said differently one day. Over every pairing of these lines and hosts: the verdict is the one
    tools/resolve.py gives, the line shown is one that matches, and a line the hint says to add does what it says."""
    monkeypatch.setattr(resolve, "parse_entry", functools.cache(resolve.parse_entry))    # the same few lines, 200,000 times
    seen, commands = set(), set()
    for ins, outs in _lists():
        for ask in ASKS:
            e = sc.explain(ins, outs, ask)
            h = HostUse(e.host, e.port, (e.host,) if resolve.is_ip(e.host) else ())
            case = (ins, outs, ask)

            def verdict(more_in=(), h=h, e=e, ins=ins, outs=outs):
                return resolve.scope_verdict(target([*ins, *more_in], outs), [h], e.path)
            v = verdict()
            assert (e.verdict == "out") == bool(v.out), case
            assert (e.verdict == "not_in") == bool(v.not_in and not v.out), case
            if e.verdict == "out":
                assert e.line in outs, case
                assert resolve.matches(e.line, h, e.path, any_address=True, loose_path=True), case
            elif e.verdict == "in":
                assert e.line in ins and resolve.matches(e.line, h, e.path), case
            else:
                assert e.line is None, case
                assert (e.verdict == "no_list") == (not ins), case
            assert e.says.startswith(e.host) or e.verdict == "no_list", case
            bare = resolve.scope_verdict(target(ins, outs), [h], None)      # a command: the same host, no path
            if e.command is None:
                assert e.path is None or kind(bare) == kind(v), case
            else:
                assert e.path is not None and e.command["verdict"] in ("in", "out"), case
                assert (e.command["verdict"] == "out") == bool(bare.out) and not (bare.not_in and not bare.out), case
                assert e.command["says"].startswith(f"In a command, {e.host} is "), case
                commands.add(e.command["verdict"])
            for hint in e.hints:
                if "on a line of its own in In scope" in hint:
                    assert not verdict([e.host]).out, case
                if "add it on a line of its own" in hint:
                    assert not verdict([e.host]).not_in, case
                if "add *." in hint:
                    assert not verdict([hint.split("add ")[1].split(" on a line")[0]]).not_in, case
            seen.add(e.verdict)
    assert seen == {"in", "out", "not_in", "no_list", "local"} and commands == {"in", "out"}
