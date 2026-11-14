"""The ported math must give what the original anbu-system-one/lib/math.js gives.
The fixtures were produced by running the original, untouched file."""
import json
from pathlib import Path

import pytest

from hunterai.decision import math as m

FIX = json.loads((Path(__file__).parent / "data" / "system_one_fixtures.json").read_text())


def same(got, want):
    """Equal structures; numbers within float noise."""
    if isinstance(want, dict):
        assert isinstance(got, dict) and set(got) == set(want), (got, want)
        for k in want:
            same(got[k], want[k])
    elif isinstance(want, float) or isinstance(got, float):
        assert got == pytest.approx(want, abs=1e-9), (got, want)
    else:
        assert got == want, (got, want)


def test_thresholds_and_gate():
    assert (m.ACT_ABOVE, m.REVIEW_ABOVE) == (FIX["thresholds"]["act"], FIX["thresholds"]["review"])
    for confidence, action in FIX["gate"]:
        assert m.gate(confidence) == action


@pytest.mark.parametrize("raw,want", FIX["noul"])
def test_process_noul_matches_the_original(raw, want):
    same(m.process_noul(raw), want)


@pytest.mark.parametrize("raw,want", FIX["choice"])
def test_process_choice_matches_the_original(raw, want):
    same(m.process_choice(raw, FIX["choiceQ"]), want)


@pytest.mark.parametrize("raw,want", FIX["score"])
def test_process_score_matches_the_original(raw, want):
    same(m.process_score(raw, FIX["scoreQ"]), want)


def test_booleans_are_not_numbers():
    assert m.process_noul(True)["invalid"] is True
    assert m.process_choice({"accept": True, "recheck": 0.5, "reject": 0.5}, FIX["choiceQ"])["invalid"] is True
