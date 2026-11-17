"""The learning pipeline: slice episodes out of sessions, draft candidate rules, lint, and queue for review.

Nothing here makes a rule active. A person does that in `once review`.
Every candidate and every decision is logged, because that record is what the pipeline later learns from.
"""
import difflib
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from pathlib import Path

from . import SCHEMA, detectors, paths, pipeline, rules, store, tasks

PIPELINE_VERSION = "p0"  # the starting pipeline; the active one is pipeline.version()
DEFAULT_DRAFTER = "claude -p {prompt} --output-format json --max-budget-usd {budget}"
PRIORITY = {"human_tag": 0, "missed_file": 1, "pr_review": 2, "retry_cluster": 3, "bootstrap": 4}

DRAFT_PROMPT = """You turn one observed mistake by a coding agent into at most one reusable rule for that repository.

Reply with a single JSON object and nothing else. Either
{{"skip": "<why no general rule follows>"}}
or
{{"directive": "<one imperative sentence, under 300 characters, specific to this repository>",
  "rationale": "<one short sentence: what went wrong without it>",
  "kind": "invariant" | "negative_constraint" | "command" | "pointer",
  "path_globs": ["<glob>", ...]}}

Rules for a good rule:
- It states something true of this repository that the agent could not have guessed.
- It would have prevented the mistake below. If the mistake was a typo, bad luck or a network error, skip.
- It contains no URLs, no shell pipelines, nothing about permissions, secrets or the agent's own behaviour.
- path_globs is empty for a repo-wide rule; otherwise the narrowest glob that covers the files involved.
{extra}
{examples}Observed episode ({source}):
{episode}
"""


def candidates_log() -> Path:
    return paths.local() / "candidates.jsonl"


def log(record: dict):
    f = candidates_log()
    f.parent.mkdir(parents=True, exist_ok=True)
    with open(f, "a") as fh:
        fh.write(json.dumps({"ts": time.time(), "pipeline_version": pipeline.version(), **record}) + "\n")


def history() -> list[dict]:
    f = candidates_log()
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()] if f.exists() else []


def _state() -> dict:
    try:
        return json.loads((paths.local() / "learn_state.json").read_text())
    except Exception:
        return {"seen": []}


