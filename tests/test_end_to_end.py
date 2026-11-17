import json
import time

from conftest import payloads, run_hook
from once import cli, paths, store
from once.adapters import claude_code as cc


def test_hook_fails_open(env):
    for bad in ("", "not json", "[1,2]", '{"hook_event_name": 7, "session_id": {"a": 1}}'):
        r = run_hook(bad)
        assert r.returncode == 0 and r.stdout == ""
    r = run_hook({"hook_event_name": "SessionStart", "session_id": "x", "cwd": "/nonexistent/dir"})
    assert r.returncode == 0


def test_kill_switch(env, monkeypatch):
    monkeypatch.setenv("ONCE_DISABLE", "1")
    r = run_hook(payloads(env["repo"])[0])
    assert r.returncode == 0 and r.stdout == ""
    assert not (paths.local() / "trajectories").exists()


def test_install_is_idempotent_and_reversible(env):
    env["settings"].parent.mkdir(parents=True)
    env["settings"].write_text(json.dumps({"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}}))
    cc.install()
    cc.install()
    s = json.loads(env["settings"].read_text())
    assert s["model"] == "x" and s["hooks"]["Stop"][0]["hooks"][0]["command"] == "mine"
    # One group per event, plus the synchronous enforcement group on PostToolUse.
    assert all(len(s["hooks"][e]) == 1 + (e in cc.ENFORCE) for e in cc.EVENTS) and all(cc.installed().values())
    cc.uninstall()
    s = json.loads(env["settings"].read_text())
    assert s == {"model": "x", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}}


def test_full_loop(env, capsys):
    repo = env["repo"]
    assert cli.main(["init", "--yes"]) == 0
    assert all(cc.installed().values())

    # Observe-only: with no rules, a session start injects nothing.
    r = run_hook(payloads(repo, "s0")[0])
    assert r.returncode == 0 and r.stdout == ""

    # Teach one repo-wide rule and one path-scoped rule.
    assert cli.main(["teach", "always run cargo fmt before finishing"]) == 0
    assert cli.main(["teach", "--path", "crates/auth/**/*.rs", "use chrono DateTime with Utc; never SystemTime"]) == 0
    native = list((repo / ".claude/rules/once").glob("*.md"))
    assert len(native) == 1 and 'crates/auth/**/*.rs' in native[0].read_text()
    assert ".claude/rules/once/" in (repo / ".git/info/exclude").read_text()

    # A session: the repo-wide rule is injected at start, in the documented reply shape.
    ps = payloads(repo, "s1")
    first = run_hook(ps[0])
    out = json.loads(first.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "SessionStart" and "cargo fmt" in out["additionalContext"]
    assert "SystemTime" not in out["additionalContext"]  # path-scoped rules are not pushed at start
    for p in ps[1:]:
        assert run_hook(p).returncode == 0

    con = store.connect()
    store.sync(con)
    assert store.sync(con) == 0  # idempotent
    row = store.sessions(con, "github.com/acme/api")[0]
    assert row["id"] == "s1" and row["steps"] == 11 and row["flags"] == 1 and row["rules_given"] == 2 and row["ended"] == 1
    evs = store.events(con, "s1")
    inferred = [e for e in evs if e.get("event") == "rule_injected" and e.get("inferred")]
    assert len(inferred) == 1 and inferred[0]["matched"] == "crates/auth/src/jwt.rs"

    capsys.readouterr()
    assert cli.main(["last", "--no-input"]) == 0
    text = capsys.readouterr().out
    assert "4-8" in text and "failed 3 times" in text and "E0308" in text

    assert cli.main(["tag", "4-8", "missing", "use the Utc type"]) == 0
    store.sync(con)
    assert store.sessions(con, "github.com/acme/api")[0]["tags"] == 1
    note = [e for e in store.events(con, "s1") if e.get("event") == "annotation"][0]
    assert note["confirms"] == "auto:retry_cluster" and note["tag"] == "missing_knowledge"

    capsys.readouterr()
    assert cli.main(["why"]) == 0
    assert "given at session start" in capsys.readouterr().out
    assert cli.main(["why", "crates/auth/src/jwt.rs"]) == 0
    assert "2 rules would apply" in capsys.readouterr().out

    # Off: nothing injected, native files gone. On: back.
    assert cli.main(["off"]) == 0
    assert run_hook(payloads(repo, "s2")[0]).stdout == ""
    assert not (repo / ".claude/rules/once").exists()
    assert cli.main(["on"]) == 0
    assert (repo / ".claude/rules/once").is_dir()

    # Undo goes back one step at a time: first the tag, then the last taught rule. Uninstall restores settings.
    assert cli.main(["undo"]) == 0
    store.sync(con)
    assert store.sessions(con, "github.com/acme/api")[0]["tags"] == 0
    assert cli.main(["undo"]) == 0
    assert not (repo / ".claude/rules/once").exists()
    assert cli.main(["uninstall"]) == 0 and not any(cc.installed().values())
    assert not (repo / ".git" / "info" / "exclude").read_text().count("CLAUDE.md")


def test_secrets_never_reach_disk(env):
    repo = env["repo"]
    base = {"session_id": "sec", "cwd": str(repo)}
    run_hook({**base, "hook_event_name": "SessionStart"})
    run_hook({**base, "hook_event_name": "UserPromptSubmit",
              "prompt": "deploy with API_KEY=sk-abcdefghijklmnopqrstuvwx and ghp_0123456789abcdefghijABCDEFGHIJ012345"})
    run_hook({**base, "hook_event_name": "PostToolUse", "tool_name": "Bash",
              "tool_input": {"command": "export DB_PASSWORD=hunter2hunter2 && psql postgres://u:p4ssw0rdxyz@h/db"},
              "tool_response": {"stdout": "ok"}})
    blob = "".join(f.read_text() for f in (paths.local() / "trajectories").rglob("*.jsonl"))
    for secret in ("sk-abcdefghijkl", "ghp_0123456789", "hunter2hunter2", "p4ssw0rdxyz"):
        assert secret not in blob
    assert "[REDACTED]" in blob


def test_hook_latency(env):
    run_hook(payloads(env["repo"])[0])
    p = payloads(env["repo"])[2]
    times = []
    for _ in range(7):
        t = time.perf_counter()
        run_hook(p)
        times.append((time.perf_counter() - t) * 1000)
    times.sort()
    print(f"\nhook median {times[3]:.0f} ms, max {times[-1]:.0f} ms")
    assert times[3] < 400  # generous for CI; the design budget is 150 ms
