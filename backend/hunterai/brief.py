"""Reads a pasted program brief (HackerOne, Bugcrowd, YesWeHack, Intigriti, or plain text) into the
two scope lists of a target.

Rules, not a model: the in-scope list decides which hosts a tool call may reach without a question, and
a brief is somebody else's text. Everything here leans one way. A host is put in scope only where the
brief says so: a table row marked in scope, an item of a list under an in-scope heading, or in so many
words. Whatever is less clear is handed back as "unplaced" for the owner to decide, a host named on both
sides is out, and nothing is saved from here: the dialog shows the result for review first."""
from __future__ import annotations

import csv
import io
import ipaddress
import re
from dataclasses import dataclass, field

from .tools import resolve
from .tools.base import HostUse

MAX_TEXT = 400_000
_MAX_LINE = 2_000
_MAX_TOKENS = 20_000
_MAX_LIST = 2_000
_MAX_RANGE_NETWORKS = 32
_MAX_OUT_NOTES, _MAX_RULES, _MAX_NOTE, _MAX_NOTES = 40, 25, 300, 4_000

# How sure a line makes a side. The surest line decides an entry; where the surest disagree, it is out.
UNSAID, HINTED, SAID, LISTED, MARKED = 0, 1, 2, 3, 4

_CC = set("""ac ad ae af ag ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj bm bn bo br bs bt bw by
bz ca cc cd cf cg ch ci ck cl cm cn co cr cu cv cw cx cy cz de dj dk dm do dz ec ee eg er es et eu fi fj fk fm fo
fr ga gd ge gf gg gh gi gl gm gn gp gq gr gs gt gu gw gy hk hm hn hr ht hu id ie il im in io iq ir is it je jm jo
jp ke kg kh ki km kn kp kr kw ky kz la lb lc li lk lr ls lt lu lv ly ma mc md me mg mh mk ml mm mn mo mp mq mr ms
mt mu mv mw mx my mz na nc ne nf ng ni nl no np nr nu nz om pa pe pf pg ph pk pl pm pn pr ps pt pw py qa re ro rs
ru rw sa sb sc sd se sg sh si sk sl sm sn so sr ss st su sv sx sy sz tc td tf tg th tj tk tl tm tn to tr tt tv tw
tz ua ug uk us uy uz va vc ve vg vi vn vu wf ws ye yt za zm zw""".split())
_GENERIC = set("""com net org edu gov mil int info biz name pro mobi asia tel travel jobs cat coop museum aero
app dev page new xyz online site website tech cloud store shop live club fun life art blog wiki design studio
agency digital network systems solutions services software technology security global world group team company
business email link click one top vip work works tools zone space press news media games game bank finance
financial insurance health care clinic fit fitness hotel house homes realty estate auto cars energy eco earth
city town land farm garden food cafe bar pub restaurant pizza coffee wine beer fashion style shoes gift gifts toys
kids family love dating wedding photo photos pictures gallery video film movie music audio radio show tickets
events party sport sports football golf tennis yoga run training academy school college university education
courses study science engineering consulting management marketing partners ventures capital fund money cash credit
loans tax legal law expert guru support help chat community social forum foundation ngo charity church exchange
market markets trade trading plus center centre direct express delivery supply parts repair build builders
construction contractors industries international limited ltd inc llc gmbh sarl srl holdings enterprises
institute report reports reviews review guide today now here host hosting domains codes computer mobile phone
io-labs data cam camera watch tube stream gg-labs
google youtube android apple amazon aws microsoft azure windows office bing ibm sap oracle cisco intel dell
sony samsung philips bosch bmw audi volkswagen toyota honda nissan ford bnpparibas barclays hsbc citi visa axa
allianz""".split())
_INTERNAL = set("local internal corp lan intranet home test localdomain".split())
_TLDS = _CC | _GENERIC | _INTERNAL
# Two-letter country codes that are also what a file ends in: `setup.py`, `README.md`, `run.sh`.
_FILE_LIKE = set("py sh pl rs md so ml ps pm tf".split())
_FILE_ENDINGS = set("""js mjs cjs ts tsx jsx css scss php html htm json xml png jpg jpeg gif svg ico pdf txt gz tgz
zip rar log bak sql yml yaml env ini conf cfg exe dll apk ipa jar war asp aspx jsp cgi wasm map lock csv doc docx
xls xlsx ppt pptx rb go java kt swift c h cpp""".split())
_NOT_HOSTS = {"asp.net", "ado.net", "vb.net", "socket.io"}           # names of software that read like hosts
_SCHEMES = set("""http https ws wss ftp ftps sftp ssh tcp udp tls ssl ldap ldaps smtp imap pop3 rdp smb grpc mqtt
amqp redis postgres postgresql mysql mongodb""".split())
_DEFAULT_PORT = {"https": 443, "wss": 443, "http": 80, "ws": 80}
_APP_ID_STARTS = ("com", "org", "net")          # com.example.app is an app, not a host
_SECOND_LEVEL = set("co com org net gov ac edu gob or ne go mil nom sch ltd plc".split())

