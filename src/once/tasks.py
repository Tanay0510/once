"""Task miner: every merged fix PR that links an issue and changes tests is a ready-made task.

The issue text is the prompt, the PR's parent commit is the starting tree, and the tests the PR
added are the check. Uses the GitHub REST API through `gh api`, or plain HTTPS if gh is missing.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from . import paths, replay

TEST_RX = re.compile(r"(^|/)(tests?|testing|spec|__tests__)/|(^|/)test_[^/]+\.py$|_test\.(py|go|rs)$|\.(test|spec)\.[jt]sx?$")
SKIP_RX = re.compile(r"(^|/)(docs?|\.github|changes|changelog)(/|\.|$)|\.(md|rst|txt|lock|toml|cfg|ini|ya?ml)$", re.I)
CLOSES_RX = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+(?:https://github\.com/[\w.\-]+/[\w.\-]+/issues/|[\w.\-]+/[\w.\-]+#|#)(\d+)")
MENTION_RX = re.compile(r"(?:^|[\s(])#(\d+)\b")


class MineError(Exception):
    pass


def api(path: str):
    """GET a GitHub REST path. Prefers gh (your login, 5,000 requests an hour)."""
    if shutil.which("gh"):
        r = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return json.loads(r.stdout)
        if "404" in r.stderr:
            return None
        raise MineError((r.stderr or r.stdout).strip().splitlines()[-1][:200])
    req = urllib.request.Request("https://api.github.com/" + path, headers={"Accept": "application/vnd.github+json"})
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise MineError(f"GitHub API {e.code}. Install the GitHub CLI and run `gh auth login`, or set GITHUB_TOKEN.")


def folder(slug: str) -> Path:
    return paths.repo_memory(slug) / "tasks"


def load_all(slug: str) -> list[dict]:
    d = folder(slug)
    out = []
    if d.is_dir():
        for f in d.glob("*.yaml"):
            try:
                t = yaml.safe_load(f.read_text()) or {}
                t["_file"] = str(f)
                out.append(t)
            except Exception:
                pass
    return sorted(out, key=lambda t: t.get("merged_at") or "")


def find(slug: str, key: str) -> dict | None:
    hits = [t for t in load_all(slug) if str(t.get("id", "")).endswith(key) or str(t.get("pr")) == key.lstrip("#")]
    return hits[0] if len(hits) == 1 else None


def save(slug: str, task: dict):
    d = folder(slug)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{task['id']}.yaml").write_text(yaml.safe_dump({k: v for k, v in task.items() if not k.startswith("_")},
                                                         sort_keys=False, allow_unicode=True))


def effective_check(slug: str, root: Path) -> tuple[str | None, list[str]]:
    """The check command and setup steps for this repo, from repo.yaml with a sensible default for uv projects."""
    try:
        cfg = yaml.safe_load((paths.repo_memory(slug) / "repo.yaml").read_text()) or {}
    except Exception:
        cfg = {}
    check = cfg.get("check_command")
    if check and re.match(r"^(python -m )?pytest\b", check) and (root / "uv.lock").exists():
        check = "uv run " + re.sub(r"^python -m ", "", check)
    return check, list(cfg.get("setup") or [])


def task_check(check: str | None, test_files: list[str]) -> str | None:
    if not check:
        return None
    if "pytest" in check:
        return f"{check} -q -x " + " ".join(test_files)
    return check


def _have_commit(root: Path, sha: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True).returncode == 0


def mine(slug: str, root: Path, limit: int = 60, max_files: int = 12, progress=print) -> dict:
    if not slug.startswith("github.com/"):
        raise MineError("Mining needs a GitHub repo; this one has no github.com remote.")
    owner_repo = slug[len("github.com/"):]
    check, _ = effective_check(slug, root)
    existing = {t.get("pr") for t in load_all(slug)}
    stats = {"seen": 0, "merged": 0, "kept": 0, "no_issue": 0, "no_tests": 0, "too_big": 0, "no_commit": 0, "already": 0}
    page = 1
    while stats["merged"] < limit and page <= 10:
        prs = api(f"repos/{owner_repo}/pulls?state=closed&sort=updated&direction=desc&per_page=50&page={page}")
        if not prs:
            break
        for pr in prs:
            stats["seen"] += 1
            if not pr.get("merged_at") or not pr.get("merge_commit_sha"):
                continue
            if stats["merged"] >= limit:
                break
            stats["merged"] += 1
            n = pr["number"]
            if n in existing:
                stats["already"] += 1
                continue
            issue_no, how = linked_issue(owner_repo, pr)
            if not issue_no:
                stats["no_issue"] += 1
                continue
            files = api(f"repos/{owner_repo}/pulls/{n}/files?per_page=100") or []
            names = [f["filename"] for f in files if f.get("status") != "removed"]
            tests = [f for f in names if TEST_RX.search(f)]
            source = [f for f in names if not TEST_RX.search(f) and not SKIP_RX.search(f)]
            if not tests or not source:
                stats["no_tests"] += 1
                continue
            if len(names) > max_files:
                stats["too_big"] += 1
                continue
            issue = api(f"repos/{owner_repo}/issues/{issue_no}")
            if not issue or issue.get("pull_request"):
                stats["no_issue"] += 1
                continue
            sha = pr["merge_commit_sha"]
            if not _have_commit(root, sha):
                subprocess.run(["git", "-C", str(root), "fetch", "-q", "origin", sha], capture_output=True, timeout=120)
            base = subprocess.run(["git", "-C", str(root), "rev-parse", f"{sha}^"], capture_output=True, text=True).stdout.strip()
            if not base:
                stats["no_commit"] += 1
                continue
            detail = api(f"repos/{owner_repo}/pulls/{n}") or {}
            prompt = f"{issue.get('title', '').strip()}\n\n{(issue.get('body') or '').strip()}"[:6000]
            notes = review_notes(owner_repo, n) if detail.get("review_comments") else []
            save(slug, {"id": f"{owner_repo.replace('/', '-')}-{n}", "pr": n, "issue": int(issue_no), "issue_link": how,
                        "review_notes": notes,
                        "title": pr.get("title", ""), "prompt": prompt, "base_commit": base, "merge_commit": sha,
                        "test_files": tests, "source_files": source, "check": task_check(check, tests),
                        "merged_at": pr["merged_at"], "review_comments": detail.get("review_comments", 0),
                        "split": None, "valid": None})
            stats["kept"] += 1
            progress(f"  kept #{n}  {pr.get('title', '')[:64]}")
        page += 1
    assign_split(slug)
    return stats


def linked_issue(owner_repo: str, pr: dict) -> tuple[str | None, str]:
    """The issue a PR resolves: a closing keyword, then GitHub's own link, then a plain mention of a real issue."""
    text = (pr.get("body") or "") + "\n" + (pr.get("title") or "")
    m = CLOSES_RX.search(text)
    if m:
        return m.group(1), "keyword"
    if shutil.which("gh"):  # links made in the sidebar only show through GitHub's GraphQL API
        try:
            r = subprocess.run(["gh", "pr", "view", str(pr["number"]), "--repo", owner_repo, "--json",
                                "closingIssuesReferences"], capture_output=True, text=True, timeout=30)
            refs = json.loads(r.stdout).get("closingIssuesReferences") if r.returncode == 0 else []
            if refs:
                return str(refs[0]["number"]), "linked"
        except Exception:
            pass
    for n in list(dict.fromkeys(MENTION_RX.findall(pr.get("body") or "")))[:3]:
        if int(n) != pr["number"]:
            try:
                iss = api(f"repos/{owner_repo}/issues/{n}")
            except MineError:
                iss = None
            if iss and not iss.get("pull_request"):
                return n, "mention"
    return None, ""


