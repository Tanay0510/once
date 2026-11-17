"""Rules shared through the repository, by pull request; and a pull request check anyone can read.

`once share` commits this machine's reviewed rules to `.once/rules/` on a new branch, in a separate worktree
so the developer's checkout is never touched, and opens a pull request with `gh`. Teammates get the rules when
the pull request merges: rules.load_all reads `.once/rules/` for everyone who uses it in that repo.

`once pr-check` looks at the files a pull request changes and lists the files that usually change with them,
from the repository's own history, plus the shared rules that apply. It can post that as a comment.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import yaml

from . import BIN, globs, index, rules


def _git(root, *args, check=True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=check, timeout=120, **kw)


def gh() -> str | None:
    return os.environ.get("ONCE_GH") or shutil.which("gh")


def default_branch(root) -> str:
    r = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD", check=False)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().split("/", 1)[1]
    for b in ("main", "master"):
        if _git(root, "rev-parse", "--verify", "-q", f"refs/remotes/origin/{b}", check=False).returncode == 0:
            return b
    return "main"


def shareable(slug: str, root) -> list[dict]:
    """Active rules on this machine that the repo does not already have, or has in another wording."""
    shared = {}
    sd = Path(root) / rules.SHARED_DIR
    if sd.is_dir():
        for f in sd.glob("*.yaml"):
            try:
                r = yaml.safe_load(f.read_text()) or {}
                shared[r.get("id")] = (r.get("rule") or {}).get("directive")
            except Exception:
                pass
    out = []
    for r in rules.load_all(slug, include_shared=False):
        if r.get("status") != "active" or "_error" in r:
            continue
        if shared.get(r["id"]) == (r.get("rule") or {}).get("directive"):
            continue
        out.append(r)
    return out


def _public(r: dict) -> dict:
    """What goes into the repository: the rule and its evidence, not this machine's session ids or chat text."""
    prov = r.get("provenance") or {}
    ev = ((r.get("evidence") or {}).get("replay") or {})
    out = {"schema": r.get("schema", 1), "id": r["id"], "status": "active", "kind": r.get("kind", "invariant"),
           "scope": {"path_globs": (r.get("scope") or {}).get("path_globs") or [], "agents": ["*"]},
           "rule": {"directive": (r.get("rule") or {}).get("directive", ""),
                    "rationale": (r.get("rule") or {}).get("rationale", "")},
           "provenance": {"source_kind": prov.get("source_kind"), "created": prov.get("created"),
                          "approved_by": prov.get("approved_by")}}
    if ev.get("verdict"):
        out["evidence"] = {"replay": {k: ev.get(k) for k in ("verdict", "with", "without", "runs", "agent", "model")
                                      if ev.get(k) is not None}}
    return out


README = f"""# Agent rules for this repository

Each file here is one convention for coding agents (Claude Code, Antigravity), written by a person or drafted
from a real session and accepted by a person. Rules with `evidence.replay.verdict: passes` were tested: the agent
failed the original task without the rule and passed with it.

If you use `once`, these are given to your agent automatically. If you don't, you can read them
as plain documentation, or run `{BIN} export` to turn them into an AGENTS.md section.
"""


def pr_body(rs: list[dict]) -> str:
    lines = [f"Adds {len(rs)} agent rule{'s' if len(rs) != 1 else ''} to `{rules.SHARED_DIR}/`.", "",
             "| Rule | Applies to | Tested |", "| --- | --- | --- |"]
    for r in rs:
        gl = (r.get("scope") or {}).get("path_globs") or []
        v = (((r.get("evidence") or {}).get("replay") or {}).get("verdict")) or "not yet"
        lines.append(f"| {(r.get('rule') or {}).get('directive', '').replace('|', '/')} | "
                     f"{', '.join(f'`{g}`' for g in gl) or 'whole repo'} | {v} |")
    lines += ["", "After the merge, everyone who uses `once` in this repo gets these rules at the start of their agent "
                  "sessions. Nothing changes for anyone else.", "", f"Opened with `{BIN} share`."]
    return "\n".join(lines)


