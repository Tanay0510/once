"""v0.6: once where the developer already is. Chat corrections, asking in the agent, status line,
notifications, menu bar, digest, sharing by pull request, pull request check, editor extension."""
import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from conftest import run_hook
from once import ambient, cli, index, mcp_server, paths, rules, team
from once.adapters import claude_code as cc

SLUG = "github.com/acme/api"
GIT = ["-c", "user.name=t", "-c", "user.email=t@t"]


# ------------------------------------------------------------------ teach by talking

@pytest.mark.parametrize("text,expect", [
    ("no, use tox instead of pytest", True),
    ("remember that we run tests with tox here", True),
    ("From now on, put new commands in src/click/commands/", True),
    ("don't use requests, use httpx", True),
    ("in this repo we never edit generated files", True),
    ("Fix the timezone bug in token expiry", False),
    ("why did you use pytest?", False),
    ("no", False),
    ("/once-teach use tox", False),
    ("Can you use tox to run the tests?", False),
])
def test_is_correction(text, expect):
    assert ambient.is_correction(text) is expect


@pytest.mark.parametrize("text,want", [
    ("no, use tox instead of pytest", "Use tox instead of pytest."),
    ("remember that we run tests with tox here", "We run tests with tox here."),
    ("From now on, put new commands in src/click/commands/", "Put new commands in src/click/commands/."),
    ("actually please don't use requests, use httpx!", "Don't use requests, use httpx!"),
])
def test_directive_from(text, want):
    assert ambient.directive_from(text) == want


def _wait(pred, secs=15):
    end = time.time() + secs
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.2)
    return False


def _hook(repo, sid, event, **kw):
    r = run_hook({"session_id": sid, "cwd": str(repo), "hook_event_name": event, **kw})
    assert r.returncode == 0
    return r.stdout


def test_chat_correction_becomes_suggestion_then_agent_asks(env):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    cli.main(["teach", "always run cargo fmt before finishing"])

    _hook(repo, "c1", "SessionStart", source="startup")
    _hook(repo, "c1", "UserPromptSubmit", prompt="Fix the token expiry bug")
    _hook(repo, "c1", "UserPromptSubmit", prompt="no, use cargo nextest instead of cargo test")
    assert _wait(lambda: index.load(SLUG).get("pending")), "the background capture made no suggestion"
    pend = index.load(SLUG)["pending"]
    assert len(pend) == 1 and pend[0]["text"] == "Use cargo nextest instead of cargo test." and pend[0]["source"] == "chat"
    cand = rules.find(SLUG, pend[0]["id"])
    assert cand["status"] == "candidate" and cand["provenance"]["chat_text"].startswith("no, use cargo nextest")

    # the status line says so, right away
    line = ambient.statusline({"session_id": "c1", "workspace": {"current_dir": str(repo)}})
    assert "1 to review" in line and "noted: Use cargo nextest" in line

    # the session ends with one line that says what happens next
    end = json.loads(_hook(repo, "c1", "SessionEnd", reason="exit"))
    assert "noted 1 correction" in end["systemMessage"] and "your agent will ask you next session" in end["systemMessage"]

    # next session: the rules, then the question for the developer
    out = json.loads(_hook(repo, "c2", "SessionStart", source="startup"))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "cargo fmt" in ctx and "<once-review>" in ctx and pend[0]["id"] in ctx and "review_rule" in ctx
    # asked at most twice per suggestion, then left for `once review`
    assert "<once-review>" in json.loads(_hook(repo, "c3", "SessionStart", source="startup"))["hookSpecificOutput"]["additionalContext"]
    third = _hook(repo, "c4", "SessionStart", source="startup")
    assert "<once-review>" not in third and "cargo fmt" in third

    # the developer answers in chat; the agent records it
    msg = mcp_server.call("review_rule", {"id": pend[0]["id"], "decision": "edit",
                                          "text": "Run tests with cargo nextest, not cargo test."}, str(repo))
    assert "Kept" in msg and "wording" in msg
    r = rules.find(SLUG, pend[0]["id"])
    assert r["status"] == "active" and r["rule"]["directive"] == "Run tests with cargo nextest, not cargo test."
    assert not index.load(SLUG).get("pending") and any(x["id"] == r["id"] for x in index.load(SLUG)["rules"])
    assert "already decided" in mcp_server.call("review_rule", {"id": r["id"], "decision": "drop"}, str(repo))