def review_notes(owner_repo: str, n: int, limit: int = 8) -> list[str]:
    """Maintainers' review comments: the project's conventions in their own words."""
    try:
        cs = api(f"repos/{owner_repo}/pulls/{n}/comments?per_page=50") or []
    except MineError:
        return []
    out = []
    for c in cs:
        who = (c.get("user") or {})
        body = " ".join((c.get("body") or "").split())
        if who.get("type") == "Bot" or len(body) < 25:
            continue
        out.append(f"{c.get('path', '')}: {body[:400]}")
        if len(out) >= limit:
            break
    return out


def gold_files(root: Path, task: dict) -> list[str]:
    """Every file the real fix changed, read from local git so it needs no API call."""
    r = subprocess.run(["git", "-C", str(root), "diff", "--name-only", task["base_commit"], task["merge_commit"]],
                       capture_output=True, text=True)
    return [f for f in r.stdout.splitlines() if f.strip()]


def missed(root: Path, task: dict, changed: list[str]) -> dict:
    """What the real fix touched that the agent did not. Test files are counted separately."""
    gold = gold_files(root, task)
    tests = [f for f in gold if TEST_RX.search(f)]
    other = [f for f in gold if not TEST_RX.search(f)]
    return {"missed_files": [f for f in other if f not in changed],
            "wrote_tests": any(TEST_RX.search(f) for f in changed) if tests else None,
            "extra_files": [f for f in changed if f not in gold and not TEST_RX.search(f)]}