# Everyone's tenants live under these: the bare host, or a wildcard over it, is never put in scope from a
# brief. A tenant (`example.zendesk.com`) or a path (`github.com/example/repo`) is an ordinary asset.
_SHARED = set("""github.com gitlab.com bitbucket.org google.com googleapis.com gstatic.com apple.com microsoft.com
live.com office.com windows.net azure.com azurewebsites.net cloudapp.net amazonaws.com amazon.com cloudfront.net
herokuapp.com heroku.com netlify.app vercel.app pages.dev workers.dev github.io gitlab.io firebaseapp.com web.app
appspot.com zendesk.com atlassian.net atlassian.com salesforce.com force.com shopify.com myshopify.com
wordpress.com wpengine.com slack.com facebook.com instagram.com twitter.com x.com linkedin.com youtube.com t.me
telegram.org discord.com discord.gg medium.com npmjs.com pypi.org docker.com cloudflare.com fastly.net akamai.net
akamaihd.net okta.com auth0.com stripe.com paypal.com intercom.io hubspot.com mailchimp.com sendgrid.net
statuspage.io freshdesk.com readme.io gitbook.io notion.so typeform.com calendly.com zoom.us dropbox.com
box.com""".split())
# Where the brief itself lives and what it cites: kept only where the brief gives them a side.
_CITED = set("""hackerone.com hackerone-user-content.com hacker.one bugcrowd.com yeswehack.com intigriti.com
hackenproof.com immunefi.com synack.com cobalt.io huntr.com huntr.dev openbugbounty.org yogosha.com
standoff365.com first.org mitre.org owasp.org nist.gov wikipedia.org disclose.io w3.org ietf.org cve.org
portswigger.net""".split())
# A link to a shop's page for an app is the app, not a host to test.
_APP_SHOPS = {"play.google.com", "apps.apple.com", "itunes.apple.com", "testflight.apple.com",
              "chromewebstore.google.com", "chrome.google.com", "addons.mozilla.org", "apps.microsoft.com"}


@dataclass
class Brief:
    name: str = ""
    in_scope: list[str] = field(default_factory=list)
    out_scope: list[str] = field(default_factory=list)
    unplaced: list[str] = field(default_factory=list)       # hosts the brief names without a side
    unsupported: list[str] = field(default_factory=list)    # patterns no scope entry can say, as written
    notes: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "in": self.in_scope, "out": self.out_scope, "unplaced": self.unplaced,
                "unsupported": self.unsupported, "notes": self.notes}


@dataclass
class _Token:
    start: int
    end: int
    entry: str | None       # what goes on a scope list; None for a pattern no entry can say
    raw: str
    bare: bool = True       # written without a scheme
    file_like: bool = False # `setup.py`: a host only as an item of a list
    listed: bool = False    # an item of a list or the asset of a row, not a word in a sentence
    refused: bool = False   # listed after a label that says no: "Not ours: cdn.example.net"
    doubted: bool = False   # a "no" in its cell that may be about it: "old.example.com - not ours"


@dataclass
class _Placed:
    entry: str
    side: str | None
    weight: int
    listed: bool = False


# ---------------------------------------------------------------------------------------------------------
# Text to lines

_INVISIBLE = re.compile("[\u200b-\u200f\u2060\ufeff]")
_DEFANGED_DOT = re.compile(r"\[\.\]|\(\.\)|\[dot\]|\(dot\)", re.I)
_MD_LINK = re.compile(r"\[([^\]\[\n]{0,300})\]\(([^\s()]{1,1000})\)")
_BULLET = re.compile(r"^\s*(?:[-\u2022\u25cf\u25aa\u25e6\u2023\u00b7\u2013\u2014]|\*(?=\s)|\d{1,3}[.)])\s+")
_MD_HEADING = re.compile(r"^\s*(#{1,6})\s+")


def _lines(text: str) -> list[str]:
    text = _INVISIBLE.sub("", text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " "))
    text = re.sub(r"\bhxxp(s?)://", r"http\1://", text, flags=re.I)
    text = re.sub(r"\bhttp\(s\)://", "https://", text, flags=re.I)
    text = _DEFANGED_DOT.sub(".", text)
    out: list[str] = []
    for line in text.split("\n"):
        while len(line) > _MAX_LINE:          # a page pasted as one line: cut at a space where there is one
            cut = line.rfind(" ", 0, _MAX_LINE)
            cut = cut if cut > _MAX_LINE // 2 else _MAX_LINE
            out.append(line[:cut])
            line = line[cut:]
        out.append(line)
    return out


@dataclass
class _Row:
    text: str                                 # the line without its bullet and markup
    cells: list[tuple[int, str]]              # (offset in text, cell)
    heading_level: int = 0                    # 1-6 for a markdown heading
    tokens: list[_Token] = field(default_factory=list)

    @property
    def single(self) -> str | None:
        return self.cells[0][1].strip() if len(self.cells) == 1 else None


def _row(line: str) -> _Row:
    level = 0
    m = _MD_HEADING.match(line)
    if m:
        level, line = len(m.group(1)), line[m.end():]
    line = _BULLET.sub("", line, count=1)
    line = _MD_LINK.sub(lambda k: f"{k.group(2)} ({k.group(1)})", line)
    line = line.replace("**", "").replace("__", "").replace("`", "")
    cells, at = [], 0
    for part in re.split(r"(\t|\|)", line):
        if part not in ("\t", "|") and part.strip():
            cells.append((at, part))
        at += len(part)
    return _Row(text=line, cells=cells, heading_level=level)


# ---------------------------------------------------------------------------------------------------------
# Hosts, addresses and URLs in a piece of text

