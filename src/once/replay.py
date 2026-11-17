"""Replay: rerun one task with and without a rule, in throwaway copies, and record what changed.

Not a container. The agent runs on this machine with edit and shell access inside a temporary
copy of the repo that has no remotes and no later history. Use only on code you trust.
"""
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import adapters, globs, paths, rules

DEFAULT_AGENT = ('claude -p {prompt} --permission-mode acceptEdits --allowedTools Bash Read Edit Write Grep Glob '
                 '--output-format json --max-budget-usd {budget}')


def _run(cmd, cwd, **kw):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, **kw)


def export(root: Path, commit: str, dest: Path):
    """A history-free copy at one commit: the agent cannot read the real fix with git log."""
    dest.mkdir(parents=True)
    tar = subprocess.run(["git", "-C", str(root), "archive", commit], capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(dest)], input=tar.stdout, check=True)
    ident = ["-c", "user.name=replay", "-c", "user.email=replay@localhost"]
    _run(["git", "init", "-q"], dest)
    _run(["git", "add", "-A"], dest)
    _run(["git", *ident, "commit", "-qm", "base"], dest)


def index_entry(rule: dict) -> dict:
    gl = (rule.get("scope") or {}).get("path_globs") or []
    wide = globs.is_repo_wide(gl)
    return {"id": rule["id"], "text": (rule.get("rule") or {}).get("directive", ""),
            "why": (rule.get("rule") or {}).get("rationale") or "", "globs": gl,
            "regex": [] if wide else [globs.to_regex(g) for g in gl],
            "depth": 0 if wide else max(globs.depth(g) for g in gl), "repo_wide": wide}


def one_run(root: Path, commit: str, rule_entries: list[dict] | None, prompt: str, check: str | None,
            signature: str | None, run_slug: str, agent_cmd: str, budget: float, timeout: int, work: Path,
            model: str | None = None, before_check=None, label: dict | None = None, setup: list[str] | None = None,
            extra_files: dict | None = None, extra_argv: list[str] | None = None, auto_memory: bool = False) -> dict:
    """One headless agent run in a throwaway copy. rule_entries=None means no rules at all."""
    export(root, commit, work)
    for rel, content in (extra_files or {}).items():  # e.g. a generated instruction file for a baseline arm
        f = work / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text((f.read_text() + "\n\n" if f.exists() else "") + content)
    if extra_files:
        _run(["git", "add", "-A"], work)
        _run(["git", "-c", "user.name=replay", "-c", "user.email=replay@localhost", "commit", "-qm", "baseline files"], work)
    env = {**os.environ, "ONCE_SLUG": run_slug}
    if not auto_memory:
        env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    env.pop("ONCE_DISABLE", None)
    if label:
        env["ONCE_LABEL"] = json.dumps(label)
    if not rule_entries:
        env["ONCE_INDEX"] = "none"
    else:
        idx = work.parent / f"{work.name}.index.json"
        idx.write_text(json.dumps({"rules": rule_entries}))
        env["ONCE_INDEX"] = str(idx)
        scoped = [e for e in rule_entries if not e["repo_wide"]]
        if scoped:
            cc = adapters.get("claude-code")
            cc.emit_native(scoped, work)
            (work / ".git" / "info").mkdir(exist_ok=True)
            with open(work / ".git" / "info" / "exclude", "a") as f:
                f.write(cc.NATIVE_DIR + "/\n")
    for cmd in setup or []:
        subprocess.run(cmd, cwd=work, shell=True, capture_output=True, timeout=timeout)
    argv = [a.format(prompt=prompt, budget=budget) for a in shlex.split(agent_cmd)]
    if model:
        argv += ["--model", model]
    argv += extra_argv or []
    t0 = time.time()
    try:
        r = subprocess.run(argv, cwd=work, env=env, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        rc, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        rc, out, err = -1, "", f"timed out after {timeout}s"
    except FileNotFoundError:
        return {"error": f"agent command not found: {argv[0]}"}
    secs = time.time() - t0
    cost, agent_error, tokens = None, None, None
    try:
        j = json.loads(out)
        cost = j.get("total_cost_usd")
        u = j.get("usage") if isinstance(j.get("usage"), dict) else None
        if u:
            tokens = sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                                       "cache_read_input_tokens"))
        if j.get("is_error"):  # the agent started but reported a failure, e.g. a usage limit or a login problem
            agent_error = str(j.get("result") or j.get("error") or "the agent reported an error")[:400]
    except Exception:
        # Not one JSON document: Codex and others print one JSON event per line.
        from .adapters import codex as _codex
        cost, agent_error = _codex.parse_output(out)
    if agent_error is None and rc != 0:
        tail = (err or out or "").strip().splitlines()
        agent_error = (tail[-1] if tail else f"exit code {rc}")[:400]
    _run(["git", "add", "-A", "-N"], work)
    changed = [l for l in _run(["git", "diff", "--name-only", "HEAD"], work).stdout.splitlines() if l.strip()]
    if before_check:
        before_check(work)
    verified, check_tail = None, ""
    if check:
        try:
            c = subprocess.run(check, cwd=work, shell=True, capture_output=True, text=True, timeout=timeout)
            verified, check_tail = c.returncode == 0, (c.stdout + c.stderr)[-600:]
        except subprocess.TimeoutExpired:
            verified, check_tail = False, "check timed out"
    events, sids = [], []
    d = paths.trajectories(run_slug)
    if d.is_dir():
        for f in sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime):
            evs = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
            if label and not any(e.get("label") == label for e in evs if e.get("event") == "session_start"):
                continue
            events += evs
            sids.append(f.stem)
    failed = [e for e in events if e.get("event") == "tool_result" and e.get("ok") is False]
    if signature:
        rx = re.compile(signature)
        hits = sum(1 for e in failed if rx.search(" ".join(str(e.get(k, "")) for k in ("error", "error_head", "target"))))
        present = hits > 0
    else:
        hits, present = len(failed), (verified is False) if check else len(failed) > 0
    got_rule = any(e.get("event") == "rule_injected" for e in events)
    return {"agent_exit": rc, "agent_error": agent_error, "seconds": round(secs, 1), "cost_usd": cost, "tokens": tokens,
            "verified": verified,
            "failed_steps": len(failed), "signature_hits": hits, "failure_present": present,
            "steps": sum(1 for e in events if e.get("event") == "tool_result"), "rule_delivered": got_rule,
            "captured": bool(events), "sessions": sids, "files_changed": len(changed), "changed": changed,
            "check_tail": check_tail}