def test_teach_by_talking_can_be_switched_off(env):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    cli.main(["config", "teach_by_talking", "false"])
    _hook(repo, "q1", "SessionStart", source="startup")
    _hook(repo, "q1", "UserPromptSubmit", prompt="remember to use uv run")
    time.sleep(1.5)
    assert not index.load(SLUG).get("pending")


def test_remember_and_drop_through_mcp(env):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    msg = mcp_server.call("remember_rule", {"directive": "Use uv run for every Python command."}, str(repo))
    assert msg.startswith("Saved as")
    rid = msg.split()[2].rstrip(".")
    assert rules.find(SLUG, rid)["status"] == "active"
    assert "Already known" in mcp_server.call("remember_rule", {"directive": "Use uv run for every Python command."}, str(repo))

    # a correction noted from the chat, then the same thing asked to be remembered: one rule, now active
    c = ambient.capture_correction(SLUG, repo, "no, never edit files under generated/", "s9", "claude-code")
    assert c["status"] == "candidate"
    out = mcp_server.call("remember_rule", {"directive": "Never edit files under generated/."}, str(repo))
    assert c["id"] in out and rules.find(SLUG, c["id"])["status"] == "active"

    d = ambient.capture_correction(SLUG, repo, "stop, use ruff instead of flake8", "s9", "claude-code")
    assert "Dropped" in mcp_server.call("review_rule", {"id": d["id"], "decision": "drop", "reason": "one-off"}, str(repo))
    assert rules.find(SLUG, d["id"])["status"] == "deprecated"
    assert "No rule" in mcp_server.call("review_rule", {"id": "nope", "decision": "keep"}, str(repo))
    names = {t["name"] for t in mcp_server.TOOLS}
    assert {"review_rule", "remember_rule"} <= names


# ------------------------------------------------------------------ status line

def test_statusline_install_wraps_and_restores(env, tmp_path):
    s = env["settings"]
    s.parent.mkdir(parents=True, exist_ok=True)
    s.write_text(json.dumps({"statusLine": {"type": "command", "command": "echo mine"}, "model": "x"}))
    cli.main(["init", "--yes"])
    cfg = json.loads(s.read_text())
    assert cc.STATUS_MARK in cfg["statusLine"]["command"] and cfg["model"] == "x"
    assert ambient.config()["statusline_wrap"] == "echo mine"

    cli.main(["teach", "always run cargo fmt before finishing"])
    r = subprocess.run([sys.executable, "-m", "once.statusline"], input=json.dumps({"cwd": str(env["repo"])}),
                       capture_output=True, text=True, env=os.environ.copy())
    assert r.stdout == "mine │ once ● 1 rule"
    cli.main(["off"])
    assert ambient.statusline({"cwd": str(env["repo"])}) == "once ○ paused"
    cli.main(["on"])

    cli.main(["uninstall"])
    assert json.loads(s.read_text())["statusLine"] == {"type": "command", "command": "echo mine"}
    assert "statusline_wrap" not in ambient.config()


def test_statusline_counts_rules_in_use_and_flags(env):
    from conftest import payloads
    repo = env["repo"]
    cli.main(["init", "--yes"])
    cli.main(["teach", "always run cargo fmt before finishing"])
    for p in payloads(repo, "f1")[:-1]:
        run_hook(p)
    line = ambient.statusline({"session_id": "f1", "cwd": str(repo)})
    assert line.startswith("once ● 1 of 1 rules in use") and "flagged" in line


# ------------------------------------------------------------------ notifications, menu bar, digest