_MAIL_NAME = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._%+-")
_V4 = r"\d{1,3}(?:\.\d{1,3}){3}"
_RANGE = re.compile(rf"(?<![\w.])({_V4})\s{{0,3}}(?:-|\u2013|\u2014|to)\s{{0,3}}({_V4}|\d{{1,3}})(?![\w.])")
_V6 = re.compile(r"(?<![0-9A-Fa-f:.\[])((?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4})(/\d{1,3})?(?![0-9A-Fa-f:.\]])")
_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9@._%+/*-])"
    r"(?:(?P<scheme>[A-Za-z][A-Za-z0-9+.-]{1,12})://)?"
    r"(?P<host>(?:[A-Za-z0-9*](?:[A-Za-z0-9*-]{0,62})?\.)+[A-Za-z0-9*-]{1,63}|\[[0-9A-Fa-f:.]{2,45}\])"
    r"(?::(?P<port>\d{1,5})(?!\d))?"
    r"(?P<path>/[^\s<>\"'`|,;)\]]{0,1000})?")
_SUBDOMAINS_OF = re.compile(r"(?i)\bsub-?domains?\s+(?:of|under|on|for)\s+(?:the\s+)?(?:domain\s+)?$")
_LABEL = re.compile(r"(?i)^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _blank(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


def _without_mail(text: str) -> str:
    """`info.me@example.com` names no host: neither side of the "@" is read."""
    at = text.find("@")
    while at != -1:
        start, end = at, at + 1
        while start > 0 and at - start < 64 and text[start - 1] in _MAIL_NAME:
            start -= 1
        while end < len(text) and end - at < 255 and (text[end] in _MAIL_NAME and text[end] not in "_%+"):
            end += 1
        text = _blank(text, start, end)
        at = text.find("@", end)
    return text


def _address(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    return None if ip.is_unspecified or ip.is_loopback or ip.is_multicast else ip


def _path(path: str | None) -> str:
    """The prefix the entry keeps: no query, no fragment, nothing from the first `*` on."""
    path = re.split(r"[?#*]", path or "", maxsplit=1)[0].rstrip(".,:;!")
    while path.endswith("/"):
        path = path[:-1]
    return path


def _name(host: str, scheme: str | None, original: str) -> str | None:
    """A host name as a scope entry writes it, or None when the word only looks like one."""
    labels = host.split(".")
    if len(host) > 253 or len(labels) < 2 or not all(_LABEL.match(x) for x in labels):
        return None
    tld = labels[-1]
    if not tld.isalpha() or len(tld) < 2:
        return None
    if scheme:
        return host
    if tld not in _TLDS or host in _NOT_HOSTS:
        return None
    if len(labels) >= 3 and labels[0] in _APP_ID_STARTS:
        return None
    if len(labels) == 2 and tld in _FILE_LIKE and original.split(".")[0] != labels[0]:   # README.md
        return None
    return host


def _entry(scheme: str | None, host: str, port: str | None, path: str | None, original: str) -> str | None:
    scheme = scheme.lower() if scheme else None
    if scheme and scheme not in _SCHEMES:
        return None
    host = host.lower().rstrip(".")
    tail = ""
    number = int(port) if port else None
    if number is not None and 0 < number < 65536 and number != _DEFAULT_PORT.get(scheme or ""):
        tail = f":{number}"
    if host.startswith("["):
        ip = _address(host.strip("[]"))
        return f"[{ip}]{tail}{_path(path)}" if ip else None
    if re.fullmatch(_V4, host):
        prefix = re.match(r"^/(\d{1,2})(?=/|$)", path or "") if not tail else None
        try:
            if prefix:
                network = ipaddress.ip_network(f"{host}/{prefix.group(1)}", strict=False)
                return f"{network}{_path(path[prefix.end():])}"
        except ValueError:
            return None
        ip = _address(host)
        return f"{ip}{tail}{_path(path)}" if ip else None
    wild = False
    while host.startswith("*."):
        wild, host = True, host[2:]
    if "*" in host:
        return None
    name = _name(host, scheme or ("wild" if wild else None), original)
    if name is None:
        return None
    if wild and (len(name.split(".")) < 2 or name.split(".")[-1] in _FILE_ENDINGS):
        return None
    return f"{'*.' if wild else ''}{name}{tail}{_path(path)}"


def _base(entry: str) -> str:
    """The registrable part of an entry's host: `api.example.co.uk/v2` -> `example.co.uk`."""
    host = re.split(r"[/:]", entry.removeprefix("*."), maxsplit=1)[0]
    labels = host.split(".")
    if len(labels) < 2 or not labels[-1].isalpha():
        return ""
    keep = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL else 2
    return ".".join(labels[-keep:])


def _shared(entry: str) -> bool:
    host, _, rest = entry.removeprefix("*.").partition("/")
    return not rest and host.removeprefix("www.") in _SHARED


def _covered(entry: str, lists: list[str]) -> bool:
    """A host a sentence mentions needs no decision when one of the lists already speaks for it."""
    e = resolve.parse_entry(entry)
    if e is None or e.host is None:
        return False
    host = HostUse(e.host, e.port or 443)
    for other in lists:
        o = resolve.parse_entry(other)
        if o is not None and not (o.path and not e.path) and resolve.matches(other, host, e.path):
            return True          # `github.com/example/repo` on a list says nothing about `github.com`
    return False


def _cited(entry: str) -> bool:
    return _base(entry) in _CITED


def _scan(text: str) -> list[_Token]:
    """Every host, address, range and URL in `text`, left to right."""
    found: list[_Token] = []
    text = _without_mail(text)
    for m in list(_RANGE.finditer(text)):
        first = _address(m.group(1))
        last = _address(m.group(2) if "." in m.group(2) else m.group(1).rsplit(".", 1)[0] + "." + m.group(2))
        if first and last and first <= last:
            networks = list(ipaddress.summarize_address_range(first, last))
            if len(networks) <= _MAX_RANGE_NETWORKS:
                found += [_Token(m.start(), m.end(), str(n) if n.num_addresses > 1 else str(n.network_address),
                                 m.group(0)) for n in networks]
            else:
                found.append(_Token(m.start(), m.end(), None, m.group(0)))
            text = _blank(text, m.start(), m.end())
    for m in list(_V6.finditer(text)):
        ip = _address(m.group(1))
        if ip is not None and ip.version == 6:
            entry = str(ipaddress.ip_network(f"{ip}{m.group(2)}", strict=False)) if m.group(2) else str(ip)
            if resolve.parse_entry(entry):
                found.append(_Token(m.start(), m.end(), entry, m.group(0)))
            text = _blank(text, m.start(), m.end())
    for m in _CANDIDATE.finditer(text):
        raw = m.group(0).rstrip(".,:;!?")
        host = m.group("host")
        entry = _entry(m.group("scheme"), host, m.group("port"), m.group("path"), host)
        if entry is None:
            pattern = host.lower()
            worth = ("*" in pattern and re.search(r"[a-z0-9]{2,}", pattern)
                     and pattern.rsplit(".", 1)[-1] not in _FILE_ENDINGS
                     and not re.fullmatch(r"\*\.[a-z0-9-]+", pattern))        # "*.js", "*.com"
            if worth:
                found.append(_Token(m.start(), m.end(), None, raw))
            continue
        if resolve.parse_entry(entry) is None or entry.split("/")[0].removeprefix("www.") in _APP_SHOPS:
            continue
        bare = not m.group("scheme")
        if (bare and "/" not in entry and ":" not in entry and not entry.startswith("*.")
                and not re.fullmatch(_V4, entry) and _SUBDOMAINS_OF.search(text[max(0, m.start() - 60):m.start()])):
            entry = "*." + entry
        file_like = bare and entry.count(".") == 1 and entry.rsplit(".", 1)[-1] in _FILE_LIKE
        found.append(_Token(m.start(), m.end(), entry, raw, bare=bare, file_like=file_like))
    return sorted(found, key=lambda t: t.start)


# ---------------------------------------------------------------------------------------------------------
# What a line says about the hosts on it

_NEG = re.compile(r"""(?ix)
      \bout[\s-]{0,3}of[\s-]{0,3}scopes?\b | \bout[\s-]scope\b | \boos\b
    | (?:\bnot|n[\u2019']t|\bnever|\bno\s{1,3}longer|\bneither|\bnor)\s{1,3}
        (?:[\w.*/:-]{1,80}\s{1,3}){0,4}in[\s-]{0,3}scope\b
    | \b(?:is|are)\s{1,3}not(?=\s{0,3}(?:[.,;)]|$))
    | \boutside\s{1,3}(?:of\s{1,3})?(?:(?:the|our)\s{1,3})?scope\b
    | \boff[\s-]{0,3}limits\b | \bexcluded\b | \bexclusions?\b""")
_EXCEPT = re.compile(r"(?i)\b(?:except(?:\s{1,3}for)?|excluding|excludes?|with\s{1,3}the\s{1,3}exception\s{1,3}of"
                     r"|other\s{1,3}than|apart\s{1,3}from|but\s{1,3}not|not\s{1,3}including)\b")
_POS = re.compile(r"(?i)\bin[\s-]{0,3}scopes?\b|\bwithin\s{1,3}(?:(?:the|our)\s{1,3})?scope\b")
_HINT = re.compile(r"""(?ix)
      (?:\bdo\s{1,3}not|\bdon[\u2019']t|\bmust\s{1,3}not|\bshould\s{1,3}not|\bmay\s{1,3}not|\bcannot|\bcan[\u2019']t|\bnever)
        \s{1,3}(?:be\s{1,3})?(?:test|attack|scan|target|touch|access|probe)(?:ed|ned)?\b
    | \bnot\s{1,3}(?:to\s{1,3}be\s{1,3})?(?:tested|allowed|permitted|authori[sz]ed|covered|included)\b
    | \bprohibited\b | \bforbidden\b | \bthird[\s-]{0,3}part(?:y|ies)\b""")
_SENTENCE_END = re.compile(r"[.;!?](?=\s|$)|\b(?:but|however|while|whereas|although|though)\b(?!\s{1,3}not\b)", re.I)
_ABBREVIATION = re.compile(r"(?i)\b(?:e\.g|i\.e|etc|vs|incl|excl|approx)\.")
_LIST_GLUE = re.compile(r"(?i)^[\s,;/&+()\[\]-]{0,12}(?:\b(?:and|or)\b)?[\s,;(]{0,6}$")
_LEAD_LABEL = re.compile(r"(?i)^(?:[\w./&+'\u2019-]{1,30}\s{1,3}){0,3}[\w./&+'\u2019-]{1,30}\s{0,3}[:=\u2192>\u2013\u2014-]$")
_LEAD_SAYS_NO = re.compile(r"(?i)\b(?:not?|never|none|without)\b|n[\u2019']t\b")
_LEAD_SUBDOMAINS = re.compile(r"(?i)^(?:(?:all|any|every|each|other|the)\s{1,3}){0,3}sub-?domains?\s{1,3}"
                              r"(?:of|under|on|for)(?:\s{1,3}the)?(?:\s{1,3}domain)?$")


def _spans(pattern: re.Pattern, text: str) -> tuple[list[tuple[int, int]], str]:
    spans = [m.span() for m in pattern.finditer(text)]
    for s, e in spans:
        text = _blank(text, s, e)
    return spans, text


def _mark_listed(row: _Row, skip: int = 0) -> None:
    """A token is listed when it opens its cell, alone or after a short label, or follows a listed one
    with nothing but a comma or an "and" between."""
    for offset, cell in row.cells:
        previous: _Token | None = None
        for t in (t for t in row.tokens if offset <= t.start < offset + len(cell)):
            before = row.text[max(offset, skip, previous.end if previous else 0):t.start]
            if previous is not None:
                t.listed = previous.listed and bool(_LIST_GLUE.match(before))
                t.refused = t.listed and previous.refused
            else:
                lead = before.strip().strip("([{<\"'\u201c\u2018").strip()
                t.listed = (not lead or (len(lead) <= 60 and bool(
                    _LEAD_LABEL.match(lead) or _LEAD_SUBDOMAINS.match(lead))))
                t.refused = t.listed and bool(_LEAD_SAYS_NO.search(lead) or _HINT.search(lead))
            previous = t
        # What the cell says beside its hosts, up to the first "except": a "no" there puts them in doubt.
        beside = cell
        for t in row.tokens:
            if offset <= t.start < offset + len(cell):
                beside = _blank(beside, t.start - offset, min(t.end - offset, len(cell)))
        excepted = _EXCEPT.search(beside)
        doubt = bool(_LEAD_SAYS_NO.search(beside[:excepted.start()] if excepted else beside))
        for t in row.tokens:
            if offset <= t.start < offset + len(cell):
                t.doubted = doubt


_COPULA = re.compile(r"(?i)\b(?:is|are|was|were|as|remains?|stays?|considered|deemed|counts?)\b")
_AFTER_IN_SCOPE = re.compile(r"""(?ix)^\s{0,3}
    (?:(?:targets?|assets?|domains?|hosts?|urls?|sites?|applications?|apps?|endpoints?|ips?|ranges?)\s{0,3})?
    (?:[:\u2013\u2014-]|(?:are|is|includes?|covers?)\b)""")


def _in_scope_said_of(text: str, token: _Token, cues: list[tuple[int, int]]) -> bool:
    """"api.example.com is in scope" and "In scope: api.example.com" say it of the host. "Issues in scope
    get a reward, see pay.example.net" only has the words and a host in one sentence."""
    for start, end in cues:
        if token.end <= start:
            between = text[token.end:start]
            if len(between) <= 80 and (not between.strip(" \t,;:()[]") or _COPULA.search(between)):
                return True
        elif end <= token.start and _AFTER_IN_SCOPE.match(text[end:token.start]):
            return True
    return False


