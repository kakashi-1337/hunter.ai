"""scripts/searxng.sh start, run for real against a stand-in for Docker: a container that cannot be reached is
made again, and one that will not stay up says why. Before, both ended in one line of Docker's own."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "searxng.sh"
IMAGE = "searxng/searxng:2026.11.12-de722ab1b"
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is not installed")

DOCKER = r'''#!/usr/bin/env bash
# A stand-in for docker: one container, described by the files in $FAKE.
S="$FAKE"; echo "docker $*" >> "$S/calls"
has() { [ -f "$S/exists" ]; }
case "$1" in
  container) has ;;                                              # container inspect NAME
  inspect)
    has || { echo "Error: No such object: $4" >&2; exit 1; }
    case "$3" in
      *PortBindings*) cat "$S/binding" 2>/dev/null; echo ;;
      *Running*) if [ -f "$S/running" ]; then echo true; else echo false; fi ;;
      *Image*) cat "$S/image" ;;
    esac ;;
  start) has || exit 1; [ -f "$S/dies" ] || touch "$S/running" ;;
  run)
    touch "$S/exists"; args="$*"
    case "$args" in *"-p 127.0.0.1:8888:8080"*) echo "127.0.0.1:8888" > "$S/binding" ;; *) : > "$S/binding" ;; esac
    echo "${@: -1}" > "$S/image"
    [ -f "$S/dies" ] || touch "$S/running"
    echo 0123456789ab ;;
  rm) rm -f "$S/exists" "$S/running" "$S/binding" "$S/image"; echo hunterai-searxng ;;
  port)
    if [ -f "$S/running" ] && [ -s "$S/binding" ]; then cat "$S/binding"; else echo "no public port '8080/tcp' published for $2" >&2; exit 1; fi ;;
  logs) echo "settings.yml: line 3: mapping values are not allowed here" ;;
  stop) rm -f "$S/running" ;;
esac
'''
CURL = r'''#!/usr/bin/env bash
# Answers when the container runs and publishes 127.0.0.1:8888.
[ -f "$FAKE/running" ] && [ "$(cat "$FAKE/binding" 2>/dev/null)" = "127.0.0.1:8888" ]
'''


@pytest.fixture
def machine(tmp_path):
    """run(): the script on a machine whose docker and curl are the stand-ins. have(...): the container that is there."""
    tools, state, home = tmp_path / "tools", tmp_path / "state", tmp_path / "home"
    for d in (tools, state, home):
        d.mkdir()
    for name, text in (("docker", DOCKER), ("curl", CURL), ("sleep", "#!/usr/bin/env bash\n")):
        (tools / name).write_text(text)
        (tools / name).chmod(0o755)

    def have(binding: str = "127.0.0.1:8888", image: str = IMAGE, running: bool = False, dies: bool = False):
        (state / "exists").touch()
        (state / "binding").write_text(binding + "\n" if binding else "")
        (state / "image").write_text(image + "\n")
        if running:
            (state / "running").touch()
        if dies:
            (state / "dies").touch()

    def run(*args):
        env = {**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "FAKE": str(state), "HUNTERAI_HOME": str(home)}
        r = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)
        calls = (state / "calls").read_text().splitlines() if (state / "calls").exists() else []
        return r.returncode, (r.stdout + r.stderr).strip(), calls
    return type("M", (), {"run": staticmethod(run), "have": staticmethod(have), "state": state, "home": home})


def test_without_a_container_one_is_made_on_loopback(machine):
    code, said, calls = machine.run("start")
    assert code == 0 and said == "searxng: ready on http://127.0.0.1:8888", said
    made = [c for c in calls if c.startswith("docker run ")]
    assert len(made) == 1 and "-p 127.0.0.1:8888:8080" in made[0] and made[0].endswith(IMAGE)
    assert (machine.home / "searxng" / "settings.yml").read_text().count("secret_key") == 1


def test_a_container_that_is_right_is_started_and_not_made_again(machine):
    machine.have()
    code, said, calls = machine.run("start")
    assert code == 0 and said == "searxng: ready on http://127.0.0.1:8888", said
    assert "docker start hunterai-searxng" in calls and not [c for c in calls if c.startswith(("docker run ", "docker rm "))]


def test_a_container_that_publishes_no_port_is_made_again(machine):
    """It runs, and nothing can reach it: `docker start` leaves it as it was made."""
    machine.have(binding="", running=True)
    code, said, calls = machine.run("start")
    assert code == 0, said
    assert said.splitlines() == [
        f"searxng: the container hunterai-searxng does not publish 127.0.0.1:8888, so nothing can reach it. It is made again; "
        f"its settings are kept in {machine.home}/searxng.",
        "searxng: ready on http://127.0.0.1:8888"]
    assert "docker rm -f hunterai-searxng" in calls and [c for c in calls if c.startswith("docker run ")]
    assert "no public port" not in said


def test_a_container_published_to_the_network_is_made_again_on_loopback(machine):
    machine.have(binding="0.0.0.0:8888")
    code, said, calls = machine.run("start")
    assert code == 0 and "does not publish 127.0.0.1:8888" in said and "docker rm -f hunterai-searxng" in calls
    assert (machine.state / "binding").read_text().strip() == "127.0.0.1:8888"


def test_a_container_of_another_version_is_made_again_from_the_one_this_script_names(machine):
    machine.have(image="searxng/searxng:2025.1.1-0000000")
    code, said, calls = machine.run("start")
    assert code == 0 and said.splitlines()[0] == (
        f"searxng: the container hunterai-searxng was made from searxng/searxng:2025.1.1-0000000, and this version of HunterAI "
        f"uses {IMAGE}. It is made again; its settings are kept in {machine.home}/searxng.")
    assert (machine.state / "image").read_text().strip() == IMAGE and "docker rm -f hunterai-searxng" in calls


def test_a_container_that_does_not_stay_up_says_why(machine):
    machine.have(dies=True)
    code, said, _ = machine.run("start")
    assert code == 1
    assert said.splitlines() == ["searxng: the container hunterai-searxng stopped right after it was started. Its last lines:",
                                 "settings.yml: line 3: mapping values are not allowed here"]


def test_status_and_stop_are_what_they_were(machine):
    assert machine.run("status")[:2] == (1, "searxng: not running")
    machine.have(running=True)
    assert machine.run("status")[:2] == (0, "searxng: ready")
    assert machine.run("stop")[:2] == (0, "searxng: stopped")
    assert machine.run()[0] == 2 and machine.run()[1] == "usage: scripts/searxng.sh start | stop | status"