def test_notify_and_switch(env, tmp_path, monkeypatch):
    log = tmp_path / "n.jsonl"
    monkeypatch.setenv("ONCE_NOTIFY_LOG", str(log))
    assert ambient.notify("once", "2 rules waiting", "http://127.0.0.1:4317/review")
    assert json.loads(log.read_text())["body"] == "2 rules waiting"
    cli.main(["config", "notifications", "false"])
    assert not ambient.notify("once", "again")
    assert len(log.read_text().splitlines()) == 1


def test_menubar(env, tmp_path, monkeypatch, capsys):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    ambient.capture_correction(SLUG, repo, "no, use cargo nextest instead of cargo test", "m1", "claude-code")
    capsys.readouterr()
    cli.main(["menubar"])
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("◆ 1") and out[1] == "---"
    assert any("Use cargo nextest" in l for l in out) and any("Review in browser | href=http://127.0.0.1:4317/review" in l for l in out)
    assert any(l.startswith("Open dashboard | bash=") and "param" in l and "ui" in l for l in out)
    assert any(l.startswith("Pause everywhere") for l in out)
    cli.main(["off", "--everywhere"])
    capsys.readouterr()
    cli.main(["menubar"])
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("◇ once paused") and any(l.startswith("Resume everywhere") for l in out)

    d = tmp_path / "plugins"
    monkeypatch.setenv("ONCE_MENUBAR_DIR", str(d))
    assert cli.main(["menubar", "--install"]) == 0
    f = d / ambient.PLUGIN_NAME
    assert f.exists() and os.access(f, os.X_OK) and "once.cli menubar" in f.read_text()
    cli.main(["uninstall"])
    assert not f.exists()


class _Slack(http.server.BaseHTTPRequestHandler):
    got = []

    def do_POST(self):
        _Slack.got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def test_digest(env, tmp_path, monkeypatch):
    from conftest import payloads
    repo = env["repo"]
    cli.main(["init", "--yes"])
    for p in payloads(repo, "d1"):
        run_hook(p)
    text, w = ambient.digest_text(7)
    assert w["sessions"] == 1 and w["avoidable"] > 0 and "Most repeated problem: retry cluster" in text

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Slack)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cli.main(["config", "slack_webhook", f"http://127.0.0.1:{srv.server_address[1]}/hook"])
    monkeypatch.setenv("ONCE_NOTIFY_LOG", str(tmp_path / "n.jsonl"))
    went = ambient.send_digest(text)
    srv.shutdown()
    assert "Slack" in went and "notification" in went and Path(went[0]).read_text().startswith("once: your agents")
    assert _Slack.got and "retry cluster" in _Slack.got[0]["text"]

    # weekly, by itself: not on install day, then once a week
    (paths.local() / "digest_state.json").unlink()
    assert not ambient.digest_due()
    st = paths.local() / "digest_state.json"
    st.write_text(json.dumps({"last": time.time() - 8 * 86400}))
    assert ambient.digest_due()
    cli.main(["config", "digest", "off"])
    assert not ambient.digest_due()


# ------------------------------------------------------------------ team: share by pull request, pr-check