def replay(slug: str, root: Path, rule: dict, prompt: str, commit: str = "HEAD", runs: int = 3,
           check: str | None = None, signature: str | None = None, agent_cmd: str | None = None,
           budget: float = 1.0, timeout: int = 900, keep: bool = False, progress=print,
           model: str | None = None, save: bool = True) -> dict:
    agent_cmd = agent_cmd or os.environ.get("ONCE_HEADLESS") or DEFAULT_AGENT
    # Unique per replay: two replays in the same second must not read each other's session files.
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(3).hex()
    base = Path(tempfile.mkdtemp(prefix="once-replay-"))
    results = {"without": [], "with": []}
    try:
        for i in range(runs):
            for arm in ("without", "with"):  # interleaved, so drift hits both arms alike
                progress(f"  run {i + 1}/{runs}  {arm:<7} the rule ...")
                res = one_run(root, commit, [index_entry(rule)] if arm == "with" else None, prompt, check, signature,
                              f"replay/{run_id}/{arm}-{i + 1}", agent_cmd, budget, timeout, base / f"{arm}-{i + 1}",
                              model=model)
                if "error" in res:
                    return {"verdict": "error", "error": res["error"]}
                if res.get("agent_error") and res["steps"] == 0:
                    return {"verdict": "error", "error": f"the agent did no work: {res['agent_error']}"}
                results[arm].append(res)
                state = "failure seen" if res["failure_present"] else "clean"
                progress(f"                       {state}, {res['steps']} steps, {res['seconds']}s"
                         + (f", ${res['cost_usd']:.2f}" if res.get("cost_usd") else ""))
    finally:
        if not keep:
            shutil.rmtree(base, ignore_errors=True)
    wo = sum(r["failure_present"] for r in results["without"])
    wi = sum(r["failure_present"] for r in results["with"])
    need = max(1, math.ceil(runs * 2 / 3))
    if not all(r["captured"] for r in results["without"] + results["with"]):
        verdict = "not_captured"
    elif not all(r["rule_delivered"] for r in results["with"]):
        verdict = "rule_not_delivered"
    elif wo < need:
        verdict = "not_reproduced"
    elif wi == 0:
        verdict = "passes"
    else:
        verdict = "no_help"
    cost = sum(r["cost_usd"] or 0 for arm in results.values() for r in arm)
    evidence = {"date": time.strftime("%Y-%m-%d"), "commit": _run(["git", "rev-parse", "--short", commit], root).stdout.strip(),
                "prompt": prompt, "signature": signature, "check": check, "runs": runs, "model": model,
                "without_rule": {"failed": wo}, "with_rule": {"failed": wi}, "verdict": verdict,
                "steps": {"without": round(sum(r["steps"] for r in results["without"]) / runs, 1),
                          "with": round(sum(r["steps"] for r in results["with"]) / runs, 1)},
                "per_run": {arm: {"cost_usd": _avg([r.get("cost_usd") for r in results[arm]]),
                                  "tokens": _avg([r.get("tokens") for r in results[arm]])} for arm in ("without", "with")},
                "cost_usd": round(cost, 2) if cost else None}
    if save:
        rule = {k: v for k, v in rule.items() if not k.startswith("_")}
        rule.setdefault("evidence", {})["replay"] = evidence
        rules.save(slug, rule)
        rules.commit(f"replay {rule['id']}: {verdict}")
    return {"verdict": verdict, "evidence": evidence, "runs": results, "kept": str(base) if keep else None}


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


VERDICTS = {
    "passes": "Passes. The failure shows without the rule and not with it.",
    "not_reproduced": "Not reproduced. The failure did not show reliably without the rule, so this proves nothing either way.",
    "no_help": "No help. The failure still shows with the rule in place.",
    "rule_not_delivered": "Inconclusive. The rule never reached the agent in the with-rule runs.",
    "not_captured": "Inconclusive. The runs were not captured; check that the hooks are installed.",
}
