"""Precompiled, read-only view of active rules for the hook hot path. JSON + re only."""
import json
import re

from . import paths

BUDGET_RULES = 8
BUDGET_CHARS = 2400  # about 600 tokens
SESSION_RULES = 20


def load(slug: str) -> dict:
    try:
        return json.loads(paths.index_file(slug).read_text())
    except Exception:
        return {"rules": []}


INHERITED_SLOTS = 2  # rules from the user, agent, stack or owner level: at most this many per injection


def for_agent(idx: dict, agent: str | None) -> dict:
    """Only the rules meant for this agent: `agents: ["*"]` is everyone."""
    if not agent:
        return idx
    keep = [r for r in idx.get("rules", []) if "*" in (r.get("agents") or ["*"]) or agent in (r.get("agents") or [])]
    return {**idx, "rules": keep}


def _context(r: dict) -> bool:
    """Instruct-level rules are given as context. Rules promoted to remind, check or block are not: they cost
    tokens only when an edit goes against them."""
    return r.get("level", "instruct") == "instruct"


def repo_wide(idx: dict) -> list[dict]:
    return [r for r in idx.get("rules", []) if r.get("repo_wide") and _context(r)]


def rank(rules: list[dict], explore: float = 0.0, rng=None) -> tuple[list[dict], str | None]:
    """Order by the ranking policy's score when more rules compete than fit. Now and then a lower-ranked rule
    takes the last slot, or the policy would never learn that it was good. Returns (ordered, explored id)."""
    if len(rules) <= BUDGET_RULES or not any("score" in r for r in rules):
        return rules, None
    ordered = sorted(rules, key=lambda r: -float(r.get("score", 0)))
    if explore > 0 and rng is not None and rng.random() < explore:
        j = rng.randrange(BUDGET_RULES, len(ordered))
        ordered[BUDGET_RULES - 1], ordered[j] = ordered[j], ordered[BUDGET_RULES - 1]
        return ordered, ordered[BUDGET_RULES - 1]["id"]
    return ordered, None


def matching(idx: dict, relpath: str) -> list[dict]:
    out = []
    for r in idx.get("rules", []):
        if r.get("repo_wide") or not _context(r):
            continue
        if any(re.match(rx, relpath) for rx in r.get("regex", [])):
            out.append(r)
    out.sort(key=lambda r: -r.get("depth", 0))
    return out


def within_budget(rules: list[dict], already: int = 0) -> tuple[list[dict], list[dict]]:
    """Returns (chosen, held_back)."""
    chosen, used, inherited = [], 0, 0
    for r in rules:
        size = len(r.get("text", "")) + len(r.get("why", ""))
        if len(chosen) >= BUDGET_RULES or used + size > BUDGET_CHARS or already + len(chosen) >= SESSION_RULES:
            continue
        up = r.get("scope_level", "repo") != "repo"
        if up and inherited >= INHERITED_SLOTS:
            continue
        chosen.append(r)
        used += size
        inherited += up
    held = [r for r in rules if r not in chosen]
    return chosen, held


def render(rules: list[dict]) -> str:
    """Fixed wrapper. Model-drafted text only ever fills the directive and rationale fields."""
    if not rules:
        return ""
    lines = ['<repo-conventions source="once">',
             "Reviewed conventions for this repository. Follow them unless the user says otherwise.", ""]
    for r in rules:
        scope = "whole repo" if r.get("repo_wide") else ", ".join(r.get("globs", []))
        lvl = r.get("scope_level", "repo")
        if lvl != "repo":
            scope += {"user": " (your own preference)", "agent": " (for this agent)", "stack": " (for this toolchain)",
                      "owner": " (shared across this organization's repos)"}.get(lvl, "")
        lines.append(f"[{r['id']}] applies to {scope}")
        lines.append(r.get("text", ""))
        if r.get("why"):
            lines.append(f"Why: {r['why']}")
        lines.append("")
    lines.append("</repo-conventions>")
    return "\n".join(lines)
