"""Codex adapter: install, a session through the real hook, failure and patch reading, headless output.
Payloads follow the Codex hooks reference; not yet checked against a live codex."""
import json
from conftest import run_hook
from once import cli, detectors, index, paths
from once.adapters import codex as cx

SLUG = "github.com/acme/api"


def cx_hook(payload):
    r = run_hook(payload, "codex")
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_install_merges_and_is_idempotent(env, tmp_path, monkeypatch):
    hooks = tmp_path / "codex" / "hooks.json"
    hooks.parent.mkdir()
    mine = {"type": "command", "command": "~/bin/audit.sh"}
    hooks.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "^Bash$", "hooks": [mine]}]}}))
    monkeypatch.setenv("ONCE_CODEX_HOOKS", str(hooks))
    cx.install()
    cx.install()
    cfg = json.loads(hooks.read_text())
    assert all(cx.installed().values())
    pre = cfg["hooks"]["PreToolUse"]
    assert len(pre) == 2 and pre[0]["hooks"] == [mine] and pre[1]["matcher"] == "^(Bash|apply_patch|Edit|Write)$"
    assert all(len(cfg["hooks"][e]) == 1 for e in cx.EVENTS if e != "PreToolUse")
    # the command must not change between installs, or Codex asks the developer to trust it again
    cmds = {h["command"] for e in cx.EVENTS for g in cfg["hooks"][e] for h in g["hooks"] if "once.hook" in h["command"]}
    assert cmds == {cx.hook_command()} and cmds.pop().endswith("-m once.hook codex")
    assert (hooks.parent / "hooks.json.once.bak").exists()
    assert set(cx.uninstall()) == set(cx.EVENTS)
    assert json.loads(hooks.read_text()) == {"hooks": {"PreToolUse": [{"matcher": "^Bash$", "hooks": [mine]}]}}


def test_codex_session_through_the_hook(env, tmp_path):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    cli.main(["teach", "always run cargo fmt before finishing"])
    cli.main(["teach", "--path", "crates/auth/**", "use chrono for token expiry"])
    base = {"session_id": "cx1", "cwd": str(repo), "model": "gpt-5-codex", "transcript_path": None,
            "permission_mode": "default"}

    start = cx_hook({**base, "hook_event_name": "SessionStart", "source": "startup"})
    assert start.startswith('<repo-conventions source="once">') and "cargo fmt" in start and "chrono" not in start
    assert cx_hook({**base, "hook_event_name": "UserPromptSubmit", "turn_id": "t1",
                    "prompt": "fix the token expiry bug"}) == ""

    jwt = str(repo / "crates/auth/src/jwt.rs")
    pre = json.loads(cx_hook({**base, "hook_event_name": "PreToolUse", "turn_id": "t1", "tool_name": "Bash",
                              "tool_input": {"command": f"sed -n 1,40p {jwt}"}}))
    assert pre["hookSpecificOutput"]["hookEventName"] == "PreToolUse" and "chrono" in pre["hookSpecificOutput"]["additionalContext"]
    assert "permissionDecision" not in pre["hookSpecificOutput"]  # never decides whether a tool may run

    for i in range(2):
        cx_hook({**base, "hook_event_name": "PostToolUse", "turn_id": "t1", "tool_name": "Bash",
                 "tool_input": {"command": ["bash", "-lc", "cargo build"]},
                 "tool_response": "Exit code: 101\nWall time: 3.1 seconds\nOutput:\nerror[E0308]: mismatched types"})
    cx_hook({**base, "hook_event_name": "PostToolUse", "turn_id": "t1", "tool_name": "apply_patch",
             "tool_input": {"input": "*** Begin Patch\n*** Update File: crates/auth/src/jwt.rs\n@@\n-a\n+b\n*** End Patch"},
             "tool_response": {"success": True}})
    cx_hook({**base, "hook_event_name": "PostToolUse", "turn_id": "t1", "tool_name": "Bash",
             "tool_input": {"command": "cargo build"}, "tool_response": {"exit_code": 0, "output": "ok"}})
    assert cx_hook({**base, "hook_event_name": "Stop", "turn_id": "t1", "stop_hook_active": False,
                    "last_assistant_message": "done"}) == ""
    cx_hook({**base, "hook_event_name": "SessionEnd", "reason": "exit"})

    evs = [json.loads(l) for l in (paths.trajectories(SLUG) / "cx1.jsonl").read_text().splitlines()]
    assert {e["agent"] for e in evs} == {"codex"}
    tools = [e for e in evs if e["event"] == "tool_result"]
    assert [t["ok"] for t in tools] == [False, False, True, True]
    assert tools[0]["command"] == "cargo build" and tools[0]["error_head"] == "E0308"
    assert tools[2]["tool"] == "edit" and tools[2]["paths"] == ["crates/auth/src/jwt.rs"]
    flags = detectors.detect([{**e, "seq": i} for i, e in enumerate(evs)])
    assert any(f["detector"] == "retry_cluster" for f in flags)
    given = [r for e in evs if e["event"] == "rule_injected" for r in e["rule_ids"]]
    assert len(given) == 2 and len(set(given)) == 2

    # a late start (hooks trusted mid-session) still records, and a bad payload never fails
    assert cx_hook({"session_id": "cx2", "cwd": str(repo), "hook_event_name": "PostToolUse", "tool_name": "Bash",
                    "tool_input": {"command": "ls"}, "tool_response": "Exit code: 0\nOutput:\n"}) == ""
    assert cx_hook("not json") == "" and cx_hook({}) == ""


def test_failure_reading():
    assert cx._failed("Exit code: 1\nOutput:\nboom")[0]
    assert not cx._failed("Exit code: 0\nOutput:\nfine")[0]
    assert cx._failed('{"exit_code": 2, "output": "x"}') == (True, "x")
    assert cx._failed({"metadata": {"exit_code": 127}, "output": "not found"}) == (True, "not found")
    assert not cx._failed({"success": True})[0] and not cx._failed(None)[0]


def test_headless_output():
    ok = "\n".join(json.dumps(e) for e in [{"type": "thread.started"}, {"type": "turn.completed",
                                           "usage": {"input_tokens": 10, "output_tokens": 5}}])
    assert cx.parse_output(ok) == (None, None)
    bad = "\n".join(json.dumps(e) for e in [{"type": "thread.started"},
                                            {"type": "turn.failed", "error": {"message": "usage limit reached"}}])
    assert cx.parse_output(bad) == (None, "usage limit reached")
    assert cx.parse_output('{"type": "error", "message": "not logged in"}')[1] == "not logged in"
    assert cx.HEADLESS.startswith("codex exec --json")


def test_init_sets_up_codex_and_tells_you_to_trust(env, tmp_path, monkeypatch, capsys):
    hooks = tmp_path / "codex" / "hooks.json"
    monkeypatch.setenv("ONCE_CODEX_HOOKS", str(hooks))
    monkeypatch.setattr(cli, "_agents_found", lambda: {"claude-code": "2.1", "codex": "codex-cli 0.130.0"})
    monkeypatch.setattr(cx, "version", lambda: (0, 130, 0))
    assert cli.main(["init", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "for codex" in out and "/hooks" in out and "0.124" not in out
    assert all(cx.installed().values())
    cli.main(["doctor"])
    assert "codex capture" in capsys.readouterr().out

    monkeypatch.setattr(cx, "version", lambda: (0, 110, 0))
    cli.main(["init", "--yes"])
    assert "0.124 or newer" in capsys.readouterr().out
    cli.main(["uninstall"])
    assert not any(cx.installed().values())
