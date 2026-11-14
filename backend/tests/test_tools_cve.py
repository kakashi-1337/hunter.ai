import asyncio
import json
import os
import time

import httpx
import pytest
import respx

from hunterai import modes
from hunterai.providers.base import ToolCall
from hunterai.tools import cve, registry
from tests.toolkit import answer_next, run_call, seeded_run


def record(cid, score=7.7, severity="HIGH", text="A flaw in the resolver.", cwe="CWE-193", refs=()):
    return {"cve": {"id": cid, "published": "2021-06-01T13:15:07.853", "vulnStatus": "Analyzed",
                    "descriptions": [{"lang": "es", "value": "Un fallo"}, {"lang": "en", "value": text}],
                    "metrics": {"cvssMetricV2": [{"cvssData": {"version": "2.0", "baseScore": 6.8, "vectorString": "AV:N"}, "baseSeverity": "MEDIUM"}],
                                "cvssMetricV31": [{"type": "Primary", "cvssData": {"version": "3.1", "baseScore": score, "baseSeverity": severity,
                                                                                   "vectorString": "CVSS:3.1/AV:N/AC:H"}}]},
                    "weaknesses": [{"description": [{"lang": "en", "value": cwe}]}],
                    "configurations": [{"nodes": [{"cpeMatch": [
                        {"vulnerable": True, "criteria": "cpe:2.3:a:openresty:openresty:*:*:*:*:*:*:*:*", "versionEndExcluding": "1.19.3.2"},
                        {"vulnerable": True, "criteria": "cpe:2.3:a:f5:nginx:*:*:*:*:*:*:*:*", "versionStartIncluding": "0.6.18", "versionEndExcluding": "1.20.1"},
                        {"vulnerable": False, "criteria": "cpe:2.3:o:linux:linux_kernel:-:*:*:*:*:*:*:*"}]}]}],
                    "references": [{"url": "https://example.org/blog", "tags": []}, *refs]}}


KEV = {"vulnerabilities": [{"cveID": "CVE-2021-44228", "dateAdded": "2021-12-10", "knownRansomwareCampaignUse": "Known",
                            "vulnerabilityName": "Log4Shell"}]}


@pytest.fixture(autouse=True)
def registered(monkeypatch):
    for t in cve.tools():
        registry.register(t)
    monkeypatch.setattr(cve, "_kev", None)
    monkeypatch.setattr(cve, "PAUSE", 0)


def services(nvd, epss=None, kev=KEV):
    n = respx.get(cve.NVD).mock(side_effect=nvd if callable(nvd) else None, return_value=None if callable(nvd) else httpx.Response(200, json=nvd))
    e = respx.get(cve.EPSS).mock(return_value=httpx.Response(200, json={"data": epss or []}))
    k = respx.get(cve.KEV).mock(return_value=httpx.Response(200, json=kev) if kev is not None else httpx.Response(503))
    return n, e, k


async def use(conn, **args):
    chat, run_id = await seeded_run(conn)
    task = asyncio.create_task(run_call(conn, chat, run_id, ToolCall("c", "cve", args)))
    req = await answer_next(run_id, "allow")
    out, _, _ = await task
    return out.block, req


@respx.mock
async def test_a_product_and_version_gets_its_known_vulnerabilities_the_worst_first(conn):
    n, e, k = services({"totalResults": 3, "vulnerabilities": [record("CVE-2021-23017"), record("CVE-2021-44228", 10.0, "CRITICAL"),
                                                                 record("CVE-2022-41741", 7.0, text="Memory corruption in the mp4 module.")]},
                       epss=[{"cve": "CVE-2021-23017", "epss": "0.534610000", "percentile": "0.990060000"},
                             {"cve": "CVE-2022-41741", "epss": "0.001", "percentile": "0.30"}])
    block, req = await use(conn, action="product", product="Nginx", version="1.18.0")
    assert req.preview.startswith("CVE lookup: nginx 1.18.0") and not block.is_error
    assert dict(n.calls.last.request.url.params)["virtualMatchString"] == "cpe:2.3:a:*:nginx:1.18.0"
    assert sorted(dict(e.calls.last.request.url.params)["cve"].split(",")) == ["CVE-2021-23017", "CVE-2021-44228", "CVE-2022-41741"]
    out = block.content
    assert "3 known vulnerabilities" in out and cve.NOTE in out
    # In the list of exploited ones first, then by the chance of exploitation.
    assert out.index("CVE-2021-44228") < out.index("CVE-2021-23017") < out.index("CVE-2022-41741")
    assert "CVSS 10.0 CRITICAL" in out and "KEV: exploited in the wild (added 2021-12-10, ransomware: Known)" in out
    assert "EPSS 0.535 (99th percentile)" in out and "CWE-193" in out and "A flaw in the resolver." in out and "Un fallo" not in out
    assert "affected: f5 nginx >=0.6.18 <1.20.1; openresty openresty <1.19.3.2" in out and "linux_kernel" not in out   # the product asked about first