def _said(text: str, tokens: list[_Token]) -> dict[int, tuple[str | None, bool]]:
    """Per token index: the side the words around it give ("in", "out" or None), and whether the
    sentence at least hints that the host is not to be touched."""
    text = _ABBREVIATION.sub(lambda m: m.group(0).replace(".", " "), text)
    neg, masked = _spans(_NEG, text)
    exc, masked = _spans(_EXCEPT, masked)
    pos, masked = _spans(_POS, masked)
    hint = [m.start() for m in _HINT.finditer(text)]
    cuts = [0] + [m.end() for m in _SENTENCE_END.finditer(masked)] + [len(text) + 1]
    out: dict[int, tuple[str | None, bool]] = {}
    for s, e in zip(cuts, cuts[1:], strict=False):
        inside = [(i, t) for i, t in enumerate(tokens) if s <= t.start < e]
        if not inside:
            continue
        if text[e - 1:e] == "?":            # "Is beta.example.com in scope?" says nothing either way
            out.update({i: (None, False) for i, _ in inside})
            continue
        cues = sorted([(a, "out") for a, _ in neg if s <= a < e] + [(a, "in") for a, _ in pos if s <= a < e])
        excepts = [a for a, _ in exc if s <= a < e]
        hinted = any(s <= a < e for a in hint)
        kinds = {k for _, k in cues}
        for i, t in inside:
            side: str | None = None
            if excepts:
                before = {k for a, k in cues if a < excepts[0]}
                base = next(iter(before)) if len(before) == 1 else None
                if t.start < excepts[0]:
                    side = base
                else:                       # "everything is out of scope except X" does not put X in scope
                    side = "out" if base != "out" else None
            elif len(kinds) == 1:
                side = next(iter(kinds))
            elif kinds:
                if cues[0][0] < inside[0][1].start:          # "In scope: a. Out of scope: b": a label leads
                    side = [k for a, k in cues if a < t.start][-1]
                else:                                        # "a is in scope and b is out of scope"
                    after = [k for a, k in cues if a > t.start]
                    side = after[0] if after else None
            if side == "in" and not _in_scope_said_of(text, t, [c for c in pos if s <= c[0] < e]):
                side = None
            out[i] = (side, hinted)
    return out


