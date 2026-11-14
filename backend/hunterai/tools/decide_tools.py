"""System One's six decision tools, as native tools on the decision engine."""
from __future__ import annotations

import json

from .. import db
from ..decision import engine as de
from .base import Tool, ToolContext, ToolError, ToolResult

RANK_BATCH = 5
STATE = {"type": "string", "description": "Context to evaluate"}

GATE_QUESTIONS = {
    "risk": {"type": "score",
             "instructions": "How risky is it to run this action automatically, without a human "
                             "looking first?",
             "criteria": ["Safe - read-only, no side effects",
                          "Low risk - minor, easily reversible changes",
                          "Needs review - could affect important state or data",
                          "High risk - could destroy data, affect production, or leak secrets"]},
    "sensitive": {"type": "noul",
                  "instructions": "Does this action touch production systems, delete or overwrite "
                                  "data, send data to a third party, or access secrets/credentials?"},
    "recommendation": {"type": "choice", "instructions": "What should the system do with this action?",
                       "criteria": {"allow": "Safe to run automatically",
                                    "confirm": "Pause and ask a human to confirm",
                                    "block": "Block and require a safer approach"}},
}


async def ask(state, questions: dict) -> dict:
    """One decision call on a connection of its own (tool handlers have none)."""
    conn = await db.connect()
    try:
        return await de.decide(conn, state, questions)
    except ValueError as e:
        raise ToolError(str(e)) from None
    finally:
        await conn.close()


def _meta(res: dict) -> dict:
    keep = {k: res[k] for k in ("provider", "model", "usage", "ms", "fallback_from", "error") if k in res}
    return keep


def _done(payload: dict) -> ToolResult:
    return ToolResult(json.dumps(payload, indent=1, ensure_ascii=False))


async def gate(state) -> dict:
    """Risk-screen an action. Fails closed: an unusable reply recommends "confirm"."""
    return gate_result(await ask(state, GATE_QUESTIONS))


def gate_result(res: dict) -> dict:
    a = res["answers"]
    risk, sens, rec = a["risk"], a["sensitive"], a["recommendation"]
    invalid = bool(risk.get("invalid") or sens.get("invalid") or rec.get("invalid") or res.get("error"))
    out = {"risk_score": risk["score"], "risk_level": risk["level"], "risk_confidence": risk["confidence"],
           "touches_sensitive": sens["probability"], "sensitive_verdict": sens["verdict"],
           "recommendation": "confirm" if invalid else rec["choice"],
           "recommendation_confidence": rec["confidence"],
           "recommendation_probabilities": rec["probabilities"]}
    if invalid:
        out["invalid"] = True
    return {**out, **_meta(res)}


GATE_MAX_RISK = 1.5          # of 3: above this an "allow" is not taken at its word
GATE_MAX_SENSITIVE = 0.5
GATE_MIN_CONFIDENCE = 0.5


def gate_clears(g: dict) -> bool:
    """True only when every answer agrees the action is safe to run without a look."""
    return (not g.get("invalid") and g["recommendation"] == "allow"
            and g["recommendation_confidence"] >= GATE_MIN_CONFIDENCE
            and g["risk_score"] <= GATE_MAX_RISK and g["touches_sensitive"] <= GATE_MAX_SENSITIVE)


async def _check(ctx: ToolContext, args: dict) -> ToolResult:
    criteria = {k: args[f"{k}_criteria"] for k in ("true", "false") if args.get(f"{k}_criteria")}
    q = {"check": {"type": "noul", "instructions": args["question"], **({"criteria": criteria} if criteria else {})}}
    res = await ask(args["state"], q)
    return _done({**res["answers"]["check"], **_meta(res)})


async def _classify(ctx: ToolContext, args: dict) -> ToolResult:
    res = await ask(args["state"], {"classify": {"type": "choice", "instructions": args["question"],
                                                 "criteria": args["options"]}})
    return _done({**res["answers"]["classify"], **_meta(res)})


async def _score(ctx: ToolContext, args: dict) -> ToolResult:
    res = await ask(args["state"], {"score": {"type": "score", "instructions": args["question"],
                                              "criteria": args["levels"]}})
    return _done({**res["answers"]["score"], **_meta(res)})


async def _gate(ctx: ToolContext, args: dict) -> ToolResult:
    return _done(await gate(args["state"]))