@pytest.fixture
def remote_repo(tmp_path, monkeypatch):
    """A repo whose origin is a local bare repo, with history in which a.py and CHANGES.rst change together."""
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(bare), str(work)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b", "main"], check=True)
    (work / "src").mkdir()
    for i in range(6):
        (work / "src" / "a.py").write_text(f"x = {i}\n")
        (work / "CHANGES.rst").write_text(f"- change {i}\n")
        (work / "src" / f"other{i}.py").write_text("y = 1\n")
        subprocess.run(["git", "-C", str(work), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(work), *GIT, "commit", "-qm", f"c{i}"], check=True)
    subprocess.run(["git", "-C", str(work), "push", "-q", "-u", "origin", "main"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(bare), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
    subprocess.run(["git", "-C", str(work), "remote", "set-head", "origin", "main"], check=True)
    monkeypatch.setenv("ONCE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ONCE_OLD_HOME", str(tmp_path / "no-old-install"))
    monkeypatch.setenv("ONCE_CLAUDE_SETTINGS", str(tmp_path / "claude" / "settings.json"))
    monkeypatch.setenv("ONCE_AGY_HOOKS", str(tmp_path / "gemini" / "config" / "hooks.json"))  # never the real agents' config
    monkeypatch.setenv("ONCE_CODEX_HOOKS", str(tmp_path / "codex" / "hooks.json"))
    monkeypatch.chdir(work)
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(f"#!{sys.executable}\nimport sys, json\nopen({str(tmp_path / 'gh.log')!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                       "if sys.argv[1:3] == ['pr', 'create']: print('https://github.com/acme/api/pull/7')\n"
                       "if sys.argv[1:3] == ['pr', 'view']: print(json.dumps({'number': 7, 'files': [{'path': 'src/a.py'}]}))\n"
                       "if sys.argv[1:3] == ['pr', 'comment']: sys.stdin.read(); print('ok')\n")
    fake_gh.chmod(0o755)
    monkeypatch.setenv("ONCE_GH", str(fake_gh))
    return {"bare": bare, "work": work, "gh_log": tmp_path / "gh.log", "tmp": tmp_path}


def test_share_opens_pull_request_and_teammates_get_rules(remote_repo, monkeypatch, capsys):
    work, bare = remote_repo["work"], remote_repo["bare"]
    cli.main(["init", "--yes"])
    cli.main(["teach", "update CHANGES.rst for every user-facing change"])
    slug, _ = cli._ctx()
    (work / "scratch.txt").write_text("uncommitted\n")
    capsys.readouterr()
    assert cli.main(["share", "--dry-run"]) == 0
    assert "1 rule to share" in capsys.readouterr().out

    assert cli.main(["share", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Opened https://github.com/acme/api/pull/7" in out
    assert (work / "scratch.txt").read_text() == "uncommitted\n"  # working copy untouched
    assert subprocess.run(["git", "-C", str(work), "worktree", "list"], capture_output=True, text=True).stdout.count("\n") == 1
    branches = subprocess.run(["git", "-C", str(bare), "branch", "--list", "once/rules-*"], capture_output=True, text=True).stdout
    br = branches.strip().lstrip("* ").strip()
    assert br.startswith("once/rules-")
    files = subprocess.run(["git", "-C", str(bare), "ls-tree", "-r", "--name-only", br], capture_output=True, text=True).stdout
    assert ".once/rules/README.md" in files and any(l.startswith(".once/rules/") and l.endswith(".yaml") for l in files.splitlines())
    calls = [json.loads(l) for l in remote_repo["gh_log"].read_text().splitlines()]
    create = next(c for c in calls if c[:2] == ["pr", "create"])
    assert "--base" in create and create[create.index("--base") + 1] == "main" and "CHANGES.rst" in create[create.index("--body") + 1]

    # after merge, a teammate with no local rules gets it
    subprocess.run(["git", "-C", str(work), "fetch", "-q", "origin"], check=True)
    subprocess.run(["git", "-C", str(work), *GIT, "merge", "-q", "--ff-only", f"origin/{br}"], check=True, capture_output=True)
    monkeypatch.setenv("ONCE_HOME", str(remote_repo["tmp"] / "teammate"))
    cli.main(["init", "--yes"])
    assert any("CHANGES.rst" in r["text"] for r in index.load(slug)["rules"])
    assert cli.main(["share", "--dry-run"]) == 0 and "Nothing to share" in capsys.readouterr().out


def test_pr_check_finds_missed_companion_file(remote_repo, capsys):
    work = remote_repo["work"]
    cli.main(["init", "--yes"])
    cli.main(["teach", "--path", "src/**", "keep public functions typed"])
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b", "feature"], check=True)
    (work / "src" / "a.py").write_text("x = 99\n")
    subprocess.run(["git", "-C", str(work), *GIT, "commit", "-qam", "only a"], check=True)
    capsys.readouterr()
    assert cli.main(["pr-check"]) == 0
    out = capsys.readouterr().out
    assert "`CHANGES.rst`: changed with `src/a.py` in 100%" in out and "keep public functions typed" in out
    assert cli.main(["pr-check", "7", "--post"]) == 0
    calls = [json.loads(l) for l in remote_repo["gh_log"].read_text().splitlines()]
    assert ["pr", "view", "7", "--json", "files,number"] in calls and any(c[:3] == ["pr", "comment", "7"] for c in calls)


# ------------------------------------------------------------------ editor

def test_editor_extension_installs(env, tmp_path, monkeypatch):
    fake = tmp_path / "fakehome"
    monkeypatch.setitem(cli.EDITOR_DIRS, "vscode", fake / ".vscode" / "extensions")
    monkeypatch.setitem(cli.EDITOR_DIRS, "cursor", fake / ".cursor" / "extensions")
    (fake / ".cursor").mkdir(parents=True)
    assert cli.main(["editor", "install"]) == 0  # finds Cursor only
    d = fake / ".cursor" / "extensions" / cli.EXT_NAME
    pkg = json.loads((d / "package.json").read_text())
    assert pkg["main"] == "./extension.js" and (d / "extension.js").exists() and not (fake / ".vscode").exists()
    js = (d / "extension.js").read_text()
    assert '"statusline"' in js and '"--detach"' in js
    assert cli.main(["editor", "uninstall", "cursor"]) == 0 and not d.exists()


def test_ui_detach_starts_once(env, monkeypatch):
    port = 4400 + os.getpid() % 500
    assert not ambient.ui_running(port)
    url = ambient.ensure_ui(port, str(env["repo"]))
    try:
        assert ambient.ui_running(port) and url.endswith(str(port))
        assert ambient.ensure_ui(port) == url
    finally:
        subprocess.run(["pkill", "-f", f"ui --no-open --port {port}"], check=False)


def test_pr_check_on_a_fresh_machine_uses_the_rules_committed_to_the_repo(remote_repo, capsys, monkeypatch, tmp_path):
    """What the GitHub Action does: no once init, no local index; only the repo and its .once/rules/."""
    work = remote_repo["work"]
    cli.main(["init", "--yes"])
    cli.main(["teach", "--path", "src/**", "keep public functions typed"])
    rule = next((paths.memory()).rglob("rules/*.yaml"))
    (work / ".once" / "rules").mkdir(parents=True, exist_ok=True)
    (work / ".once" / "rules" / rule.name).write_text(rule.read_text())
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b", "feature2"], check=True)
    (work / "src" / "a.py").write_text("x = 7\n")
    subprocess.run(["git", "-C", str(work), *GIT, "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(work), *GIT, "commit", "-qm", "a and team rule"], check=True)
    monkeypatch.setenv("ONCE_HOME", str(tmp_path / "ci-home"))  # a CI runner: nothing local
    capsys.readouterr()
    assert cli.main(["pr-check"]) == 0
    assert "keep public functions typed" in capsys.readouterr().out
    assert cli.main(["pr-check", "8", "--post"]) == 0
    calls = [json.loads(l) for l in remote_repo["gh_log"].read_text().splitlines()]
    assert any(c[:3] == ["pr", "comment", "7"] and "--edit-last" in c for c in calls)  # one comment, updated


def test_pr_check_if_found_keeps_clean_pull_requests_quiet(remote_repo, capsys):
    work = remote_repo["work"]
    cli.main(["init", "--yes"])
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b", "clean"], check=True)
    (work / "src" / "other0.py").write_text("y = 2\n")  # never changes with anything else
    subprocess.run(["git", "-C", str(work), *GIT, "commit", "-qam", "clean change"], check=True)
    fake = remote_repo["tmp"] / "gh"  # this PR touches only other0.py
    fake.write_text(fake.read_text().replace("'src/a.py'", "'src/other0.py'"))
    capsys.readouterr()
    assert cli.main(["pr-check", "7", "--post", "--if-found"]) == 0
    assert "no comment posted" in capsys.readouterr().out
    calls = [json.loads(l) for l in remote_repo["gh_log"].read_text().splitlines()]
    assert not any(c[:2] == ["pr", "comment"] for c in calls)
