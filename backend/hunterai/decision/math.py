"""Probability math and answer processing: a port of anbu-system-one/lib/math.js.

Pure functions, no I/O. The original stays untouched as the reference; the tests compare
this file against outputs the original produced.
"""
from __future__ import annotations

import math

ACT_ABOVE = 0.8        # confidence at or above this: act
REVIEW_ABOVE = 0.5     # at or above this: review; below: abstain
YES_AT = 0.7
NO_AT = 0.3


def _r3(x: float) -> float:
    return math.floor(x * 1000 + 0.5) / 1000     # JavaScript's Math.round, not banker's rounding


def _number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def gate(confidence: float) -> str:
    return "act" if confidence >= ACT_ABOVE else "review" if confidence >= REVIEW_ABOVE else "abstain"


def normalize(raw, keys: list[str]) -> tuple[dict[str, float], bool, float]:
    """(probabilities over every expected key summing to 1, invalid, normalization error).
    invalid is True when no usable number was given, or when a key was missing."""
    ok = isinstance(raw, dict)
    out: dict[str, float] = {}
    present = 0
    for k in keys:
        v = raw.get(k) if ok else None
        if _number(v):
            out[k] = max(0.0, float(v))
            present += 1
        else:
            out[k] = 0.0
    total = sum(out.values())
    if not present or total == 0:
        u = 1 / len(keys)
        return {k: u for k in keys}, True, 1.0
    error = abs(total - 1)
    probs = {k: out[k] / total for k in keys} if error > 1e-4 else out
    return probs, present < len(keys), error


def choice_confidence(probs: dict[str, float]) -> float:
    vals = list(probs.values())
    if len(vals) <= 1:
        return 1.0
    u = 1 / len(vals)
    return max(0.0, (max(vals) - u) / (1 - u))


def score_confidence(probs: dict[str, float]) -> float:
    vals = list(probs.values())
    if len(vals) <= 1:
        return 1.0
    mode = vals.index(max(vals))
    dist = sum(p * abs(i - mode) for i, p in enumerate(vals))
    center = (len(vals) - 1) / 2
    uniform_mad = sum(abs(i - center) for i in range(len(vals))) / len(vals)
    return 1.0 if uniform_mad == 0 else max(0.0, 1 - dist / uniform_mad)


def process_noul(raw) -> dict:
    ok = _number(raw)
    p = max(0.0, min(1.0, float(raw))) if ok else 0.5
    verdict = "yes" if p >= YES_AT else "no" if p <= NO_AT else "uncertain"
    out = {"probability": _r3(p), "verdict": verdict}
    if not ok:
        out["invalid"] = True
    return out


def process_choice(raw, q: dict) -> dict:
    keys = list(q["criteria"])
    probs, invalid, error = normalize(raw, keys)
    choice = keys[0]
    for k in keys:
        if probs[k] > probs[choice]:
            choice = k
    confidence = 0.0 if invalid else choice_confidence(probs)
    out = {"choice": choice, "confidence": _r3(confidence), "probabilities": probs,
           "action": "abstain" if invalid else gate(confidence)}
    if invalid:
        out["invalid"] = True
    if error > 1e-4:
        out["normalization_error"] = _r3(error)
    return out


def process_score(raw, q: dict) -> dict:
    levels = list(q["criteria"])
    keys = [str(i) for i in range(len(levels))]
    probs, invalid, error = normalize(raw, keys)
    score = sum(int(k) * probs[k] for k in keys)
    confidence = 0.0 if invalid else score_confidence(probs)
    out = {"score": _r3(score), "level": levels[min(len(levels) - 1, math.floor(score + 0.5))],
           "confidence": _r3(confidence), "probabilities": probs,
           "legend": {str(i): c for i, c in enumerate(levels)},
           "action": "abstain" if invalid else gate(confidence)}
    if invalid:
        out["invalid"] = True
    if error > 1e-4:
        out["normalization_error"] = _r3(error)
    return out