# ---------------------------------------------------------------------------------------------------------
# Headings and table cells

_CELL_IN = re.compile(r"(?i)^\W{0,3}in[\s-]{0,3}scope\W{0,3}$")
_CELL_OUT = re.compile(r"(?i)^\W{0,3}(?:out[\s-]{0,3}of[\s-]{0,3}scope|not\s{1,3}in\s{1,3}scope|out[\s-]scope"
                       r"|oos|excluded)\W{0,3}$")
_TYPE_CELL = re.compile(r"""(?ix)^(?:wildcard|domain|url|cidr|ip(?:\s+address(?:es)?)?|other(?:\s+(?:apk|ipa))?
    |api|website|web(?:\s+app(?:lication)?)?|mobile(?:\s+app(?:lication)?)?|android\b.{0,40}|ios\b.{0,40}|windows\b.{0,40}
    |source\s+code|(?:downloadable\s+)?executables?|hardware(?:/iot)?|iot|smart\s+contract|ai\s+model|testflight
    |(?:google\s+play|apple\s+store)\s+app\s+id
    |(?:website|api|android|ios|iot|hardware|network|mobile)\s+testing)$""")
_SEVERITY_CELL = re.compile(r"(?i)^(?:critical|high|medium|low|none|informational|eligible|ineligible|n/a|[-\u2013\u2014])$")
_NOT_A_HOST_ROW = re.compile(r"(?i)\b(?:android|ios|apk|ipa|app\s+store|play\s+store|testflight|executable|hardware"
                             r"|smart\s+contract|ai\s+model|google_play_app_id|apple_store_app_id)\b")
