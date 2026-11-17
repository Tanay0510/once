"""Rules as a regression suite: rerun each rule's replay and sort it into one of four states.

  needed    fails without the rule, passes with it      keep
  obsolete  passes without the rule                     retire after two runs in a row
  broken    fails with and without                      rework or retire
  harmful   passes without, fails with                  deprecate now
"""
import math
import time
from pathlib import Path

from . import replay, rules


def classify(without_failed: int, with_failed: int, runs: int) -> str:
    need = max(1, math.ceil(runs * 2 / 3))
    wo, wi = without_failed >= need, with_failed >= need
    if wo and not wi:
        return "needed"
    if not wo and not wi:
        return "obsolete"
    if wo and wi:
        return "broken"
    return "harmful"


def testable(slug: str) -> list[dict]:
    return [r for r in rules.active(slug) if ((r.get("evidence") or {}).get("replay") or {}).get("prompt")]


def run(slug: str, root: Path, agent: str, agent_cmd: str, model: str | None, budget: float, runs: int = 3,
        only: str | None = None, apply: bool = False, progress=print) -> list[dict]:
    out = []
    for rule in testable(slug):
        if only and not rule["id"].startswith(only):
            continue
        ev = rule["evidence"]["replay"]
        progress(f"\n{rule['id']}")
        res = replay.replay(slug, root, rule, ev["prompt"], ev.get("commit") or "HEAD", runs, ev.get("check"),
                            ev.get("signature"), agent_cmd, budget, model=model, progress=progress, save=False)
        if res["verdict"] == "error":
            out.append({"id": rule["id"], "state": "error", "error": res["error"]})
            progress(f"  could not run: {res['error']}")
            break
        e2 = res["evidence"]
        state = classify(e2["without_rule"]["failed"], e2["with_rule"]["failed"], runs)
        fresh = rules.find(slug, rule["id"])
        hist = fresh.setdefault("evidence", {}).setdefault("regress", [])
        hist.append({"date": time.strftime("%Y-%m-%d"), "agent": agent, "model": model, "state": state,
                     "without_failed": e2["without_rule"]["failed"], "with_failed": e2["with_rule"]["failed"],
                     "runs": runs, "cost_usd": e2.get("cost_usd")})
        same_agent = [h["state"] for h in hist if h["agent"] == agent]
        action = "keep"
        if state == "harmful":
            action = "deprecate"
        elif state == "obsolete" and same_agent[-2:] == ["obsolete", "obsolete"]:
            action = "retire"
        elif state in ("obsolete", "broken"):
            action = "watch" if state == "obsolete" else "rework"
        if apply and action in ("deprecate", "retire"):
            fresh["status"] = "deprecated"
            fresh.setdefault("provenance", {})["retired"] = {"by": "regress", "why": state, "date": time.strftime("%Y-%m-%d")}
        rules.save(slug, fresh)
        out.append({"id": rule["id"], "state": state, "action": action, "applied": apply and action in ("deprecate", "retire"),
                    "without_failed": e2["without_rule"]["failed"], "with_failed": e2["with_rule"]["failed"]})
        progress(f"  {state}: failed {e2['without_rule']['failed']}/{runs} without, {e2['with_rule']['failed']}/{runs} with -> {action}")
    if out:
        rules.commit(f"regress ({agent})")
    return out