@respx.mock
async def test_ids_are_looked_up_with_their_references(conn):
    def nvd(request):
        cid = request.url.params["cveId"]
        if cid == "CVE-2099-0001":
            return httpx.Response(200, json={"totalResults": 0, "vulnerabilities": []})
        return httpx.Response(200, json={"vulnerabilities": [record(cid, refs=(
            {"url": "https://example.org/poc", "tags": ["Exploit", "Third Party Advisory"]}, {"url": "https://example.org/fix", "tags": ["Patch"]}))]})
    services(nvd)
    block, _ = await use(conn, action="lookup", ids=["cve-2021-23017", "CVE-2099-0001"])
    out = block.content
    assert "[Exploit] https://example.org/poc" in out and "[Patch] https://example.org/fix" in out
    assert out.index("example.org/poc") < out.index("example.org/blog")            # the useful ones first
    assert "CVE-2099-0001: not in the NVD" in out and "KEV: not listed" in out


@respx.mock
async def test_a_keyword_search_and_what_is_said_when_a_source_is_down(conn):
    n, e, k = services({"totalResults": 40, "vulnerabilities": [record("CVE-2021-23017")]}, kev=None)
    respx.get(cve.EPSS).mock(return_value=httpx.Response(500))
    block, _ = await use(conn, action="search", query="nginx resolver", limit=5)
    params = dict(n.calls.last.request.url.params)
    assert params["keywordSearch"] == "nginx resolver" and params["resultsPerPage"] == "5"
    out = block.content
    assert "1 of 40" in out and "KEV: not checked (the list could not be fetched)" in out and "EPSS not available" in out


@respx.mock
async def test_errors_are_plain(conn):
    for args, said in (({"action": "product", "product": "nginx"}, "needs a version"), ({"action": "lookup", "ids": ["nope"]}, "not a CVE id"),
                       ({"action": "product", "product": "a b:c", "version": "1"}, "one word"), ({"action": "search"}, "needs a query")):
        chat, run_id = await seeded_run(conn)
        out, _, _ = await run_call(conn, chat, run_id, ToolCall("c", "cve", args))
        assert out.block.is_error and said in out.block.content
    services(lambda request: httpx.Response(403, text="rate limit"))
    block, _ = await use(conn, action="search", query="x")
    assert block.is_error and "NVD" in block.content and "rate limit" in block.content.lower()
    block, _ = await use(conn, action="product", product="nginx", version="9.9")
    assert block.is_error


@respx.mock
async def test_a_stalled_nvd_is_asked_once_more(conn):
    answers = iter([httpx.ReadTimeout("slow"), httpx.Response(200, json={"totalResults": 1, "vulnerabilities": [record("CVE-2021-23017")]})])

    def nvd(request):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a
    n, _, _ = services(nvd)
    block, _ = await use(conn, action="search", query="nginx")
    assert not block.is_error and n.call_count == 2 and "CVE-2021-23017" in block.content
    respx.get(cve.NVD).mock(side_effect=httpx.ReadTimeout("slow"))
    block, _ = await use(conn, action="search", query="nginx")
    assert block.is_error and "twice" in block.content


@respx.mock
async def test_the_exploited_list_is_kept_for_a_day(conn):
    n, e, k = services({"totalResults": 1, "vulnerabilities": [record("CVE-2021-23017")]})
    await use(conn, action="search", query="a")
    await use(conn, action="search", query="b")
    assert k.call_count == 1 and cve.cache_file().exists()
    cve._kev = None                                # a restart: the file is used
    await use(conn, action="search", query="c")
    assert k.call_count == 1
    old = time.time() - 2 * 86400
    os.utime(cve.cache_file(), (old, old)); cve._kev = None
    await use(conn, action="search", query="d")
    assert k.call_count == 2


def test_the_model_is_told_to_ask_before_naming_a_cve():
    assert "cve" in modes.orchestrator_block(["cve"]) and "never name a CVE" in modes.orchestrator_block(["cve"])