_HEAD_OUT = re.compile(r"""(?ix)\b(?:out[\s-]{0,3}of[\s-]{0,3}scopes?|not\s{1,3}in[\s-]{0,3}scope|out[\s-]scope|oos
    |off[\s-]{0,3}limits|excluded|exclusions?|(?:do\s{1,3}not|don[\u2019']t)\s{1,3}test|non[\s-]{0,3}qualifying
    |prohibited|forbidden)\b""")
_HEAD_IN = re.compile(r"(?i)\b(?:in[\s-]{0,3}scopes?|scopes?|tier\s{0,3}\d{1,2}|no\s{1,3}bounty)\b")
_FILLER = set("""the following these those all any our your list of for from in on to this that program programme
engagement section are is as be considered explicitly strictly currently only other additional main primary core
web mobile api apis and or targets target assets asset domains domain subdomains subdomain hosts host hostnames
urls url uris items item systems system applications application apps app ips ip addresses address ranges range
endpoints endpoint websites website sites site services service properties type types name names value values
tags tag description we consider please note eligible scope scopes""".split())
_ABOUT_BUGS = set("""vulnerability vulnerabilities vulns issues issue findings finding bugs bug reports report
submissions submission activities activity attacks attack testing tests techniques technique behaviour behavior
behaviours behaviors classes categories actions action""".split())
_OTHER_SECTION = set("""reward rewards bounty bounties payout payouts payment payments rule rules policy policies
disclosure guideline guidelines eligibility acknowledgements acknowledgments thanks harbor harbour legal faq contact
contacts about overview introduction background focus credentials account accounts access reporting requirements
issues update updates changelog announcements statistics stats response severity cvss terms conditions instructions
resources documentation references note notes rating ratings taxonomy impact methodology restrictions vulnerability
vulnerabilities qualifying timeline summary""".split())
_OTHER_SECTION_GLUE = set("""of and the our program scope test testing types table general known max last hall fame
safe areas efficiency targets range""".split())
_COUNT = re.compile(r"\s*\(\d{1,5}\)\s*$")
_WORD = re.compile(r"[A-Za-z']+")


def _heading(row: _Row) -> tuple[str | None, bool] | None:
    """(side, about bug classes) when the line opens a section, None when it is an ordinary line. A side
    of None closes the scope lists: what follows belongs to neither."""
    raw = " ".join(c.strip() for _, c in row.cells)
    text = _COUNT.sub("", raw.rstrip().rstrip(":").rstrip())
    words = _WORD.findall(text)
    if not words or len(words) > 14:
        return None
    colon = raw.rstrip().endswith(":")
    cue_out, cue_in = _HEAD_OUT.search(text), _HEAD_IN.search(text)
    cue = _HEAD_OUT if cue_out else _HEAD_IN if cue_in else None
    if cue is not None:
        rest = [w.lower() for w in _WORD.findall(cue.sub(" ", text))]
        bugs = any(w in _ABOUT_BUGS for w in rest)
        plain = all(w in _FILLER or w in _ABOUT_BUGS for w in rest)
        if cue_out and (colon or plain):
            return "out", bugs
        if not cue_out and plain:
            return (None, False) if bugs else ("in", False)
    lowered = [w.lower() for w in words]
    if (cue_out is None and row.single is not None and len(words) <= 4
            and any(w in _OTHER_SECTION for w in lowered)
            and all(w in _OTHER_SECTION or w in _OTHER_SECTION_GLUE for w in lowered)):
        return None, False
    return None


# ---------------------------------------------------------------------------------------------------------
# Notes

