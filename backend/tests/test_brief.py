"""A pasted program brief becomes a target's scope lists. Every sample is the text a browser gives when
the scope part of a program page is selected and copied."""
import time

import pytest

from hunterai import brief
from hunterai.tools import resolve

H1_ROWS = """Asset name\tType\tCoverage\tMax. severity\tBounty\tLast update
*.example.com\tWildcard\tIn scope\tCritical\tEligible\tJan 12, 2025
api.example.com\tDomain\tIn scope\tCritical\tIneligible\tJan 12, 2025
https://example.com/careers\tURL\tOut of scope\tNone\tIneligible\tJan 12, 2025
blog.example.com\tDomain\tOut of scope\tNone\tIneligible\tJan 12, 2025
com.example.android\tAndroid: Play Store\tIn scope\tCritical\tEligible\tJan 12, 2025
203.0.113.0/24\tCIDR\tIn scope\tHigh\tEligible\tJan 12, 2025
"""

H1_SPLIT_ROWS = """*.example.com
All subdomains, except those listed as out of scope
Wildcard\tIn scope\t
Critical
\tEligible\tJan 12, 2025\t45 (12%)
blog.example.com
Hosted by a third party
Domain\tOut of scope\t
None
\tIneligible\tJan 12, 2025\t0 (0%)
"""

H1_ONE_CELL_PER_LINE = """Asset name
Type
Coverage
Max. severity
Bounty
*.example.com
Wildcard
In scope
Critical
Eligible
blog.example.com
Domain
Out of scope
None
Ineligible
"""

H1_CSV = """identifier,asset_type,instruction,eligible_for_bounty,eligible_for_submission,availability_requirement,confidentiality_requirement,integrity_requirement,max_severity,system_tags,created_at,updated_at
*.example.com,WILDCARD,"All subdomains, see https://docs.example.org",true,true,,,,critical,,2024-01-01,2024-01-02
blog.example.com,URL,,false,false,,,,none,,2024-01-01,2024-01-02
com.example.app,GOOGLE_PLAY_APP_ID,,true,true,,,,critical,,2024-01-01,2024-01-02
10.20.0.0/16,CIDR,,true,true,,,,high,,2024-01-01,2024-01-02
"""

BUGCROWD = """Scope & rewards
In scope
Target name\tType\tTags
https://app.example.com\tWebsite Testing\tReactJS\tRuby on Rails
*.example.io\tWebsite Testing
api.example.com:8443\tAPI Testing
Out of scope
Target name\tType
https://support.example.com\tWebsite Testing
https://status.example.com/\tWebsite Testing
Any domain not listed above
"""

YESWEHACK = """SCOPES
SCOPE\tTYPE\tASSET VALUE
https://www.example.com\tWeb application\tCritical
https://api.example.com/v2\tAPI\tHigh
*.example.net\tWeb application\tMedium
OUT OF SCOPES
https://blog.example.com
https://shop.example.com/checkout/*
All other subdomains of example.org
VULNERABILITY TYPES
QUALIFYING VULNERABILITIES
Remote code execution (RCE)
SQL injection on legacy.example.com
NON-QUALIFYING VULNERABILITIES
Self-XSS
Missing cookie flags
"""

INTIGRITI = """Domains
Tier 1
app.example.com
URL
Tier 2
*.example.com
Wildcard
No bounty
labs.example.com
URL
Out of scope
blog.example.com
URL
In scope
We are interested in any vulnerability that impacts our users.
Out of scope
Application
Self-XSS that cannot be used to exploit other users
Verbose error messages
"""

FREE_TEXT = """Scope: *.example.com, api.example.org and 198.51.100.7
Out of scope: blog.example.com, *.dev.example.com
Everything under shop.example.org is in scope except shop.example.org/admin and payments.example.org.
Please do not test our vendors (zendesk.com, salesforce.com).
Report to security@example.net.
"""

MARKDOWN = """## In Scope
- `https://app.example[.]com`
- hxxps://api.example[.]com/v1/*
* **portal.example.com** (customer portal)
| staging.example.com | Web |
- [the shop](https://shop.example.com/store?ref=brief#top)
## Out of Scope
1. https://example.com/blog
2) *.corp.example.com
"""


def test_hackerone_rows_carry_their_own_side():
    r = brief.read(H1_ROWS)
    assert r.in_scope == ["*.example.com", "api.example.com", "203.0.113.0/24"]
    assert r.out_scope == ["example.com/careers", "blog.example.com"]
    assert r.unplaced == [] and r.unsupported == []


