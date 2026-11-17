"""Regression tests for what the 0.8 test run found on a real Mac with live Claude Code and Antigravity sessions."""
import json
import os
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

from conftest import payloads, run_hook
from once import cli, paths, redact, replay, store
from once.adapters import antigravity as agy

SLUG = "github.com/acme/api"


def test_paths_survive_masking_and_secrets_do_not():
    keep = ["/private/tmp/claude-501/-Users-v-Desktop-once/d5727de2-4efa-44db-a966-2589012f9b9e/scratchpad/live/a1",
            "/var/folders/yj/hjs7w5dn43zbw97w6jlbvx800000gn/T/pytest-of-v/test_x0/proj",
            "/Users/v/.claude/projects/-Users-v-Desktop-once/f0a2e97d-bcda-4660-8e9e-04422d368151.jsonl",
            "/Users/v/Library/Application Support/Code/User/workspaceStorage/9f8e7d6c5b4a39281706f5e4d3c2b1a0/x"]
    for p in keep:
        assert redact.redact_text(p) == (p, 0), p
    for s in ("aws_secret wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY99", "/wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY99Zq",
              "x=/a9Fk2LmQ8pZ/Rt7Xw1Vb4Nc6Yd3Hs5Je0Kg9Mf2Pq", "ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"):
        assert redact.redact_text(s)[1] > 0, s
    out = redact.redact_text("cd /home/u/app/Xk9fQ2mZp7Lw3Rt8Yv1Bn6Cs4Hd5Jg0aEe/run")[0]
    assert out == "cd /home/u/app/[REDACTED]/run"  # only the secret segment goes


def test_session_cwd_is_kept(env):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    for p in payloads(repo, "cw1"):
        run_hook(p)
    con = store.connect()
    store.sync(con)
    assert store.sessions(con, SLUG)[0]["cwd"] == str(repo)


def test_one_malformed_payload_cannot_break_the_cli(env, capsys):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    run_hook({"session_id": {"x": 1}, "cwd": str(repo), "hook_event_name": "PostToolUse"})
    run_hook({"session_id": ["a"], "cwd": 5, "hook_event_name": {"no": 1}})
    for p in payloads(repo, "ok1"):
        run_hook(p)
    capsys.readouterr()
    assert cli.main(["last", "--no-input"]) == 0 and "Fix the timezone bug" in capsys.readouterr().out
    assert cli.main(["status"]) == 0


def test_a_corrupt_database_is_rebuilt(env, capsys):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    for p in payloads(repo, "db1"):
        run_hook(p)
    store.sync()
    paths.db().write_bytes(os.urandom(4096))
    for suffix in ("-wal", "-shm"):
        f = paths.db().with_name(paths.db().name + suffix)
        if f.exists():
            f.unlink()
    capsys.readouterr()
    assert cli.main(["last", "--no-input"]) == 0 and "Fix the timezone bug" in capsys.readouterr().out
    assert list(paths.local().glob("state.db.corrupt-*"))


def test_cli_errors_are_one_line_not_a_traceback(env, capsys, monkeypatch):
    cli.main(["init", "--yes"])
    monkeypatch.setattr(cli, "cmd_rules", lambda a: (_ for _ in ()).throw(RuntimeError("boom")))
    capsys.readouterr()
    assert cli.main(["rules"]) == 1
    err = capsys.readouterr().err
    assert "RuntimeError: boom" in err and "Traceback" not in err and (paths.local() / "cli-errors.log").exists()
    monkeypatch.setattr(cli, "cmd_rules", lambda a: (_ for _ in ()).throw(PermissionError(13, "denied", "/x/y")))
    assert cli.main(["rules"]) == 1 and "permission denied" in capsys.readouterr().err


def test_every_command_answers_json(env, capsys):
    cli.main(["init", "--yes"])
    for args in (["teach", "--dry-run", "x y z"], ["config"], ["off", "--dry-run"], ["purge", "--dry-run"],
                 ["collector", "status"], ["lab", "exp", "report"], ["uninstall", "--dry-run"]):
        capsys.readouterr()
        rc = cli.main(args + ["--json"])
        out = json.loads(capsys.readouterr().out)  # parses: that is the promise
        if isinstance(out, dict) and "message" in out:  # prose, wrapped with its outcome
            assert out["exit"] == rc and out["ok"] == (rc == 0) and out["message"], args


def test_tag_notes_and_chat_corrections_are_masked(env):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    for p in payloads(repo, "m1"):
        run_hook(p)
    key = "sk-ant-api03-Zq8xW2vB7nM4kL9pR3tY6uI1oP5aS0dF"
    cli.main(["tag", "4-8", "missing", f"never paste {key}"])
    base = {"session_id": "m2", "cwd": str(repo)}
    run_hook({**base, "hook_event_name": "SessionStart"})
    run_hook({**base, "hook_event_name": "UserPromptSubmit", "prompt": f"no, never hardcode keys like {key}, read them from the env"})
    import time
    time.sleep(2)
    for root, _, files in os.walk(paths.home()):
        for f in files:
            assert key.encode() not in open(os.path.join(root, f), "rb").read(), f


