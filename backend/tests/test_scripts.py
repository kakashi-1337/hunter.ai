"""Text checks on the start scripts; nothing is executed."""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def test_searxng_script_binds_loopback_and_pins_the_image():
    text = (ROOT / "scripts" / "searxng.sh").read_text()
    assert "-p 127.0.0.1:8888:8080" in text and "searxng/searxng:2026.11.12-de722ab1b" in text
    assert "hunterai-searxng" in text and "formats" in text and "json" in text
    assert "--restart" not in text and "0.0.0.0" not in text


def test_start_scripts_sync_and_start_search():
    for name in ("start.sh", "dev.sh"):
        text = (ROOT / "scripts" / name).read_text()
        assert "uv sync" in text and "searxng.sh start" in text
    assert "HUNTERAI_DEV=1" in (ROOT / "scripts" / "dev.sh").read_text()
    assert "HUNTERAI_DEV" not in (ROOT / "scripts" / "start.sh").read_text()


def test_readme_states_the_known_limits_and_the_tools():
    text = (ROOT / "README.md").read_text()
    for tool in ("read", "write", "edit", "execute", "process_output", "process_input",
                 "process_kill", "web_fetch", "web_download", "web_search", "repo_search"):
        assert f"`{tool}`" in text
    assert "runs with the owner's full rights" in text and "mcp.json" in text
    assert "HUNTERAI_MCP" in text and "hunterai-searxng" in text


@pytest.mark.parametrize("name", ["qdrant.sh", "start.sh", "dev.sh", "deploy.sh"])
def test_scripts_parse(name):
    assert subprocess.run(["bash", "-n", str(SCRIPTS / name)]).returncode == 0


def test_qdrant_is_published_on_loopback_only():
    text = (SCRIPTS / "qdrant.sh").read_text()
    assert "-p 127.0.0.1:6333:6333" in text and "hunterai-qdrant" in text and "qdrant/qdrant:v1.20.0" in text and "mkdir -p" in text
    assert "0.0.0.0" not in text and "6334" not in text and "--restart" not in text


@pytest.mark.parametrize("name", ["start.sh", "dev.sh"])
def test_start_scripts_run_and_stop_the_worker(name):
    text = (SCRIPTS / name).read_text()
    assert all(s in text for s in ("python -m hunterai.worker", "qdrant.sh", "trap", "kill -KILL"))


def test_qdrant_script_does_nothing_in_memory_mode(tmp_path):
    r = subprocess.run(["bash", str(SCRIPTS / "qdrant.sh")], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "HUNTERAI_VECTOR": "memory"})
    assert r.returncode == 0 and "memory" in r.stdout and not (tmp_path / ".hunterai").exists()