def test_hackerone_rows_split_over_lines():
    r = brief.read(H1_SPLIT_ROWS)
    assert (r.in_scope, r.out_scope, r.unplaced) == (["*.example.com"], ["blog.example.com"], [])


def test_hackerone_with_every_cell_on_its_own_line():
    r = brief.read(H1_ONE_CELL_PER_LINE)
    assert (r.in_scope, r.out_scope, r.unplaced) == (["*.example.com"], ["blog.example.com"], [])


def test_hackerone_csv_export():
    r = brief.read(H1_CSV)
    assert r.in_scope == ["*.example.com", "10.20.0.0/16"]
    assert r.out_scope == ["blog.example.com"]
    assert r.unplaced == []          # the host in an instruction is not an asset


def test_bugcrowd_headings_decide():
    r = brief.read(BUGCROWD)
    assert r.in_scope == ["app.example.com", "*.example.io", "api.example.com:8443"]
    assert r.out_scope == ["support.example.com", "status.example.com"]
    assert "Any domain not listed above" in r.notes


def test_yeswehack_scopes_and_out_of_scopes():
    r = brief.read(YESWEHACK)
    assert r.in_scope == ["www.example.com", "api.example.com/v2", "*.example.net"]
    assert r.out_scope == ["blog.example.com", "shop.example.com/checkout", "*.example.org"]
    assert r.unplaced == ["legacy.example.com"]      # named under a list of bug classes, on neither side
    assert "Self-XSS" in r.notes and "Missing cookie flags" in r.notes
    assert "Remote code execution" not in r.notes


def test_intigriti_tiers_are_in_scope():
    r = brief.read(INTIGRITI)
    assert r.in_scope == ["app.example.com", "*.example.com", "labs.example.com"]
    assert r.out_scope == ["blog.example.com"]
    assert "Verbose error messages" in r.notes


def test_free_text_labels_and_exceptions():
    r = brief.read(FREE_TEXT)
    assert r.in_scope == ["*.example.com", "api.example.org", "198.51.100.7", "shop.example.org"]
    assert r.out_scope == ["blog.example.com", "*.dev.example.com", "shop.example.org/admin",
                           "payments.example.org", "zendesk.com", "salesforce.com"]
    assert r.unplaced == []          # the address the reports go to is not an asset


def test_markdown_defanged_hosts_and_links():
    r = brief.read(MARKDOWN)
    assert r.in_scope == ["app.example.com", "api.example.com/v1", "portal.example.com", "staging.example.com",
                          "shop.example.com/store"]
    assert r.out_scope == ["example.com/blog", "*.corp.example.com"]


@pytest.mark.parametrize("line,entry", [
    ("https://example.com", "example.com"),
    ("https://example.com:443/", "example.com"),
    ("http://example.com:80", "example.com"),
    ("https://example.com:8443/app/", "example.com:8443/app"),
    ("HTTPS://WWW.Example.COM/Path/Kept", "www.example.com/Path/Kept"),
    ("http(s)://example.com/*", "example.com"),
    ("example.com/api/*/users", "example.com/api"),
    ("*.example.com/*", "*.example.com"),
    ("https://*.example.com", "*.example.com"),
    ("*.*.example.com", "*.example.com"),
    ("example.co.uk.", "example.co.uk"),
    ("(example.com),", "example.com"),
    ("10.0.0.5:8080", "10.0.0.5:8080"),
    ("10.0.0.77/24", "10.0.0.0/24"),
    ("2001:db8::/48", "2001:db8::/48"),
    ("[2001:db8::1]:8443", "[2001:db8::1]:8443"),
    ("dc01.corp.local", "dc01.corp.local"),
    ("any subdomain of example.com", "*.example.com"),
])
def test_how_an_asset_is_written_down(line, entry):
    r = brief.read("In scope:\n" + line)
    assert r.in_scope == [entry], r
    assert resolve.parse_entry(entry) is not None          # the scope check reads what the brief reader writes


def test_address_ranges_become_networks():
    r = brief.read("In scope:\n198.51.100.0 - 198.51.100.255\n192.0.2.8-11\n")
    assert r.in_scope == ["198.51.100.0/24", "192.0.2.8/30"]


@pytest.mark.parametrize("line", [
    "security@example.com", "README.md", "report.pdf and index.html", "version 2.4.1 of Node.js",
    "built with ASP.NET", "com.example.app", "https://play.google.com/store/apps/details?id=com.example",
    "https://apps.apple.com/app/id123456", "e.g. the login page", "TLS 1.2 or 1.3", "*.js and *.min.js files",
    "127.0.0.1", "0.0.0.0", "file:///etc/passwd", "android-app://com.example/open",
])
def test_what_is_not_a_host(line):
    r = brief.read("In scope:\n" + line)
    assert (r.in_scope, r.out_scope, r.unplaced, r.unsupported) == ([], [], [], []), r