_RULE = re.compile(r"""(?ix)\b(?:rate[\s-]{0,3}limit\w{0,6}|requests?\s{1,3}(?:per|a|/)\s{0,3}(?:second|minute|hour)|rps
    |user[\s-]{0,3}agent|x-[a-z0-9-]{2,40}\s{0,3}:|(?:add|include|send|set|use|append)\b[^.]{0,60}\bheader
    |do\s{1,3}not|don[\u2019']t|must\s{1,3}not|never|prohibited|forbidden|not\s{1,3}(?:allowed|permitted|authori[sz]ed)
    |automated\s{1,3}(?:scann?\w{0,6}|tools?|testing)|scanners?|denial[\s-]of[\s-]service|d?dos
    |social\s{1,3}engineering|brute[\s-]{0,3}forc\w{0,4})\b""")
_CELL_WORDS = set("""application applications mobile web api general other infrastructure network domains domain url
urls wildcard targets target assets asset type tags name description coverage bounty""".split())


def _worth_a_note(text: str) -> bool:
    text = text.strip()
    if not 4 <= len(text) <= _MAX_NOTE or not re.search(r"[A-Za-z]{4}", text):
        return False
    return not (text.lower() in _CELL_WORDS or _TYPE_CELL.match(text) or _SEVERITY_CELL.match(text)
                or _CELL_IN.match(text) or _CELL_OUT.match(text))


def _notes(out_of_scope: list[str], rules: list[str]) -> str:
    def block(title: str, lines: list[str], most: int) -> str:
        lines = list(dict.fromkeys(x.strip() for x in lines))[:most]
        return title + "\n" + "\n".join(f"- {x}" for x in lines) if lines else ""
    rules = [r for r in rules if r.strip() not in {x.strip() for x in out_of_scope}]
    parts = [block("Out of scope (from the brief):", out_of_scope, _MAX_OUT_NOTES),
             block("Rules (from the brief):", rules, _MAX_RULES)]
    return "\n\n".join(p for p in parts if p)[:_MAX_NOTES]


# ---------------------------------------------------------------------------------------------------------
# The whole brief

class _Reader:
    def __init__(self) -> None:
        self.placed: list[_Placed] = []
        self.unsupported: list[str] = []
        self.out_notes: list[str] = []
        self.rules: list[str] = []
        self.count = 0
        self.sided = False        # the text gave a side somewhere: a heading, a status, or in words

    def place(self, token: _Token, side: str | None, weight: int) -> _Placed | None:
        self.count += 1
        if self.count > _MAX_TOKENS:
            return None
        if token.entry is None:
            if token.raw not in self.unsupported:
                self.unsupported.append(token.raw)
            return None
        self.sided = self.sided or side is not None
        p = _Placed(token.entry, side, weight, token.listed and not token.doubted)
        self.placed.append(p)
        return p

    def result(self) -> Brief:
        if not self.sided:        # a bare list of hosts, pasted as the scope: that is what it is
            for p in self.placed:
                if p.listed:
                    p.side, p.weight = "in", LISTED
        best: dict[str, list[_Placed]] = {}
        for p in self.placed:
            best.setdefault(p.entry, []).append(p)
        b = Brief(unsupported=self.unsupported[:_MAX_LIST], notes=_notes(self.out_notes, self.rules))
        for entry, all_placed in best.items():
            top = max(p.weight for p in all_placed)
            sides = {p.side for p in all_placed if p.weight == top}
            side = "out" if "out" in sides else "in" if "in" in sides else None
            if side == "in" and (_shared(entry) or _cited(entry)):
                side = None
            elif side is None and _cited(entry):
                continue
            {"in": b.in_scope, "out": b.out_scope, None: b.unplaced}[side].append(entry)
        b.unplaced = [e for e in b.unplaced if not _covered(e, b.in_scope + b.out_scope)]
        for name in ("in_scope", "out_scope", "unplaced"):
            setattr(b, name, getattr(b, name)[:_MAX_LIST])
        bases: dict[str, int] = {}
        for entry in b.in_scope:
            if _base(entry):
                bases[_base(entry)] = bases.get(_base(entry), 0) + 1
        b.name = max(bases, key=lambda k: bases[k]) if bases else ""
        return b


def _status(row: _Row) -> str | None:
    cells = [c.strip() for _, c in row.cells]
    if any(_CELL_OUT.match(c) for c in cells):
        return "out"
    return "in" if any(_CELL_IN.match(c) for c in cells) else None


def _csv(text: str) -> Brief | None:
    """HackerOne's scope export: one asset a row, and a column that says whether reports are taken."""
    head = text.lstrip().split("\n", 1)[0].lower()
    if "identifier" not in head or "eligible_for_submission" not in head:
        return None
    reader = _Reader()
    for record in csv.DictReader(io.StringIO(text.lstrip())):
        record = {(k or "").strip().lower(): (v or "") for k, v in record.items() if isinstance(v, str)}
        if _NOT_A_HOST_ROW.search(record.get("asset_type", "").replace("_", " ")):
            continue
        taken = record.get("eligible_for_submission", "").strip().lower()
        side = "in" if taken in ("true", "1", "yes") else "out" if taken in ("false", "0", "no") else None
        for token in _scan(record.get("identifier", "")[:_MAX_LINE]):
            token.listed = True
            reader.place(token, side, MARKED if side else UNSAID)
    return reader.result()


