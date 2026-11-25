import os

import pytest
import pytest_asyncio

from hunterai import db


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Every test gets its own data dir and env file; the real ones are never touched."""
    home = tmp_path / "home"
    env_file = tmp_path / "test.env"
    env_file.write_text("")
    monkeypatch.setenv("HUNTERAI_HOME", str(home))
    monkeypatch.setenv("HUNTERAI_ENV_FILE", str(env_file))
    monkeypatch.setenv("HUNTERAI_MCP", "off")   # never start the owner's MCP servers in tests
    monkeypatch.setenv("HUNTERAI_CAIDO", "off")  # never touch a Caido that runs on this machine
    monkeypatch.setenv("HUNTERAI_ANSWER_CHECK", "off")   # no extra decision call after each run unless a test asks
    monkeypatch.setenv("HUNTERAI_VECTOR", "memory")   # no Docker, no real embedding model
    monkeypatch.setenv("HUNTERAI_FAKE_EMBED", "1")
    monkeypatch.setenv("HUNTERAI_OCR", "off")         # no external OCR programs unless a test asks
    monkeypatch.setenv("HUNTERAI_FAKE_FREE_BYTES", str(1 << 40))   # the test folder's disk decides nothing
    for name in ("HUNTERAI_PORT", "HUNTERAI_DEV", "HUNTERAI_CHUNKER", "HUNTERAI_SEARXNG_URL",
                 "ABLIT_API_KEY", "ABLIT_MODEL", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
                 "XAI_API_KEY", "GEMINI_API_KEY", "OLLAMA_HOST", "HUNTERAI_MOCK",
                 "HUNTERAI_ABLIT_BASE_URL", "HUNTERAI_QDRANT_URL", "HUNTERAI_FAKE_EMBED_DELAY",
                 "HUNTERAI_FAKE_EMBED_LOG", "HUNTERAI_COLIBRI_URL", "COLIBRI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    yield home
    # A test that undoes every patch (monkeypatch.undo()) would go on in the owner's real
    # data folder: fail loudly instead.
    assert os.environ.get("HUNTERAI_HOME") == str(home), "a test dropped the isolated home"


@pytest_asyncio.fixture
async def conn(isolated_home):
    c = await db.connect()
    await db.migrate(c)
    yield c
    await c.close()


@pytest.fixture(autouse=True)
def no_project_bin(tmp_path, monkeypatch):
    """The programs the owner keeps in the project's bin/ are not part of any test."""
    from hunterai import toolbox
    monkeypatch.setattr(toolbox, "PROJECT_BIN", tmp_path / "project-bin")


@pytest.fixture(autouse=True)
def clean_registry():
    from hunterai.providers import registry
    yield
    registry._overrides.clear()
    registry._instances.clear()


@pytest.fixture(autouse=True)
def clean_tools():
    from hunterai.tools import registry
    yield
    registry.reset()


@pytest.fixture(autouse=True)
def no_certificate_handshakes(monkeypatch):
    """No test makes a TLS handshake with a site of its own accord: a certificate is found good unless a test
    puts the real check back (hunterai.certs.HANDSHAKE) or says otherwise."""
    from hunterai import certs

    async def good(host, ip, port):
        return None
    monkeypatch.setattr(certs, "_handshake", good)
    certs._good.clear()     # noqa: SLF001


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    """A test that reaches the real resolver fails."""
    from hunterai.tools import resolve

    async def refuse(host):
        raise AssertionError(f"test tried a real DNS lookup: {host}")
    monkeypatch.setattr(resolve, "_getaddrinfo", refuse)


@pytest.fixture
def dns(no_dns, monkeypatch):
    """(table, lookups): table[host] is a list of answers, one per lookup, the last repeating;
    an answer is one address or a list of addresses. Unknown names raise OSError."""
    from hunterai.tools import resolve
    table: dict = {}
    lookups: list = []
    seen: dict = {}

    async def lookup(host):
        lookups.append(host)
        if host not in table:
            raise OSError(f"no such host: {host}")
        n = seen.get(host, 0)
        seen[host] = n + 1
        answer = table[host][min(n, len(table[host]) - 1)]
        return [answer] if isinstance(answer, str) else list(answer)
    monkeypatch.setattr(resolve, "_getaddrinfo", lookup)
    return table, lookups


@pytest.fixture(autouse=True)
def clean_permissions():
    from hunterai import permissions
    yield
    permissions.reset()


@pytest_asyncio.fixture(autouse=True)
async def clean_procs():
    """A test that fails before its own kill leaves no child process behind."""
    from hunterai import procs
    yield
    await procs.shutdown()
    procs.reset()
    from hunterai import hop, upstream
    await hop.shutdown()           # a hop to the owner's proxy belongs to the test that asked for it
    upstream._logins.clear()       # and so does a login it read
    from hunterai.tools import search
    search.set_provider(None)      # a fake provider never leaks into another test
    from hunterai import weblookup
    weblookup.reset()              # nor does the pause after a web search that did not answer


@pytest_asyncio.fixture(autouse=True)
async def runs_end_with_their_test():
    """A test may stop looking at a run once it announced its end. The run is waited for all the same: it
    is still closing its database connection, and a loop that closed under it would fail a later test
    with "Event loop is closed"."""
    yield
    from tests.toolkit import runs_have_ended
    await runs_have_ended()


@pytest.fixture(autouse=True)
def gate_off(monkeypatch):
    """The risk gate asks a model; only the tests about it turn it on."""
    from hunterai.tools import runner
    monkeypatch.setattr(runner, "GATE_DEFAULT", False)


@pytest.fixture(autouse=True)
def machine_of_no_temperature(tmp_path, monkeypatch):
    """No test reads how hot the machine is that it runs on: a suite at full load on a laptop would wait for it
    to cool, and stop runs when it does not. A test about heat sets the readings it wants."""
    from hunterai import thermal
    monkeypatch.setattr(thermal, "HWMON", tmp_path / "no-hwmon")
    monkeypatch.setattr(thermal, "NVIDIA_SMI", "no-nvidia-smi-in-a-test")
    monkeypatch.setattr(thermal, "_gpu", None)


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    """fake_bin(name, script): an executable shell script on PATH; FAKE_OUT points at tmp_path."""
    def make(name, script):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        f = bin_dir / name
        f.write_text(script)
        f.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
        monkeypatch.setenv("FAKE_OUT", str(tmp_path))
        return tmp_path
    return make


@pytest_asyncio.fixture
async def col(conn):
    from tests.kb_samples import make_collection
    return await make_collection(conn)


@pytest.fixture
def embed_spy(monkeypatch):
    """Wraps the fake embedder: records batches, can fail, can act after a successful call."""
    from hunterai.kb import embed
    from tests.kb_samples import EmbedSpy
    spy, real = EmbedSpy(), embed.FakeEmbedder.embed

    async def wrapped(self, texts):
        if spy.fail is not None and (spy.fail_times is None or spy.fail_times > 0):
            if spy.fail_times is not None:
                spy.fail_times -= 1
            raise spy.fail
        spy.batches.append(list(texts))
        out = await real(self, texts)
        if spy.on_call is not None:
            await spy.on_call(len(spy.batches))
        return out
    monkeypatch.setattr(embed.FakeEmbedder, "embed", wrapped)
    return spy