def test_patterns_no_scope_entry_can_say():
    r = brief.read("In scope\n*.example.*\napi-*.example.com\n*.example.org\n")
    assert r.in_scope == ["*.example.org"]
    assert r.unsupported == ["*.example.*", "api-*.example.com"]


def test_named_on_both_sides_is_out():
    r = brief.read("In scope\nexample.com\nshop.example.com\nOut of scope\nexample.com\n")
    assert (r.in_scope, r.out_scope) == (["shop.example.com"], ["example.com"])


def test_a_rule_about_a_host_does_not_take_it_out_of_scope():
    r = brief.read("In scope\nshop.example.com\napi.example.com\n\nRules\n"
                   "Do not test payments on shop.example.com with real cards.\n"
                   "Automated scanning of api.example.com is prohibited.\n"
                   "Out of scope\nRate limiting on shop.example.com\n")
    assert r.in_scope == ["shop.example.com", "api.example.com"] and r.out_scope == []


def test_the_side_written_on_the_line_beats_the_heading():
    r = brief.read("In scope\nwww.example.com\nblog.example.com (out of scope)\n"
                   "Out of scope\nold.example.com\napi.example.com is in scope\n")
    assert r.in_scope == ["www.example.com"]
    assert r.out_scope == ["blog.example.com", "old.example.com", "api.example.com"]   # an out-of-scope list never adds


def test_hosts_in_plain_sentences_are_left_for_the_owner():
    r = brief.read("Welcome to the Example program. Our main site is https://www.example.com and support "
                   "runs on example.zendesk.com.\nSee https://hackerone.com/example/policy and "
                   "https://www.first.org/cvss/calculator/3.1 for the rating.\n")
    assert (r.in_scope, r.out_scope) == ([], [])
    assert r.unplaced == ["www.example.com", "example.zendesk.com"]


def test_a_sentence_under_in_scope_does_not_put_a_host_in_scope():
    r = brief.read("In scope\n*.example.com\nOur status page runs on statuspage.io and is hosted elsewhere.\n")
    assert r.in_scope == ["*.example.com"] and r.unplaced == ["statuspage.io"]


def test_a_platform_everyone_shares_is_never_in_scope_by_itself():
    r = brief.read("In scope\ngithub.com\n*.amazonaws.com\nhttps://github.com/example/widget\n"
                   "example.zendesk.com\nwww.example.com\n")
    assert r.in_scope == ["github.com/example/widget", "example.zendesk.com", "www.example.com"]
    assert r.unplaced == ["github.com", "*.amazonaws.com"]


def test_a_new_section_ends_the_scope_list():
    r = brief.read("In scope\nwww.example.com\n\nRewards\nPaid through pay.example.org\n\n"
                   "## Test accounts\nsignup.example.org\n")
    assert r.in_scope == ["www.example.com"]
    assert r.unplaced == ["pay.example.org", "signup.example.org"]


def test_rules_reach_the_notes():
    r = brief.read("In scope\nwww.example.com\n\nRules\nDo not perform denial of service attacks.\n"
                   "Limit automated scanning to 5 requests per second.\n"
                   "Add the header X-Bug-Bounty: your-username to every request.\n"
                   "We look forward to your reports.\n")
    for line in ("Do not perform denial of service attacks.", "Limit automated scanning to 5 requests per second.",
                 "Add the header X-Bug-Bounty: your-username to every request."):
        assert line in r.notes
    assert "look forward" not in r.notes


def test_the_name_is_the_domain_most_of_the_scope_is_under():
    assert brief.read("In scope\n*.example.co.uk\napi.example.co.uk\ncdn.other.net\n").name == "example.co.uk"
    assert brief.read("In scope\n10.0.0.0/24\n").name == ""


def test_a_bare_list_of_hosts_is_the_scope():
    r = brief.read("*.example.com\napi.example.com\n\nhttps://www.example.org/app\n")
    assert r.in_scope == ["*.example.com", "api.example.com", "www.example.org/app"] and r.unplaced == []


def test_a_bare_list_keeps_a_doubted_host_for_the_owner():
    r = brief.read("old.example.com - not ours\napp.example.com\n")
    assert (r.in_scope, r.unplaced) == (["app.example.com"], ["old.example.com"])