async def _decide(ctx: ToolContext, args: dict) -> ToolResult:
    res = await ask(args["state"], args["questions"])
    return _done({"answers": res["answers"], **_meta(res)})


async def rank(items: list[str], question: str, levels: list[str], batch: int = RANK_BATCH) -> dict:
    ranked, usage, ms, last = [], {"input_tokens": 0, "output_tokens": 0}, 0, {}
    for start in range(0, len(items), batch):
        part = items[start:start + batch]
        state = "\n\n".join(f"[Item {start + i + 1}]\n{it}" for i, it in enumerate(part))
        questions = {f"item_{start + i + 1}": {
            "type": "score", "criteria": levels,
            "instructions": f"{question} Evaluate ONLY [Item {start + i + 1}], independently of the others."}
            for i in range(len(part))}
        res = await ask(state, questions)
        last = res
        usage = {k: usage[k] + res["usage"][k] for k in usage}
        ms += res["ms"]
        for i, item in enumerate(part):
            p = res["answers"][f"item_{start + i + 1}"]
            ranked.append({"item": item, "score": p["score"], "level": p["level"],
                           "confidence": p["confidence"], "action": p["action"],
                           **({"invalid": True} if p.get("invalid") else {})})
    ranked.sort(key=lambda r: (-r["score"], -r["confidence"]))
    for i, r in enumerate(ranked):
        r["rank"] = i + 1
    return {"ranked": ranked, "total": len(items), "usage": usage, "ms": ms,
            **{k: last[k] for k in ("provider", "model") if k in last}}


async def _rank(ctx: ToolContext, args: dict) -> ToolResult:
    out = await rank(args["items"], args["question"], args["levels"],
                     max(1, min(20, int(args.get("batch_size") or RANK_BATCH))))
    top = int(args.get("top_k") or 0)
    if top > 0:
        out["ranked"] = out["ranked"][:top]
    return _done(out)


LEVELS = {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 10,
          "description": "Ordered levels, low to high (2-10)"}


def _tool(name, description, properties, required, handler) -> Tool:
    return Tool(name, description, {"type": "object", "properties": properties, "required": required,
                                    "additionalProperties": False}, "read", handler)


def tools() -> list[Tool]:
    return [
        _tool("decide_check", "Yes/no probability on a context. Returns probability (0-1) and verdict "
              "yes/no/uncertain. Use it instead of guessing.",
              {"state": STATE, "question": {"type": "string"}, "true_criteria": {"type": "string"},
               "false_criteria": {"type": "string"}}, ["state", "question"], _check),
        _tool("decide_classify", "Pick one option. Returns the choice, confidence, the full probability "
              "distribution, and an action (act / review / abstain).",
              {"state": STATE, "question": {"type": "string"},
               "options": {"type": "object", "additionalProperties": {"type": "string"},
                           "minProperties": 2, "maxProperties": 20}},
              ["state", "question", "options"], _classify),
        _tool("decide_score", "Rate on an ordered scale (2-10 levels). Returns the weighted score, level, "
              "confidence, and distribution.",
              {"state": STATE, "question": {"type": "string"}, "levels": LEVELS},
              ["state", "question", "levels"], _score),
        _tool("decide_gate", "Risk-screen an action before running it: risk score, sensitivity check, and "
              "a recommendation (allow / confirm / block). Advisory only.",
              {"state": {"type": "string", "description": "The action, command, or plan to screen"}},
              ["state"], _gate),
        _tool("decide_decide", "Several typed questions on one context. questions = {id: {type: noul | "
              "choice | score, instructions, criteria}}.",
              {"state": STATE, "questions": {"type": "object", "minProperties": 1, "additionalProperties": {
                  "type": "object", "properties": {"type": {"enum": ["noul", "choice", "score"]},
                                                   "instructions": {"type": "string"}, "criteria": {}},
                  "required": ["type", "instructions"]}}},
              ["state", "questions"], _decide),
        _tool("decide_rank", "Score and rank 2-20 items on one scale. Use it to order findings, attack "
              "angles, or next steps.",
              {"items": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 20},
               "question": {"type": "string"}, "levels": LEVELS, "top_k": {"type": "integer", "minimum": 1},
               "batch_size": {"type": "integer", "minimum": 1, "maximum": 20}},
              ["items", "question", "levels"], _rank),
    ]