def _key(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def _steps_text(evs: list[dict], lo: int, hi: int) -> str:
    out = []
    for s in detectors.steps(evs):
        if lo - 2 <= s["step"] <= hi + 1:
            if s["event"] == "user_prompt":
                out.append(f"{s['step']}. prompt: {detectors._short(s.get('prompt'), 200)}")
            else:
                res = "ok" if s.get("ok") is not False else f"FAILED {s.get('error_head', '')}"
                out.append(f"{s['step']}. {s.get('tool')}: {detectors._short(s.get('target'), 120)} -> {res}")
    return "\n".join(out)


def episodes(slug: str, con=None) -> list[dict]:
    """Slice: human tags, files the agent keeps missing, maintainers' review comments, retry clusters."""
    own = con is None
    con = con or store.connect()
    store.sync(con)
    seen = set(_state()["seen"])
    out, missed, lab_runs = [], {}, 0
    for row in store.sessions(con, slug, 400):
        evs = store.events(con, row["id"])
        prompt = detectors._short(row["prompt"], 300)
        tagged = set()
        for a in detectors.annotations(evs):
            if a.get("tag") == "good":
                continue
            tagged.update(range(a["step_from"], a["step_to"] + 1))
            out.append({"source": "human_tag", "key": _key("tag", row["id"], a["seq_from"], a.get("note")),
                        "session": row["id"], "note": a.get("note", ""), "tag": a.get("tag"),
                        "paths": sorted({p for s in detectors.steps(evs) if a["step_from"] <= s["step"] <= a["step_to"]
                                         for p in s.get("paths", [])}),
                        "replay": {"prompt": row["prompt"], "commit": row["commit_sha"]},
                        "text": f"Task: {prompt}\nSteps:\n{_steps_text(evs, a['step_from'], a['step_to'])}\n"
                                f"The developer tagged steps {a['step_from']}-{a['step_to']} as {a.get('tag')}"
                                + (f" and said: {a['note']}" if a.get("note") else ".")})
        for f in detectors.detect(evs):
            if f["detector"] != "retry_cluster" or f["step_from"] in tagged:
                continue
            st = [s for s in detectors.steps(evs) if f["step_from"] <= s["step"] <= f["step_to"]]
            err = next((s.get("error", "") for s in st if s.get("ok") is False), "")
            head = next((s.get("error_head") for s in st if s.get("ok") is False and s.get("error_head")), None)
            out.append({"source": "retry_cluster", "key": _key("retry", row["id"], f["seq_from"]), "session": row["id"],
                        "paths": sorted({p for s in st for p in s.get("paths", [])}),
                        "replay": {"prompt": row["prompt"], "commit": row["commit_sha"],
                                   "signature": __import__("re").escape(head) if head else None},
                        "text": f"Task: {prompt}\nSteps:\n{_steps_text(evs, f['step_from'], f['step_to'])}\n"
                                f"The same command failed repeatedly. Error: {detectors._short(err, 300)}"})
        oc = next((e for e in evs if e.get("event") == "outcome"), None)
        if oc and row["steps"] > 1:  # a run where the agent did nothing says nothing about what it would miss
            lab_runs += 1
            for mf in oc.get("missed_files") or []:
                missed.setdefault(mf, set()).add(oc.get("task"))
    for f, ts in missed.items():
        if len(ts) >= 2:
            out.append({"source": "missed_file", "key": _key("missed", f), "file": f, "count": len(ts), "of": lab_runs,
                        "tasks": sorted(ts), "paths": [], "text": ""})
    for t in tasks.load_all(slug):
        if t.get("split") == "train" and t.get("review_notes"):  # never read held-out PRs: that would leak
            out.append({"source": "pr_review", "key": _key("review", t["id"]), "task": t["id"], "paths": [],
                        "text": f"Pull request: {t.get('title')}\nMaintainers' review comments on it:\n- "
                                + "\n- ".join(t["review_notes"])})
    if own:
        con.close()
    # An episode whose draft failed for a technical reason was never really looked at: offer it again.
    hist = history()
    failed = {r["episode"] for r in hist if r.get("outcome") == "skipped" and str(r.get("why", "")).startswith("drafter")}
    failed -= {r["episode"] for r in hist if r.get("outcome") in ("queued", "duplicate", "lint_failed")
               or (r.get("outcome") == "skipped" and not str(r.get("why", "")).startswith("drafter"))}
    seen -= failed
    out = [e for e in out if e["key"] not in seen]
    prio = {**PRIORITY, **pipeline.source_priority()}
    out.sort(key=lambda e: prio.get(e["source"], 9))
    return out


def _examples(cfg: dict | None = None, before: float | None = None) -> str:
    """The drafter sees recent decisions: what was accepted as written, and what was rejected and why."""
    n = ((cfg or pipeline.active()).get("drafter") or {}).get("examples") or {}
    h = [r for r in history() if r.get("decision") and (before is None or r.get("ts", 0) < before)]
    good = [r for r in h if r["decision"].startswith("accepted")][-int(n.get("accepted", 4)):] if n.get("accepted", 4) else []
    bad = [r for r in h if r["decision"] == "rejected"][-int(n.get("rejected", 4)):] if n.get("rejected", 4) else []
    if not good and not bad:
        return ""
    lines = ["Earlier decisions by this developer, to calibrate:"]
    lines += [f"- ACCEPTED: {r.get('final_text') or r.get('draft_text')}" for r in good]
    lines += [f"- REJECTED ({r.get('reject_reason') or 'no reason given'}): {r.get('draft_text')}" for r in bad]
    return "\n".join(lines) + "\n\n"


def _parse(out: str) -> dict | None:
    text = out
    try:
        j = json.loads(out)
        if isinstance(j, dict) and "result" in j:
            text = str(j["result"])
        elif isinstance(j, dict) and ("directive" in j or "skip" in j):
            return j
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _extra(cfg: dict) -> str:
    d = cfg.get("drafter") or {}
    lines = [f"- {x}" for x in (d.get("extra") or []) if isinstance(x, str)]
    lines += [f"- {x}" for x in (d.get("prefer") or []) if isinstance(x, str)]
    return "\n".join(lines)


def prompt_for(ep: dict, cfg: dict | None = None, before: float | None = None) -> str:
    cfg = cfg or pipeline.active()
    return DRAFT_PROMPT.format(examples=_examples(cfg, before), extra=_extra(cfg),
                               source=ep["source"].replace("_", " "), episode=ep["text"][:5000])


def draft(ep: dict, drafter_cmd: str | None = None, budget: float = 0.25, cfg: dict | None = None,
          before: float | None = None) -> tuple[dict | None, str, float]:
    """Returns (fields, skip_reason, cost). `cfg` is a pipeline version; `before` hides later decisions (benchmarks)."""
    if ep["source"] == "missed_file":  # deterministic: no model, no untrusted text
        return ({"directive": f"When a change alters behaviour, also update {ep['file']} as the project's own pull requests do.",
                 "rationale": f"In {ep['count']} mined pull requests the real fix changed {ep['file']} and the agent did not.",
                 "kind": "invariant", "path_globs": []}, "", 0.0)
    cmd = drafter_cmd or os.environ.get("ONCE_DRAFTER") or DEFAULT_DRAFTER
    prompt = prompt_for(ep, cfg, before)
    argv = [a.format(prompt=prompt, budget=budget) for a in shlex.split(cmd)]
    with tempfile.TemporaryDirectory(prefix="once-draft-") as d:  # an empty folder: no repo context, no hooks
        try:
            r = subprocess.run(argv, cwd=d, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
                               env={**os.environ, "ONCE_DISABLE": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"})
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            return None, f"drafter did not run: {e}", 0.0
    cost = 0.0
    try:
        cost = float(json.loads(r.stdout).get("total_cost_usd") or 0)
    except Exception:
        pass
    try:
        top = json.loads(r.stdout)
        if isinstance(top, dict) and top.get("is_error"):
            return None, f"drafter failed: {str(top.get('result') or 'the model call reported an error')[:160]}", cost
    except Exception:
        pass
    if r.returncode != 0 and not r.stdout.strip():
        tail = (r.stderr or "").strip().splitlines()
        return None, f"drafter failed: {(tail[-1] if tail else 'exit code ' + str(r.returncode))[:160]}", cost
    j = _parse(r.stdout)
    if not j:
        return None, "drafter reply was not usable: " + " ".join(r.stdout.split())[:120], cost
    if j.get("skip"):
        return None, f"no general rule: {str(j['skip'])[:120]}", cost
    return j, "", cost


def _similar(a: str, b: str) -> float:
    wa, wb = set(re.findall(r"[a-z0-9_]+", a.lower())), set(re.findall(r"[a-z0-9_]+", b.lower()))
    return len(wa & wb) / max(1, len(wa | wb))


def duplicate_of(slug: str, directive: str) -> str | None:
    """An existing rule that says the same, or the repo's own instruction files (the do-not-relearn list)."""
    try:
        known = json.loads((paths.repo_memory(slug) / "known.json").read_text())
    except Exception:
        known = []
    for line in known:
        if _similar(directive, line) >= 0.6:
            return "the repo's instruction files"
    for r in rules.load_all(slug):
        if r.get("status") in ("active", "candidate") and _similar(directive, (r.get("rule") or {}).get("directive", "")) >= 0.6:
            return r["id"]
    return None


def learn(slug: str, root: Path | None, max_candidates: int = 5, drafter_cmd: str | None = None,
          budget: float = 0.25, dry_run: bool = False, progress=print, version: str | None = None,
          eps: list[dict] | None = None, mark_seen: bool = True) -> dict:
    cfg = pipeline.load(version)
    eps = episodes(slug) if eps is None else eps
    stats = {"episodes": len(eps), "drafted": 0, "queued": 0, "skipped": 0, "duplicates": 0, "lint_failed": 0, "cost": 0.0}
    if dry_run:
        return {**stats, "episodes_list": eps}
    state = _state()
    for ep in eps:
        if stats["queued"] >= max_candidates:
            break
        fields, why, cost = draft(ep, drafter_cmd, budget, cfg)
        stats["cost"] += cost
        if why.startswith("drafter"):
            stats["failed"] = stats.get("failed", 0) + 1
            log({"episode": ep["key"], "source_kind": ep["source"], "repo": slug, "outcome": "skipped", "why": why,
                 "pipeline_version": cfg["version"]})
            progress(f"  could not draft ({ep['source'].replace('_', ' ')}): {why}")
            if stats["failed"] >= 2 and not stats["drafted"]:
                progress("  Stopping: the drafting model is not responding. Nothing was lost; run this again later.")
                break
            continue
        if mark_seen:
            state["seen"].append(ep["key"])
        res = queue(slug, root, ep, fields, why, cost, cfg, progress)
        stats[{"skipped": "skipped", "duplicate": "duplicates", "lint_failed": "lint_failed", "queued": "queued"}[res["outcome"]]] += 1
        if res["outcome"] != "skipped":
            stats["drafted"] += 1
    (paths.local() / "learn_state.json").write_text(json.dumps(state))
    if stats["queued"]:
        rules.commit(f"learn: {stats['queued']} candidates")
    return stats


def queue(slug: str, root: Path | None, ep: dict, fields: dict | None, why: str, cost: float, cfg: dict,
          progress=print, check_duplicates: bool = True) -> dict:
    """One drafted episode: skip, duplicate, lint failure, or a candidate in the review queue. Always logged."""
    base = {"episode": ep["key"], "source_kind": ep["source"], "repo": slug, "session": ep.get("session"),
            "task": ep.get("task"), "draft_cost": cost, "pipeline_version": cfg["version"],
            "episode_text": (ep.get("text") or "")[:5000]}
    if not fields:
        log({**base, "outcome": "skipped", "why": why})
        progress(f"  skipped   {ep['source'].replace('_', ' ')}: {why}")
        return {"outcome": "skipped"}
    directive = " ".join(str(fields.get("directive", "")).split())
    dup = duplicate_of(slug, directive) if check_duplicates else None
    if dup:
        log({**base, "outcome": "duplicate", "of": dup, "draft_text": directive})
        progress(f"  duplicate of {dup}")
        return {"outcome": "duplicate", "of": dup}
    gl = [g for g in (fields.get("path_globs") or []) if isinstance(g, str)][:4]
    rule = {"schema": SCHEMA, "id": rules.unique_id(slug, rules.slugify(directive)), "status": "candidate",
            "kind": fields.get("kind") if fields.get("kind") in rules.KINDS else "invariant",
            "scope": {"path_globs": gl, "symbol_triggers": [], "agents": ["*"]},
            "rule": {"directive": directive, "rationale": " ".join(str(fields.get("rationale", "")).split())[:300]},
            "provenance": {"source_kind": ep["source"], "source_sessions": [ep["session"]] if ep.get("session") else [],
                           "source_tasks": ep.get("tasks") or ([ep["task"]] if ep.get("task") else []),
                           "drafted_by": cfg["version"], "created": time.strftime("%Y-%m-%d"), "episode": ep["key"]},
            "enforcement": {"level": "instruct"}}
    if ep.get("replay", {}).get("prompt"):
        rule["provenance"]["replay_hint"] = {"prompt": ep["replay"]["prompt"][:2000], "commit": ep["replay"].get("commit"),
                                             "signature": ep["replay"].get("signature")}
    errs = rules.lint(rule, root)
    if errs:
        log({**base, "outcome": "lint_failed", "errors": errs, "draft_text": directive})
        progress(f"  rejected by lint: {errs[0]}")
        return {"outcome": "lint_failed"}
    rules.save(slug, rule)
    also = [o for o, _ in rules.similar_elsewhere(slug, directive)]
    log({**base, "candidate_id": rule["id"], "outcome": "queued", "draft_text": directive, "lint": "pass",
         "features": rule_features(rule), "also_in": also, "warnings": lint_warnings(rule, cfg)})
    progress(f"  queued    {rule['id']}  ({ep['source'].replace('_', ' ')})")
    if also:
        progress(f"            the same rule is already active in {', '.join(also[:3])}: once accepted, consider"
                 f"  once rules lift {rule['id']} stack")
    return {"outcome": "queued", "id": rule["id"], "directive": directive}


def rule_features(rule: dict) -> dict:
    d = (rule.get("rule") or {}).get("directive", "")
    gl = (rule.get("scope") or {}).get("path_globs") or []
    return {"kind": rule.get("kind"), "directive_chars": len(d), "scoped": bool(gl),
            "scope_depth": max([g.strip('/').count('/') for g in gl], default=0),
            "has_rationale": bool((rule.get("rule") or {}).get("rationale")),
            "has_example": bool(re.search(r"`[^`]+`|\be\.g\.|\bnot\b|\bnever\b|\binstead\b", d, re.I)),
            "source": (rule.get("provenance") or {}).get("source_kind")}


def lint_warnings(rule: dict, cfg: dict | None = None) -> list[str]:
    """Preferences learned from which rules survived (the rule-form priors). They warn; they never block."""
    pri = ((cfg or pipeline.active()).get("drafter") or {}).get("priors") or []
    f = rule_features(rule)
    out = []
    for p in pri:
        feat, val, msg = p.get("feature"), p.get("value"), p.get("message")
        if feat and msg and str(_bucket(feat, f.get(feat))) == str(val) and p.get("direction") == "worse":
            out.append(msg)
    return out


def _bucket(feat, v):
    if feat == "directive_chars":
        return "short" if (v or 0) < 120 else ("medium" if v < 250 else "long")
    return v


def decide(slug: str, rule: dict, decision: str, who: str, new_text: str | None = None, reason: str | None = None,
           review_secs: float | None = None) -> dict:
    """Accept, accept with an edit, or reject a candidate. The decision is logged for the pipeline to learn from."""
    draft_text = (rule.get("rule") or {}).get("directive", "")
    rule = {k: v for k, v in rule.items() if not k.startswith("_")}
    rec = {"candidate_id": rule["id"], "repo": slug, "source_kind": (rule.get("provenance") or {}).get("source_kind"),
           "draft_text": draft_text, "replay": ((rule.get("evidence") or {}).get("replay") or {}).get("verdict"),
           "drafted_by": (rule.get("provenance") or {}).get("drafted_by"), "episode": (rule.get("provenance") or {}).get("episode")}
    if review_secs is not None:
        rec["review_secs"] = round(review_secs, 1)
    if decision == "reject":
        rule["status"] = "deprecated"
        rule.setdefault("provenance", {})["rejected"] = {"by": who, "reason": reason or "", "date": time.strftime("%Y-%m-%d")}
        rec.update({"decision": "rejected", "reject_reason": reason or ""})
    else:
        if new_text and new_text.strip() != draft_text:
            rule["rule"]["directive"] = new_text.strip()
            ratio = 1 - difflib.SequenceMatcher(None, draft_text, new_text.strip()).ratio()
            rec.update({"decision": "accepted_with_edit", "edit_ratio": round(ratio, 3), "final_text": new_text.strip()})
        else:
            rec.update({"decision": "accepted", "edit_ratio": 0.0, "final_text": draft_text})
        rule["status"] = "active"
        rule.setdefault("provenance", {}).update({"approved_by": who, "approved": time.strftime("%Y-%m-%d")})
    rules.save(slug, rule)
    rules.commit(f"review {rec['decision']} {rule['id']}")
    log(rec)
    return rec


def stats() -> dict:
    """How the pipeline is doing, by source. This is the ground the outer loop learns on."""
    by = {}
    for r in history():
        src = r.get("source_kind") or "?"
        b = by.setdefault(src, {"queued": 0, "skipped": 0, "duplicate": 0, "lint_failed": 0,
                                "accepted": 0, "accepted_with_edit": 0, "rejected": 0, "edit": []})
        if r.get("outcome") in b:
            b[r["outcome"]] += 1
        if r.get("decision") in b:
            b[r["decision"]] += 1
            if r.get("edit_ratio") is not None:
                b["edit"].append(r["edit_ratio"])
    for b in by.values():
        decided = b["accepted"] + b["accepted_with_edit"] + b["rejected"]
        b["acceptance"] = (b["accepted"] + b["accepted_with_edit"]) / decided if decided else None
        b["as_written"] = b["accepted"] / decided if decided else None
        b["mean_edit"] = sum(b["edit"]) / len(b["edit"]) if b["edit"] else None
        del b["edit"]
    return by