@pytest.mark.parametrize("line,in_scope,out_scope,unplaced", [
    ("legacy.example.com is no longer considered to be in scope", [], ["legacy.example.com"], []),
    ("We do not currently treat old.example.com as in scope", [], ["old.example.com"], []),
    ("Only api.example.com is in scope; www.example.com is not.", ["api.example.com"], ["www.example.com"], []),
    ("Is beta.example.com in scope? Ask us first.", [], [], ["beta.example.com"]),
    ("Please note the following regarding scope: partner.example.net belongs to a vendor", [], [],
     ["partner.example.net"]),
    ("Everything is out of scope except api.example.com", [], [], ["api.example.com"]),
])
def test_only_plain_words_put_a_host_in_scope(line, in_scope, out_scope, unplaced):
    r = brief.read(line)
    assert (r.in_scope, r.out_scope, r.unplaced) == (in_scope, out_scope, unplaced)


@pytest.mark.parametrize("line,in_scope,unplaced", [
    # "in scope" has to be said of the host, not merely stand in the same sentence
    ("Vulnerabilities in scope must leave the uptime of status.example.net alone", [], ["status.example.net"]),
    ("Issues in scope get a reward, see pay.example.net for the amounts", [], ["pay.example.net"]),
    ("We consider api.example.com in scope", ["api.example.com"], []),
    ("The in-scope domains are a.example.com and b.example.com", ["a.example.com", "b.example.com"], []),
    ("Both api.example.com and app.example.com are in scope", ["api.example.com", "app.example.com"], []),
    ("Note that in scope: cdn.example.com", ["cdn.example.com"], []),
])
def test_in_scope_has_to_be_said_of_the_host(line, in_scope, unplaced):
    r = brief.read(line)
    assert (r.in_scope, r.unplaced) == (in_scope, unplaced)


def test_a_table_header_named_in_scope_marks_nothing():
    r = brief.read("Out of scope\nblog.example.com\n| Asset | In scope | Notes |\n")
    assert (r.in_scope, r.out_scope) == ([], ["blog.example.com"])


@pytest.mark.parametrize("tail", ["Something new\tOut of scope\t\n", "Something new\nOut of scope\nNone\nIneligible\n"])
def test_an_out_of_scope_cell_marks_its_row_whatever_the_type(tail):
    r = brief.read("In scope\nbeta.example.com\n" + tail)
    assert (r.in_scope, r.out_scope) == ([], ["beta.example.com"])


@pytest.mark.parametrize("tail", ["Something new\tIn scope\t\n", "Something new\nIn scope\nCritical\n"])
def test_an_in_scope_cell_needs_a_row_it_knows(tail):
    r = brief.read("Out of scope\nbeta.example.com\n" + tail)
    assert (r.in_scope, r.out_scope) == ([], ["beta.example.com"])


def test_a_description_does_not_end_the_list():
    r = brief.read("In scope\nhttps://app.example.com\nCustomer account portal\n"
                   "https://api.example.com\nMain access point\nType\nhttps://cdn.example.com\n")
    assert r.in_scope == ["app.example.com", "api.example.com", "cdn.example.com"]


def test_a_file_in_a_sentence_is_not_a_host():
    assert brief.read("In scope:\nrun setup.py before testing\n").as_dict()["unplaced"] == []
    assert brief.read("In scope:\nallegro.pl\n").in_scope == ["allegro.pl"]


def test_a_word_about_an_app_does_not_hide_a_host():
    assert brief.read("In scope\nOur iOS app talks to api.example.com\n").unplaced == ["api.example.com"]
    r = brief.read("de.example.app\tAndroid: Play Store\tIn scope\n")
    assert (r.in_scope, r.unplaced) == ([], [])


def test_mail_addresses_name_no_host():
    r = brief.read("In scope:\ninfo.me@example.com, first.last@sub.example.org\n")
    assert (r.in_scope, r.unplaced) == ([], [])


def test_a_star_glued_to_a_name_is_shown_as_written():
    r = brief.read("In scope\n*example.com\nhttps://*.example.*\n")
    assert r.in_scope == [] and r.unsupported == ["*example.com", "https://*.example.*"]


def test_a_mention_the_lists_already_cover_is_not_asked_about():
    r = brief.read("In scope\n*.example.com\nOut of scope\nblog.example.org\n\nOverview\n"
                   "Sign up at https://app.example.com/signup and read https://blog.example.org/rules "
                   "or https://www.example.net/about\n")
    assert r.unplaced == ["www.example.net/about"]


