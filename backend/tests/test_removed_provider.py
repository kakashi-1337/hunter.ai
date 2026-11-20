"""The provider that ran the `claude` program under its own login was taken out of HunterAI. Nothing of it is left
in the package, and a chat that still names it is told what to do."""
import importlib.util
from pathlib import Path

import pytest

from hunterai import subagents
from hunterai.providers import registry
from tests.test_engine import go, setup  # noqa: F401 (setup is a fixture)

ROOT = Path(__file__).resolve().parents[2]
NAME = "claude_code"


def test_nothing_of_it_is_left_in_the_package_or_the_readme():
    assert importlib.util.find_spec(f"hunterai.providers.{NAME}") is None
    assert NAME not in registry.ORDER and NAME not in [p.name for p in registry.all()]
    with pytest.raises(KeyError):
        registry.get(NAME)
    assert NAME not in subagents.CLI_LOGINS
    assert NAME not in (ROOT / "README.md").read_text()


async def test_a_chat_that_still_names_it_is_told_to_pick_a_model(conn, setup):  # noqa: F811
    chat, user = await setup(provider=NAME, model="sonnet")
    run, msg = await go(conn, chat, user)
    assert run.status == "error" and msg.status == "error"
    assert f'the provider "{NAME}", which this HunterAI does not have' in run.error and "Pick a model" in run.error
