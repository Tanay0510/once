import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

from once import cli, paths, store, tasks

SLUG = "github.com/acme/api"


def _commit(repo, msg):
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", msg], check=True)
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def _setup(env, monkeypatch):
    repo = env["repo"]
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_other.py").write_text("def test_ok():\n    assert True\n")
    _commit(repo, "buggy")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "tests" / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
    fix = _commit(repo, "fix add")
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b  # tidy\n")
    (repo / "tests" / "test_other.py").write_text("def test_ok():\n    assert 1 == 1\n")
    tidy = _commit(repo, "tidy")
    cli.main(["init", "--yes"])
    cfg = paths.repo_memory(SLUG) / "repo.yaml"
    cfg.write_text(yaml.safe_dump({"check_command": f"{sys.executable} -m pytest -p no:cacheprovider"}))
    prs = [
        {"number": 10, "title": "Fix add", "body": "Fixes #7\n\nThe fix is to use plus.", "merged_at": "2026-01-02T00:00:00Z", "merge_commit_sha": fix},
        {"number": 11, "title": "Tidy", "body": "closes #8", "merged_at": "2026-01-03T00:00:00Z", "merge_commit_sha": tidy},
        {"number": 12, "title": "Docs", "body": "fixes #9", "merged_at": "2026-01-04T00:00:00Z", "merge_commit_sha": tidy},
        {"number": 13, "title": "No issue", "body": "", "merged_at": "2026-01-05T00:00:00Z", "merge_commit_sha": tidy},
        {"number": 14, "title": "Unmerged", "body": "fixes #7", "merged_at": None, "merge_commit_sha": None},
    ]
    files = {10: ["calc.py", "tests/test_calc.py"], 11: ["calc.py", "tests/test_other.py"], 12: ["README.md", "tests/test_other.py"]}
    issues = {7: {"title": "add() returns the wrong sum", "body": "add(2, 3) gives -1."}, 8: {"title": "tidy", "body": ""},
              9: {"title": "docs", "body": ""}}

    def fake(path):
        if "/pulls?" in path:
            return prs if "page=1" in path else []
        m = re.search(r"/pulls/(\d+)/files", path)
        if m:
            return [{"filename": f, "status": "modified"} for f in files.get(int(m.group(1)), [])]
        if re.search(r"/pulls/\d+$", path):
            return {"review_comments": 3}
        m = re.search(r"/issues/(\d+)$", path)
        return issues.get(int(m.group(1))) if m else None

    monkeypatch.setattr(tasks, "api", fake)
    return repo


def test_mine_and_validate(env, monkeypatch, capsys):
    repo = _setup(env, monkeypatch)
    assert cli.main(["lab", "tasks", "mine"]) == 0
    out = capsys.readouterr().out
    assert "Kept 2 of 4 merged PRs" in out
    ts = {t["pr"]: t for t in tasks.load_all(SLUG)}
    assert set(ts) == {10, 11}
    t = ts[10]
    assert t["prompt"].startswith("add() returns the wrong sum") and "use plus" not in t["prompt"]  # issue text, never the PR body
    assert t["test_files"] == ["tests/test_calc.py"] and t["check"].endswith("-q -x tests/test_calc.py")
    assert cli.main(["lab", "tasks", "mine"]) == 0 and "Kept 0" in capsys.readouterr().out  # idempotent

    assert cli.main(["lab", "tasks", "validate"]) == 0
    out = capsys.readouterr().out
    assert "1 of 2 valid" in out and "passes even before the fix" in out
    ts = {t["pr"]: t for t in tasks.load_all(SLUG)}
    assert ts[10]["valid"] is True and ts[11]["valid"] is False and ts[10]["split"] in ("train", "heldout")
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout == ""


def test_lab_run(env, monkeypatch, capsys):
    repo = _setup(env, monkeypatch)
    cli.main(["lab", "tasks", "mine"])
    cli.main(["lab", "tasks", "validate", "10"])
    cli.main(["teach", "keep functions pure"])
    agent = f'{sys.executable} {Path(__file__).parent / "fake_fixer.py"} {{prompt}}'
    capsys.readouterr()

    monkeypatch.setenv("FAKE_FIX", "0")
    assert cli.main(["lab", "run", "10", "--agent-cmd", agent, "--yes"]) == 0
    assert "FAILED the PR's tests" in capsys.readouterr().out
    monkeypatch.setenv("FAKE_FIX", "1")
    assert cli.main(["lab", "run", "10", "--agent-cmd", agent, "--with-rules", "--yes", "--model", "cheap"]) == 0
    out = capsys.readouterr().out
    assert "PASSED the PR's tests" in out and "1 files changed" in out and "$0.02" in out

    con = store.connect()
    store.sync(con)
    rows = store.sessions(con, SLUG)
    assert len(rows) == 2  # lab runs show up as sessions of this repo, so they can be tagged
    labels = [next(e["label"] for e in store.events(con, r["id"]) if e.get("event") == "session_start") for r in rows]
    assert {l["arm"] for l in labels} == {"bare", "rules"} and all(l["task"] == "acme-api-10" for l in labels)
    assert sorted(r["rules_given"] for r in rows) == [0, 1]  # bare gets none, the rules arm gets the taught rule
    assert "add() returns the wrong sum" in rows[0]["prompt"]
    capsys.readouterr()
    cli.main(["last", "--no-input"])
    assert "lab run: acme-api-10" in capsys.readouterr().out
    assert (repo / "calc.py").read_text().endswith("# tidy\n")  # the real working tree is untouched