def test_scope_exclusions_is_an_out_of_scope_heading():
    r = brief.read("Scope exclusions\nold.example.com\nIssues on blog.example.com, which a vendor hosts.\n")
    assert r.out_scope == ["old.example.com", "blog.example.com"] and r.unplaced == []


@pytest.mark.parametrize("line,in_scope,out_scope", [
    ("We don't test: old.example.com", [], ["old.example.com"]),
    ("We don\u2019t test: old.example.com", [], ["old.example.com"]),
    ("Never scan: fragile.example.com, db.example.com", [], ["fragile.example.com", "db.example.com"]),
    ("Not ours: cdn.example.net", [], ["cdn.example.net"]),
    ("Third party - pay.example.net", [], ["pay.example.net"]),
    ("Production: app.example.com", ["app.example.com"], []),
])
def test_a_label_that_says_no_keeps_the_host_out_of_an_in_scope_list(line, in_scope, out_scope):
    r = brief.read("In scope\n" + line + "\n")
    assert (r.in_scope, r.out_scope) == (in_scope, out_scope)


@pytest.mark.parametrize("line,in_scope,out_scope,unplaced", [
    # a "no" after the host may be about the host or about one corner of it: the owner decides
    ("www.example.com - please don't test /checkout", [], [], ["www.example.com"]),
    ("old.example.com - not ours", [], [], ["old.example.com"]),
    ("api.example.com (no rate limit testing)", [], [], ["api.example.com"]),
    # an exception names what is out and leaves the rest of the line alone
    ("*.example.com (not including blog.example.com)", ["*.example.com"], ["blog.example.com"], []),
    # another cell of the row describes the asset: its words are not about scope
    ("https://app.example.com\tWebsite Testing\tNo credentials are provided", ["app.example.com"], [], []),
    ("app.example.com (customer portal)", ["app.example.com"], [], []),
])
def test_a_no_beside_a_listed_host_is_left_to_the_owner(line, in_scope, out_scope, unplaced):
    r = brief.read("In scope\n" + line + "\n")
    assert (r.in_scope, r.out_scope, r.unplaced) == (in_scope, out_scope, unplaced)


def test_a_host_a_table_puts_in_scope_stays_there_whatever_a_later_line_adds():
    r = brief.read("www.example.com\tDomain\tIn scope\n\nIn scope\nwww.example.com - please don't test /checkout\n")
    assert (r.in_scope, r.unplaced) == (["www.example.com"], [])


@pytest.mark.parametrize("text,entries", [
    ("In scope\n\u2022 app.example.com\u200b\n", ["app.example.com"]),              # a bullet, a zero-width space
    ("In scope\nAPI \u2013 api.example.com\n", ["api.example.com"]),                  # an en dash after the label
    ("In scope\nstaging\u00a0\u2192 stg.example.com\n", ["stg.example.com"]),         # a no-break space, an arrow
    ("In scope\n10.0.0.1 \u2013 10.0.0.3\n", ["10.0.0.1", "10.0.0.2/31"]),
])
def test_typographic_characters_are_read(text, entries):
    assert brief.read(text).in_scope == entries


def test_nothing_to_read():
    for text in ("", "   \n\n", "hello there"):
        r = brief.read(text)
        assert r.as_dict() == {"name": "", "in": [], "out": [], "unplaced": [], "unsupported": [], "notes": ""}


def test_every_entry_written_is_one_the_scope_check_reads():
    for sample in (H1_ROWS, H1_SPLIT_ROWS, H1_CSV, BUGCROWD, YESWEHACK, INTIGRITI, FREE_TEXT, MARKDOWN):
        r = brief.read(sample)
        for entry in r.in_scope + r.out_scope + r.unplaced:
            assert resolve.parse_entry(entry) is not None, entry


@pytest.mark.parametrize("text", [
    "a." * 150_000, "*." * 150_000, "-" * 300_000, ("a" * 60 + ".") * 5_000, "1.2.3.4-" * 40_000,
    ("x" * 1_999 + "\n") * 150, "[" * 200_000, "http://" * 50_000, ":" * 300_000, "in scope " * 30_000,
    ("except " * 200 + "\n") * 200, "\t" * 300_000, "(.)" * 100_000, "@" * 300_000, "a@b.co " * 50_000,
    "not " * 90_000, "a.example.com, " * 25_000, "| a | " * 60_000,
])
def test_hostile_text_is_read_in_bounded_time(text):
    started = time.monotonic()
    brief.read(text[:brief.MAX_TEXT])
    assert time.monotonic() - started < 3


def test_longer_than_the_limit_is_refused():
    with pytest.raises(ValueError):
        brief.read("a" * (brief.MAX_TEXT + 1))
