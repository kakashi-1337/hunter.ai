"""scripts/searxng.sh start, run for real against a stand-in for Docker: a container that cannot be reached is
made again, and one that will not stay up says why. Before, both ended in one line of Docker's own.

A container can also be made right and still be out of reach: Docker 29 was seen running one that was attached to
no network (`ports={} nets=`), with no published port, no address and no name lookups, and a restart kept it so.
The script looks at what is published now, not only at how the container was made."""
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
    [ ! -f "$S/stuck" ] || touch "$S/detached"                   # this Docker attaches no container to a network
    echo 0123456789ab ;;
  rm) rm -f "$S/exists" "$S/running" "$S/binding" "$S/image" "$S/detached"; echo hunterai-searxng ;;
  port)                                                          # what is published now, not how it was made
    if [ -f "$S/running" ] && [ -s "$S/binding" ] && [ ! -f "$S/detached" ]; then cat "$S/binding"
    else echo "no public port '8080/tcp' published for $2" >&2; exit 1; fi ;;
  logs) echo "settings.yml: line 3: mapping values are not allowed here" ;;
  stop) rm -f "$S/running" ;;
esac
'''
CURL = r'''#!/usr/bin/env bash
# Answers when the container runs, publishes 127.0.0.1:8888 and is attached to its network. $FAKE/curl_says: what
# it prints, failing, in place of an answer.
echo "curl $*" >> "$FAKE/curl_calls"
if [ -f "$FAKE/curl_says" ]; then cat "$FAKE/curl_says" >&2; exit 22; fi
[ -f "$FAKE/running" ] && [ ! -f "$FAKE/detached" ] && [ "$(cat "$FAKE/binding" 2>/dev/null)" = "127.0.0.1:8888" ]
'''


CLOCK = r'''#!/usr/bin/env bash
# The clock, which a test moves: every look at it is $FAKE/clock_step seconds later than the one before.
now=$(( $(cat "$FAKE/clock" 2>/dev/null || echo 1000) + $(cat "$FAKE/clock_step" 2>/dev/null || echo 0) ))
echo "$now" > "$FAKE/clock"; echo "$now"
'''


@pytest.fixture
def machine(tmp_path):
    """run(): the script on a machine whose docker and curl are the stand-ins. have(...): the container that is there."""
    tools, state, home = tmp_path / "tools", tmp_path / "state", tmp_path / "home"
    for d in (tools, state, home):
        d.mkdir()
    for name, text in (("docker", DOCKER), ("curl", CURL), ("date", CLOCK), ("sleep", "#!/usr/bin/env bash\n")):
        (tools / name).write_text(text)
        (tools / name).chmod(0o755)

    def have(binding: str = "127.0.0.1:8888", image: str = IMAGE, running: bool = False, dies: bool = False,
             detached: bool = False):
        (state / "exists").touch()
        (state / "binding").write_text(binding + "\n" if binding else "")
        (state / "image").write_text(image + "\n")
        if running:
            (state / "running").touch()
        if dies:
            (state / "dies").touch()
        if detached:                        # made right, and attached to no network: no port of it is published
            (state / "detached").touch()

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


def test_a_container_that_runs_without_its_published_port_is_made_again(machine):
    """It was made with the port, so how it was made says nothing is wrong. It is attached to no network."""
    machine.have(running=True, detached=True)
    code, said, calls = machine.run("start")
    assert code == 0, said
    made_again = ("searxng: the container hunterai-searxng runs, and Docker publishes no port for it: it is attached to no "
                  "network, so nothing can reach it and it can look up no name. It is made again; its settings are kept in "
                  f"{machine.home}/searxng.")
    assert said.splitlines() == [made_again, "searxng: ready on http://127.0.0.1:8888"]
    assert calls.count("docker rm -f hunterai-searxng") == 1 and len([c for c in calls if c.startswith("docker run ")]) == 1
    assert calls.index("docker rm -f hunterai-searxng") > calls.index("docker start hunterai-searxng")


def test_when_a_new_container_gets_no_port_either_the_script_says_what_to_do_and_stops(machine):
    machine.have(running=True, detached=True)
    (machine.state / "stuck").touch()
    code, said, calls = machine.run("start")
    assert code == 1
    what_to_do = ("searxng: Docker publishes no port for the new container either. Restart Docker (sudo systemctl restart "
                  "docker) and start HunterAI again.")
    assert said.splitlines()[1:] == [what_to_do]
    assert len([c for c in calls if c.startswith("docker run ")]) == 1          # made again once, not for ever
    assert not (machine.state / "curl_calls").exists()                          # nothing is asked of a search nobody can reach


def test_a_new_container_is_looked_at_too(machine):
    """The first container of a machine whose Docker attaches none: the same sentence, and no second try."""
    (machine.state / "stuck").touch()
    code, said, calls = machine.run("start")
    assert code == 1 and len([c for c in calls if c.startswith("docker run ")]) == 2 and "docker rm -f hunterai-searxng" in calls
    assert said.splitlines()[-1].startswith("searxng: Docker publishes no port for the new container either.")


def test_a_search_that_does_not_answer_says_what_the_last_try_got(machine):
    machine.have()
    (machine.state / "curl_says").write_text("curl: (22) The requested URL returned error: 403\n")
    code, said, _ = machine.run("start")
    assert code == 1
    assert said.splitlines() == [
        "searxng: not answering after 60 s. The last try: curl: (22) The requested URL returned error: 403",
        "settings.yml: line 3: mapping values are not allowed here"]


def test_every_try_has_a_time_limit(machine):
    machine.have()
    (machine.state / "curl_says").write_text("curl: (28) Operation timed out\n")
    machine.run("start")
    tries = (machine.state / "curl_calls").read_text().splitlines()
    assert len(tries) == 60 and all(" -m 15 " in t for t in tries)
    assert machine.run("status")[:2] == (1, "searxng: not running")
    assert " -m 15 " in (machine.state / "curl_calls").read_text().splitlines()[-1]


def test_tries_that_take_long_end_by_the_clock(machine):
    """Sixty tries of fifteen seconds would be a quarter of an hour. The start gives up after a minute."""
    machine.have()
    (machine.state / "curl_says").write_text("curl: (28) Operation timed out after 15001 milliseconds\n")
    (machine.state / "clock_step").write_text("25")
    code, said, _ = machine.run("start")
    assert code == 1 and said.splitlines()[0].startswith("searxng: not answering after 60 s. The last try: curl: (28)")
    assert len((machine.state / "curl_calls").read_text().splitlines()) == 3      # at 25, 50 and 75 seconds


def test_status_and_stop_are_what_they_were(machine):
    assert machine.run("status")[:2] == (1, "searxng: not running")
    machine.have(running=True)
    assert machine.run("status")[:2] == (0, "searxng: ready")
    assert machine.run("stop")[:2] == (0, "searxng: stopped")
    assert machine.run()[0] == 2 and machine.run()[1] == "usage: scripts/searxng.sh start | stop | status"