def test_enforcement_shows_in_last(env, capsys):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    cli.main(["teach", "--path", "crates/auth/**", "never use SystemTime in the auth crate"])
    rid = cli.rules.active(SLUG)[0]["id"]
    cli.main(["rules", "detect", rid, r"SystemTime::now"])
    cli.main(["rules", "promote", rid, "block", "--force"])
    run_hook({"session_id": "e1", "cwd": str(repo), "hook_event_name": "SessionStart"})
    run_hook({"session_id": "e1", "cwd": str(repo), "hook_event_name": "UserPromptSubmit", "prompt": "go"})
    run_hook({"session_id": "e1", "cwd": str(repo), "hook_event_name": "PreToolUse", "tool_name": "Write",
              "tool_input": {"file_path": str(repo / "crates/auth/src/jwt.rs"), "content": "SystemTime::now()"}})
    capsys.readouterr()
    cli.main(["last", "--no-input"])
    assert f"Enforced: {rid} (block)" in capsys.readouterr().out


def test_model_comes_from_usage_when_missing_at_start(env, tmp_path):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"message": {"model": "claude-haiku-5-5", "usage": {"input_tokens": 3, "output_tokens": 2}}}) + "\n")
    base = {"session_id": "mo1", "cwd": str(repo), "transcript_path": str(t)}
    run_hook({**base, "hook_event_name": "SessionStart"})
    run_hook({**base, "hook_event_name": "SessionEnd", "reason": "exit"})
    con = store.connect()
    store.sync(con)
    assert store.sessions(con, SLUG)[0]["model"] == "claude-haiku-5-5"


def test_antigravity_real_tool_names_and_exit_codes_from_the_transcript(tmp_path):
    t = tmp_path / "transcript_full.jsonl"
    t.write_text("\n".join(json.dumps(x) for x in [
        {"step_index": 2, "type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command", "args": {"CommandLine": "pytest"}}]},
        {"step_index": 3, "type": "GENERIC", "content": "Created At: x\nCommand exited with code 3.\n\nCommand output:\nboom\r\n"},
        {"step_index": 6, "type": "GENERIC", "content": "Command exited with code 0.\n\nCommand output:\nok"}]) + "\n")
    assert agy.command_result(str(t), 3) == (3, "boom") and agy.command_result(str(t), 6)[0] == 0
    raw = agy.normalize({"conversationId": "c", "workspacePaths": [str(tmp_path)], "transcriptPath": str(t), "stepIdx": 3,
                         "toolCall": {"name": "run_command", "args": {"CommandLine": "pytest"}}, "error": ""}, "PostToolUse")
    ev = agy.parse(raw, str(tmp_path))[0]
    assert ev["ok"] is False and ev["error_head"] == "exit code 3" and "boom" in ev["error"]
    for name, kind in (("read_file", "read"), ("edit_file", "edit"), ("write_file", "edit")):
        raw = agy.normalize({"conversationId": "c", "workspacePaths": [str(tmp_path)],
                             "toolCall": {"name": name, "args": {"Path": str(tmp_path / "a.py")}}}, "PostToolUse")
        assert agy.parse(raw, str(tmp_path))[0]["tool"] == kind
    assert "print(" in agy.edit_text({"tool_input": {"Path": "a.py", "Target": "x", "Replacement": "print(1)"}})


def test_a_refused_headless_run_is_an_error_not_a_measurement(env, tmp_path):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    agent = tmp_path / "agent.py"
    agent.write_text("import json\nprint(json.dumps({'status': 'SUCCESS', 'denied_actions': "
                     "[{'action': 'command', 'display_name': 'Bash'}]}))\n")
    cli.main(["teach", "always run the linter"])
    rule = cli.rules.active(SLUG)[0]
    res = replay.replay(SLUG, repo, rule, "run the tests", "HEAD", 1, None, None, f"{sys.executable} {agent} {{prompt}}",
                        0.1, progress=lambda *_: None, save=False)
    assert res["verdict"] == "error" and "permissions.allow" in res["error"]


def test_connected_does_not_require_the_enforcement_group(env, capsys):
    cli.main(["init", "--yes"])
    s = json.loads(env["settings"].read_text())
    s["hooks"]["PostToolUse"] = [g for g in s["hooks"]["PostToolUse"]
                                 if not g["hooks"][0]["command"].endswith(" enforce")]
    env["settings"].write_text(json.dumps(s))
    capsys.readouterr()
    cli.main(["status"])
    assert "claude-code" in capsys.readouterr().out.splitlines()[0]