def assign_split(slug: str, train: float = 0.6):
    """By time, never at random: rules learned from later work must not leak into earlier tasks."""
    for t in load_all(slug):
        if t.get("valid") is False and t.get("split"):
            t["split"] = None
            save(slug, t)
    ts = [t for t in load_all(slug) if t.get("valid") is not False]
    cut = round(len(ts) * train)
    for i, t in enumerate(ts):
        want = "train" if i < cut else "heldout"
        if t.get("split") != want:
            t["split"] = want
            save(slug, t)


def apply_tests(root: Path, task: dict, work: Path):
    """Copy the PR's test files from the merge commit over the working copy."""
    for f in task.get("test_files", []):
        r = subprocess.run(["git", "-C", str(root), "show", f"{task['merge_commit']}:{f}"], capture_output=True)
        if r.returncode == 0:
            dest = work / f
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.stdout)


def validate(slug: str, root: Path, task: dict, timeout: int = 900) -> dict:
    """Keep a task only if its check fails before the fix and passes after it."""
    if not task.get("check"):
        return {"valid": None, "why": "no check command; set check_command in repo.yaml"}
    _, setup = effective_check(slug, root)
    base = Path(tempfile.mkdtemp(prefix="once-validate-"))
    out = {}
    try:
        for name, commit in (("before", task["base_commit"]), ("after", task["merge_commit"])):
            work = base / name
            replay.export(root, commit, work)
            apply_tests(root, task, work)
            for cmd in setup:
                subprocess.run(cmd, cwd=work, shell=True, capture_output=True, timeout=timeout)
            try:
                r = subprocess.run(task["check"], cwd=work, shell=True, capture_output=True, text=True, timeout=timeout)
                out[name] = r.returncode == 0
                out[name + "_tail"] = (r.stdout + r.stderr)[-400:]
            except subprocess.TimeoutExpired:
                out[name], out[name + "_tail"] = False, "timed out"
    finally:
        shutil.rmtree(base, ignore_errors=True)
    valid = out.get("before") is False and out.get("after") is True
    why = "fails before the fix, passes after" if valid else (
        "passes even before the fix" if out.get("before") else "does not pass after the fix (needs setup, network, or is flaky)")
    task = {k: v for k, v in task.items() if not k.startswith("_")}
    task.update({"valid": valid, "validation": {"date": time.strftime("%Y-%m-%d"), "why": why}})
    save(slug, task)
    return {"valid": valid, "why": why, **out}