def share(slug: str, root, dry_run: bool = False, push: bool = True, say=print) -> dict:
    rs = shareable(slug, root)
    if not rs:
        return {"rules": 0}
    base = default_branch(root)
    branch = f"once/rules-{time.strftime('%Y%m%d-%H%M%S')}"
    if dry_run:
        return {"rules": len(rs), "branch": branch, "base": base, "ids": [r["id"] for r in rs], "body": pr_body(rs)}
    _git(root, "fetch", "-q", "origin", base, check=False)
    start = f"origin/{base}" if _git(root, "rev-parse", "--verify", "-q", f"origin/{base}", check=False).returncode == 0 \
        else "HEAD"
    tmp = Path(tempfile.mkdtemp(prefix="once-share-"))
    wt = tmp / "wt"
    try:
        _git(root, "worktree", "add", "-q", "-b", branch, str(wt), start)
        d = wt / rules.SHARED_DIR
        d.mkdir(parents=True, exist_ok=True)
        for r in rs:
            (d / f"{r['id']}.yaml").write_text(yaml.safe_dump(_public(r), sort_keys=False, allow_unicode=True))
        if not (d / "README.md").exists():
            (d / "README.md").write_text(README)
        _git(wt, "add", rules.SHARED_DIR)
        _git(wt, "-c", "user.name=" + (_git(root, "config", "user.name", check=False).stdout.strip() or "once"),
             "-c", "user.email=" + (_git(root, "config", "user.email", check=False).stdout.strip() or "once@localhost"),
             "commit", "-q", "-m", f"Add {len(rs)} agent rule{'s' if len(rs) != 1 else ''} ({BIN} share)")
        out = {"rules": len(rs), "branch": branch, "base": base, "ids": [r["id"] for r in rs]}
        if not push:
            return out
        p = _git(wt, "push", "-q", "-u", "origin", branch, check=False)
        if p.returncode != 0:
            out["error"] = f"git push failed: {(p.stderr or '').strip().splitlines()[-1:] or ['?']}"[:300]
            return out
        tool = gh()
        if tool:
            r = subprocess.run([tool, "pr", "create", "--base", base, "--head", branch,
                                "--title", f"Agent rules: {len(rs)} from {BIN}", "--body", pr_body(rs)],
                               cwd=str(wt), capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                out["url"] = (r.stdout.strip().splitlines() or [""])[-1]
            else:
                out["error"] = f"gh pr create failed: {(r.stderr or r.stdout).strip()[:300]}"
        else:
            out["error"] = "gh is not installed; the branch is pushed, open the pull request on GitHub"
        return out
    finally:
        _git(root, "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ pull request check

def cochange(root, limit: int = 600, min_commits: int = 5, min_share: float = 0.6) -> dict:
    """file -> [(mate, share of that file's commits that also touched mate)], from recent history."""
    log = _git(root, "log", f"-{limit}", "--no-merges", "--name-only", "--format=tformat:@@", check=False).stdout
    commits = [[f for f in c.split() if f] for c in log.split("@@") if c.strip()]
    commits = [c for c in commits if 2 <= len(c) <= 15]
    count, pair = {}, {}
    for c in commits:
        for f in c:
            count[f] = count.get(f, 0) + 1
        for x in c:
            for y in c:
                if x != y:
                    pair[(x, y)] = pair.get((x, y), 0) + 1
    out = {}
    for (x, y), k in pair.items():
        n = count[x]
        if n >= min_commits and k >= 4 and k / n >= min_share:
            out.setdefault(x, []).append((y, k / n, n))
    for x in out:
        out[x].sort(key=lambda t: -t[1])
    return out


def changed_files(root, pr: str | None) -> tuple[list[str], str | None]:
    tool = gh()
    if pr and tool:
        r = subprocess.run([tool, "pr", "view", str(pr), "--json", "files,number"], cwd=str(root),
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            data = json.loads(r.stdout)
            return [f["path"] for f in data.get("files", [])], str(data.get("number", pr))
    base = default_branch(root)
    ref = f"origin/{base}" if _git(root, "rev-parse", "--verify", "-q", f"origin/{base}", check=False).returncode == 0 else base
    r = _git(root, "diff", "--name-only", f"{ref}...HEAD", check=False)
    return [l for l in r.stdout.splitlines() if l.strip()], pr


def pr_check(slug: str, root, pr: str | None = None) -> dict:
    files, number = changed_files(root, pr)
    pairs = cochange(root)
    have = set(files)
    missed = {}
    for f in files:
        for mate, share_, n in pairs.get(f, []):
            if mate not in have and Path(root, mate).exists():
                cur = missed.get(mate)
                if cur is None or share_ > cur[1]:
                    missed[mate] = (f, share_, n)
    idx = index.load(slug)
    applies = {}
    for f in files:
        for r in index.matching(idx, f):
            applies.setdefault(r["id"], (r["text"], f))
    for r in index.repo_wide(idx):
        applies.setdefault(r["id"], (r["text"], None))
    return {"pr": number, "files": files,
            "missed": [{"file": m, "because": f, "share": s, "commits": n} for m, (f, s, n) in
                       sorted(missed.items(), key=lambda kv: -kv[1][1])],
            "rules": [{"id": k, "text": t, "matched": f} for k, (t, f) in applies.items()]}


def pr_comment(res: dict) -> str:
    lines = ["**once** checked this pull request against the repository's history and agent rules.", ""]
    if res["missed"]:
        lines.append("Files that usually change together with these, but are not in this pull request:")
        for m in res["missed"][:8]:
            lines.append(f"- `{m['file']}`: changed with `{m['because']}` in {100 * m['share']:.0f}% "
                         f"of its last {m['commits']} commits")
        lines.append("")
    else:
        lines += ["No usual companion files are missing.", ""]
    scoped = [r for r in res["rules"] if r["matched"]]
    if scoped:
        lines.append("Rules for the files touched here:")
        for r in scoped[:8]:
            lines.append(f"- {r['text']}  _(applies to `{r['matched']}`)_")
        lines.append("")
    lines.append(f"<sub>Generated by `{BIN} pr-check`. It reads git history only; no code leaves the repository.</sub>")
    return "\n".join(lines)


def post_comment(root, pr: str, body: str) -> tuple[bool, str]:
    tool = gh()
    if not tool:
        return False, "gh is not installed"
    r = subprocess.run([tool, "pr", "comment", str(pr), "--body-file", "-"], cwd=str(root), input=body,
                       capture_output=True, text=True, timeout=60)
    return r.returncode == 0, (r.stdout or r.stderr).strip()[:300]
