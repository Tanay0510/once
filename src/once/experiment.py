"""Lab runs: one task, a sweep over the train split, or an experiment over arms.

Arms:
  bare       the repo's own instruction files only
  rules      bare plus your active rules
  generated  bare plus an instruction file the agent wrote for itself at the training cutoff
  native     bare plus the agent's built-in memory, built on the train tasks and then frozen (experimental)
  unreviewed bare plus your active rules and every suggestion still waiting for review: is review worth it?
  rules+native  rules and the frozen native memory together: do they add up, or overlap?
"""
import hashlib
import json
import math
import os
import random
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import SCHEMA, __version__, paths, replay, rules, tasks

ARMS = ("bare", "rules", "generated", "native", "unreviewed", "rules+native")
GEN_PROMPT = ("Study this repository and write a CLAUDE.md at the repository root for future coding sessions here. "
              "Cover how to run the tests, the layout, and the conventions a contributor must follow. Be concise.")


def exp_dir(exp_id: str) -> Path:
    return paths.local() / "experiments" / exp_id


def baseline_dir(slug: str) -> Path:
    return paths.local() / "baselines" / slug.replace("/", "__")


def agent_version(agent: str = "claude-code") -> str:
    from . import adapters
    try:
        r = subprocess.run([adapters.KNOWN.get(agent, "claude"), "--version"], capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        return (r.stdout or "").strip().splitlines()[0] if r.stdout.strip() else "unknown"
    except Exception:
        return "unknown"


def fingerprint(slug: str, task_ids: list[str], model: str | None, agent_cmd: str, agent: str = "claude-code") -> dict:
    """What must stay fixed for runs to be comparable."""
    head = subprocess.run(["git", "-C", str(paths.memory()), "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    return {"agent": f"{agent} {agent_version(agent)}", "model": model or "agent default", "meta_agent": __version__,
            "rules_commit": head, "tasks": hashlib.sha1(",".join(sorted(task_ids)).encode()).hexdigest()[:10],
            "agent_cmd": hashlib.sha1(agent_cmd.encode()).hexdigest()[:10], "pipeline": _pipeline_version()}


def _pipeline_version() -> str:
    """Rules drafted by different pipeline versions must not be mixed in one experiment without saying so."""
    try:
        from . import pipeline
        return pipeline.version()
    except Exception:
        return "p0"


def arm_setup(slug: str, arm: str) -> dict:
    """What an arm adds to a run. Raises ValueError when its baseline has not been prepared."""
    if arm == "bare":
        return {"entries": None}
    if arm == "rules":
        return {"entries": [replay.index_entry(r) for r in rules.active(slug)] or None}
    if arm == "generated":
        f = baseline_dir(slug) / "generated.md"
        if not f.exists():
            raise ValueError("the generated baseline is not prepared; run: once lab exp baseline generated")
        return {"entries": None, "extra_files": {"CLAUDE.md": f.read_text()}}
    if arm == "unreviewed":
        pool = [r for r in rules.load_all(slug) if r.get("status") in ("active", "candidate")]
        return {"entries": [replay.index_entry(r) for r in pool] or None}
    if arm == "rules+native":
        nat = arm_setup(slug, "native")
        return {**nat, "entries": arm_setup(slug, "rules")["entries"]}
    if arm == "native":
        d = baseline_dir(slug) / "native-frozen"
        if not d.is_dir():
            raise ValueError("the native baseline is not prepared; run: once lab exp baseline native")
        copy = Path(tempfile.mkdtemp(prefix="once-native-"))
        shutil.copytree(d, copy / "memory")
        return {"entries": None, "auto_memory": True, "_tmp": str(copy),
                "extra_argv": ["--settings", json.dumps({"autoMemoryEnabled": True, "autoMemoryDirectory": str(copy / "memory")})]}
    raise ValueError(f"unknown arm {arm!r}; choose from {', '.join(ARMS)}")


def run_task(slug: str, root: Path, task: dict, arm: str = "bare", model: str | None = None, budget: float = 1.0,
             timeout: int = 900, agent_cmd: str | None = None, keep: bool = False, run_id: str | None = None,
             setup_override: dict | None = None, agent: str = "claude-code") -> dict:
    """Run the agent on one mined task and record the outcome on the captured session."""
    from . import hook
    from . import adapters
    agent_cmd = agent_cmd or os.environ.get("ONCE_HEADLESS") or adapters.get(agent).HEADLESS
    cfg = setup_override or arm_setup(slug, arm)
    _, setup = tasks.effective_check(slug, root)
    run_id = run_id or time.strftime("%Y%m%d-%H%M%S") + f"-{random.randrange(1000):03d}"
    base = Path(tempfile.mkdtemp(prefix="once-run-"))
    prompt = ("Resolve this issue in this repository. Make the code change itself; do not only describe it.\n\n"
              + task["prompt"])
    label = {"task": task["id"], "arm": arm, "run": run_id}
    try:
        res = replay.one_run(root, task["base_commit"], cfg.get("entries"), prompt, task.get("check"), None, slug,
                             agent_cmd, budget, timeout, base / "work", model=model,
                             before_check=lambda w: tasks.apply_tests(root, task, w), label=label, setup=setup,
                             extra_files=cfg.get("extra_files"), extra_argv=cfg.get("extra_argv"),
                             auto_memory=cfg.get("auto_memory", False))
    finally:
        if not keep:
            shutil.rmtree(base, ignore_errors=True)
        if cfg.get("_tmp"):
            shutil.rmtree(cfg["_tmp"], ignore_errors=True)
    if "error" in res:
        return res
    if res["steps"] == 0 and not res["changed"]:
        # The agent never did anything. Recording that as a failed task would poison every later number.
        why = res.get("agent_error") or "it made no tool calls and changed no files"
        return {"error": f"the agent did no work on this task: {why}", "task": task["id"], "arm": arm}
    injected = set((cfg.get("extra_files") or {}).keys())
    res["changed"] = [f for f in res["changed"] if f not in injected]
    res["files_changed"] = len(res["changed"])
    res.update(tasks.missed(root, task, res["changed"]))
    res.update({"task": task["id"], "pr": task["pr"], "arm": arm, "run": run_id, "model": model, "agent": agent})
    if res.get("sessions"):  # the outcome becomes one more event on the session, for the slicer and the viewer
        hook._append(slug, res["sessions"][-1], {
            "schema": SCHEMA, "session_id": res["sessions"][-1], "agent": agent, "repo": slug, "ts": time.time(),
            "event": "outcome", "verified": res["verified"], "cost_usd": res["cost_usd"], "seconds": res["seconds"],
            "changed": res["changed"], "missed_files": res["missed_files"], "wrote_tests": res["wrote_tests"],
            "task": task["id"], "arm": arm})
    return res


# ---------------------------------------------------------------- experiments

def create(slug: str, task_ids: list[str], arms: list[str], repeats: int, model: str | None, budget: float,
           agent_cmd: str, name: str = "exp", agent: str = "claude-code") -> dict:
    exp_id = f"{name}-{time.strftime('%Y%m%d-%H%M%S')}"
    m = {"id": exp_id, "slug": slug, "tasks": task_ids, "arms": arms, "repeats": repeats, "model": model,
         "budget": budget, "created": time.time(), "agent": agent,
         "fingerprint": fingerprint(slug, task_ids, model, agent_cmd, agent)}
    d = exp_dir(exp_id)
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps(m, indent=2))
    return m


def load(exp_id: str | None) -> tuple[dict, list[dict]] | None:
    base = paths.local() / "experiments"
    if not base.is_dir():
        return None
    dirs = sorted([d for d in base.iterdir() if (d / "manifest.json").exists()], key=lambda d: d.stat().st_mtime)
    d = next((x for x in reversed(dirs) if exp_id and x.name.startswith(exp_id)), dirs[-1] if dirs and not exp_id else None)
    if d is None:
        return None
    m = json.loads((d / "manifest.json").read_text())
    f = d / "results.jsonl"
    rows = [json.loads(l) for l in f.read_text().splitlines() if l.strip()] if f.exists() else []
    return m, rows


def plan(m: dict, rows: list[dict]) -> list[tuple[str, str, int]]:
    """Remaining runs. Arms are shuffled inside each task and repeat, so drift hits every arm alike."""
    done = {(r["task"], r["arm"], r["repeat"]) for r in rows if not did_no_work(r)}
    rng = random.Random(m["id"])
    out = []
    for rep in range(1, m["repeats"] + 1):
        ts = list(m["tasks"])
        rng.shuffle(ts)
        for t in ts:
            arms = list(m["arms"])
            rng.shuffle(arms)
            out += [(t, a, rep) for a in arms if (t, a, rep) not in done]
    return out


def run(m: dict, root: Path, max_cost: float | None, agent_cmd: str, progress=print, force_mixed: bool = False) -> dict:
    _, rows = load(m["id"])
    now = fingerprint(m["slug"], m["tasks"], m["model"], agent_cmd, m.get("agent", "claude-code"))
    old = m["fingerprint"]
    diff = [k for k in now if k in old and now[k] != old[k]]  # a key an older manifest lacks is not a change
    if rows and diff and not force_mixed:
        return {"stopped": f"the environment changed since this experiment started ({', '.join(diff)}). "
                           "Results would not be comparable. Start a new experiment, or pass --force-mixed."}
    todo = plan(m, rows)
    spent = sum(r.get("cost_usd") or 0 for r in rows)
    by_id = {t["id"]: t for t in tasks.load_all(m["slug"])}
    out = exp_dir(m["id"]) / "results.jsonl"
    for i, (tid, arm, rep) in enumerate(todo, 1):
        if max_cost is not None and spent >= max_cost:
            return {"stopped": f"reached the spend cap of ${max_cost:.2f}", "remaining": len(todo) - i + 1, "spent": spent}
        res = run_task(m["slug"], root, by_id[tid], arm, m["model"], m["budget"], agent_cmd=agent_cmd,
                       agent=m.get("agent", "claude-code"))
        res.update({"task": tid, "arm": arm, "repeat": rep})
        slim = {k: v for k, v in res.items() if k not in ("check_tail", "sessions")}
        with open(out, "a") as f:
            f.write(json.dumps(slim) + "\n")
        spent += res.get("cost_usd") or 0
        if "error" in res:
            return {"stopped": res["error"]}
        mark = {True: "pass", False: "FAIL", None: "?"}[res["verified"]]
        progress(f"  [{i}/{len(todo)}] #{by_id[tid]['pr']:<6} {arm:<9} {mark}  {res['steps']} steps, "
                 f"{res['failed_steps']} failed, {len(res['missed_files'])} missed"
                 + (f", ${res['cost_usd']:.2f}" if res.get("cost_usd") else ""))
    return {"done": True, "spent": spent}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def sign_test(wins: int, losses: int) -> float:
    """Two-sided exact sign test on tasks where the two arms differ."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def did_no_work(r: dict) -> bool:
    return "error" in r or (r.get("steps") == 0 and not r.get("files_changed"))


def summarize(m: dict, rows: list[dict]) -> dict:
    excluded = sum(1 for r in rows if did_no_work(r))
    rows = [r for r in rows if not did_no_work(r)]
    arms = {}
    for a in m["arms"]:
        rs = [r for r in rows if r["arm"] == a]
        if not rs:
            continue
        k = sum(1 for r in rs if r.get("verified"))
        cost = sum(r.get("cost_usd") or 0 for r in rs)
        lo, hi = wilson(k, len(rs))
        arms[a] = {"runs": len(rs), "verified": k, "rate": k / len(rs), "ci": [lo, hi],
                   "steps": sum(r["steps"] for r in rs) / len(rs),
                   "failed_steps": sum(r["failed_steps"] for r in rs) / len(rs),
                   "missed_rate": sum(1 for r in rs if r.get("missed_files")) / len(rs),
                   "cost": cost, "cost_per_verified": (cost / k) if k and cost else None}
    pairs = []
    base = m["arms"][0]
    for a in m["arms"][1:]:
        wins = losses = ties = 0
        dsteps = []
        for t in m["tasks"]:
            x = [r for r in rows if r["task"] == t and r["arm"] == base]
            y = [r for r in rows if r["task"] == t and r["arm"] == a]
            if not x or not y:
                continue
            fx = sum(bool(r.get("verified")) for r in x) / len(x)
            fy = sum(bool(r.get("verified")) for r in y) / len(y)
            wins, losses, ties = wins + (fy > fx), losses + (fy < fx), ties + (fy == fx)
            dsteps.append(sum(r["steps"] for r in y) / len(y) - sum(r["steps"] for r in x) / len(x))
        if wins + losses + ties:
            pairs.append({"arm": a, "against": base, "better": wins, "worse": losses, "same": ties,
                          "p": sign_test(wins, losses), "steps_diff": sum(dsteps) / len(dsteps)})
    return {"arms": arms, "pairs": pairs, "total_runs": len(rows), "excluded": excluded,
            "planned": len(m["tasks"]) * len(m["arms"]) * m["repeats"]}


# ---------------------------------------------------------------- baselines

def prepare_generated(slug: str, root: Path, commit: str, model, budget, agent_cmd, timeout=900) -> Path | None:
    """One-shot summarization baseline: the agent writes its own instruction file at the training cutoff."""
    base = Path(tempfile.mkdtemp(prefix="once-gen-"))
    try:
        res = replay.one_run(root, commit, None, GEN_PROMPT, None, None, f"replay/baseline-{int(time.time())}",
                             agent_cmd, budget, timeout, base / "work", model=model)
        f = base / "work" / "CLAUDE.md"
        if "error" in res or not f.exists():
            return None
        d = baseline_dir(slug)
        d.mkdir(parents=True, exist_ok=True)
        (d / "generated.md").write_text(f.read_text())
        (d / "generated.json").write_text(json.dumps({"commit": commit, "cost_usd": res.get("cost_usd"), "date": time.strftime("%Y-%m-%d")}))
        return d / "generated.md"
    finally:
        shutil.rmtree(base, ignore_errors=True)


def prepare_native(slug: str, root: Path, train: list[dict], model, budget, agent_cmd, progress=print) -> Path:
    """Built-in memory baseline: run the train tasks in merge order with one persistent memory folder, then freeze it."""
    d = baseline_dir(slug)
    live = d / "native-live"
    shutil.rmtree(live, ignore_errors=True)
    live.mkdir(parents=True)
    cfg = {"entries": None, "auto_memory": True,
           "extra_argv": ["--settings", json.dumps({"autoMemoryEnabled": True, "autoMemoryDirectory": str(live)})]}
    for t in train:
        res = run_task(slug, root, t, "native-train", model, budget, agent_cmd=agent_cmd, setup_override=cfg)
        progress(f"  #{t['pr']:<6} {'pass' if res.get('verified') else 'FAIL'}"
                 + (f"  ${res['cost_usd']:.2f}" if res.get("cost_usd") else ""))
    frozen = d / "native-frozen"
    shutil.rmtree(frozen, ignore_errors=True)
    shutil.copytree(live, frozen)
    return frozen