def _closes_a_row(rows: list[_Row], above: int | None, i: int, below: int | None, status: str) -> bool:
    """A status with no asset on its line belongs to the asset above it when the line is the tail of a
    table row: `Wildcard<tab>In scope`, or a cell of its own between a type cell and a severity cell.
    Anything else that reads "In scope" is a heading. "Out of scope" needs less: taking a host out by
    mistake costs a question, putting one in by mistake does not ask."""
    row = rows[i]
    if len(row.cells) > 1:
        others = [c.strip() for _, c in row.cells if not (_CELL_IN.match(c.strip()) or _CELL_OUT.match(c.strip()))]
        return status == "out" or any(_TYPE_CELL.match(c) or _SEVERITY_CELL.match(c) for c in others)
    typed = above is not None and bool(_TYPE_CELL.match(rows[above].single or ""))
    graded = below is not None and bool(_SEVERITY_CELL.match(rows[below].single or ""))
    return graded and (typed or status == "out")


def read(text: str) -> Brief:
    if len(text) > MAX_TEXT:
        raise ValueError(f"the brief is longer than {MAX_TEXT:,} characters; paste the scope part of it")
    export = _csv(text)
    if export is not None:
        return export
    rows = [_row(line) for line in _lines(text)]
    for row in rows:
        for offset, cell in row.cells:
            for t in _scan(cell):
                t.start, t.end = t.start + offset, t.end + offset
                row.tokens.append(t)
    filled = [i for i, r in enumerate(rows) if r.cells]
    before = {i: (filled[n - 1] if n else None) for n, i in enumerate(filled)}
    after = {i: (filled[n + 1] if n + 1 < len(filled) else None) for n, i in enumerate(filled)}

    reader = _Reader()
    side: str | None = None       # the section the lines are under
    bugs = False                  # an out-of-scope section about classes of bugs, not about hosts
    level = 0                     # the markdown level of the heading that opened it
    pending: list[tuple[int, _Placed]] = []      # (line, placement) a status further down may still mark

    for i in filled:
        row = rows[i]
        status = _status(row)
        if not row.tokens:
            if status and _closes_a_row(rows, before[i], i, after[i], status):
                for line, p in pending:
                    if i - line <= 12:
                        p.side, p.weight = status, MARKED
                reader.sided = True
                pending = []
                continue
            heading = _heading(row)
            if heading is None and row.heading_level and row.heading_level <= level:
                heading = (None, False)
            if heading is not None:
                side, bugs = heading
                level = row.heading_level
                reader.sided = reader.sided or side is not None
                pending = []
                continue
            if row.single is not None and _worth_a_note(row.single):
                if side == "out":
                    reader.out_notes.append(row.single)
                elif _RULE.search(row.single) and len(_WORD.findall(row.single)) >= 4:
                    reader.rules.append(row.single)
            continue

        # "Out of scope: a.example.com, b.example.com": the label holds for its own line
        here, here_bugs, skip = side, bugs, 0
        colon = row.text.find(":")
        if 0 < colon < row.tokens[0].start and len(row.cells) == 1:
            label = _heading(_Row(text=row.text[:colon + 1], cells=[(0, row.text[:colon + 1])]))
            if label is not None and label[0] is not None:
                here, here_bugs = label
                skip = colon + 1
                reader.sided = True
        _mark_listed(row, skip)
        asset_cell = next(o for o, c in row.cells if any(o <= t.start < o + len(c) for t in row.tokens))
        # `com.example.app<tab>Android: Play Store`: the type cell of the row says the name is an app
        an_app = any(o != asset_cell and len(c.strip()) <= 40 and _NOT_A_HOST_ROW.search(c) for o, c in row.cells)
        tokens = [t for t in row.tokens if not (an_app and t.bare) and not (t.file_like and not t.listed)]
        said = _said(row.text[skip:], [_Token(t.start - skip, t.end - skip, t.entry, t.raw) for t in tokens])
        if status:
            pending = []
            reader.sided = True
        for n, t in enumerate(tokens):
            words, hinted = said.get(n, (None, False))
            if status:
                if asset_cell <= t.start < asset_cell + len(dict(row.cells)[asset_cell]):
                    reader.place(t, status, MARKED)
                elif status == "out" or words == "out":
                    reader.place(t, "out", HINTED)
                else:
                    reader.place(t, None, UNSAID)
                continue
            if words == "out":
                reader.place(t, "out", MARKED if t.listed else SAID)
            elif t.refused:
                reader.place(t, "out", LISTED)
            elif words == "in" and here != "out":
                reader.place(t, "in", LISTED if t.listed else SAID)
            elif t.listed and here == "in":      # the plain case; a "no" on the line leaves it to the owner
                p = reader.place(t, None, UNSAID) if hinted or t.doubted else reader.place(t, "in", LISTED)
                if p:
                    pending.append((i, p))
            elif t.listed and here == "out" and not here_bugs:
                p = reader.place(t, "out", LISTED)
                if p:
                    pending.append((i, p))
            elif hinted or (here == "out" and not here_bugs):
                reader.place(t, "out", HINTED)
            else:
                p = reader.place(t, None, UNSAID)
                if p and t.listed:
                    pending.append((i, p))
        if row.single is not None and _worth_a_note(row.single):
            sentence = not all(t.listed for t in tokens)
            if here == "out" and sentence and skip == 0:
                reader.out_notes.append(row.single)
            elif _RULE.search(row.single) and len(_WORD.findall(row.single)) >= 4:
                reader.rules.append(row.single)
    return reader.result()
