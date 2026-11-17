import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from conftest import payloads, run_hook
from once import cli, detectors, paths, rules, store, ui
from once.adapters import claude_code as cc


def test_shell_paths(env):
    root = str(env["repo"])
    assert cc.shell_paths('echo "" >> crates/auth/src/jwt.rs && tail -c 5 crates/auth/src/jwt.rs', root) == (["crates/auth/src/jwt.rs"], True)
    assert cc.shell_paths("sed -n 1,5p crates/auth/src/jwt.rs", root) == (["crates/auth/src/jwt.rs"], False)
    assert cc.shell_paths("cargo build --release", root) == ([], False)
    assert cc.shell_paths("cat /etc/hosts 'unterminated", root)[0] == []


def test_path_rule_reaches_shell_edits(env):
    """A file touched through the shell still gets its path-scoped rule, before the command runs."""
    repo = env["repo"]
    cli.main(["init", "--yes"])
    cli.main(["teach", "--path", "crates/auth/**/*.rs", "use chrono DateTime with Utc; never SystemTime"])
    base = {"session_id": "sh1", "cwd": str(repo)}
    run_hook({**base, "hook_event_name": "SessionStart"})
    pre = {**base, "hook_event_name": "PreToolUse", "tool_name": "Bash",
           "tool_input": {"command": "sed -i '' 's/a/b/' crates/auth/src/jwt.rs"}}
    out = json.loads(run_hook(pre).stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and "SystemTime" in out["additionalContext"]
    assert "permissionDecision" not in out  # context only; never touches permissions
    assert run_hook(pre).stdout == ""  # once per session
    assert run_hook({**base, "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}}).stdout == ""
    con = store.connect()
    store.sync(con)
    inj = [e for e in store.events(con, "sh1") if e.get("event") == "rule_injected"]
    assert len(inj) == 1 and inj[0]["channel"] == "pre_tool"


def _mcp(msgs, cwd):
    p = subprocess.run([sys.executable, "-m", "once.mcp_server"], text=True, capture_output=True, cwd=cwd,
                       input="\n".join(json.dumps(m) for m in msgs) + "\n", env=os.environ.copy())
    return [json.loads(l) for l in p.stdout.splitlines() if l.strip()]


def test_mcp_server_and_playbooks(env, capsys):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    cli.main(["playbooks", "new", "add-endpoint", "Use when adding an HTTP endpoint"])
    f = paths.repo_memory("github.com/acme/api") / "playbooks" / "add-endpoint.md"
    f.write_text(f.read_text().replace("1. First step.", "1. Add the route in the router."))
    cli.main(["playbooks", "sync"])
    skill = repo / ".claude/skills/once-add-endpoint/SKILL.md"
    assert "name: once-add-endpoint" in skill.read_text() and "Add the route" in skill.read_text()
    assert ".claude/skills/once-*/" in (repo / ".git/info/exclude").read_text()

    for p in payloads(repo, "m1")[:6]:
        run_hook(p)
    out = _mcp([
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_playbook", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "get_playbook", "arguments": {"name": "add-endpoint"}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "record_feedback", "arguments": {"note": "use Utc", "tag": "missing_knowledge"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "record_blocker", "arguments": {"reason": "cannot reach the registry"}}},
        {"jsonrpc": "2.0", "id": 7, "method": "nope"},
    ], str(repo))
    by = {m["id"]: m for m in out}
    assert len(out) == 7  # the notification gets no reply
    assert by[1]["result"]["protocolVersion"] == "2025-06-18" and by[1]["result"]["capabilities"] == {"tools": {}}
    assert {t["name"] for t in by[2]["result"]["tools"]} == {"get_playbook", "list_rules", "record_blocker", "record_feedback",
                                                                 "review_rule", "remember_rule"}
    assert "add-endpoint" in by[3]["result"]["content"][0]["text"]
    assert "Add the route" in by[4]["result"]["content"][0]["text"]
    assert by[5]["result"]["isError"] is False and by[7]["error"]["code"] == -32601

    con = store.connect()
    store.sync(con)
    evs = store.events(con, "m1")
    note = detectors.annotations(evs)[0]
    assert note["tag"] == "missing_knowledge" and (note["step_from"], note["step_to"]) == (2, 5)  # since the prompt
    assert any(e.get("event") == "blocker" for e in evs)

    cli.main(["off"])
    assert not skill.exists()


def test_replay_with_fake_agent(env, capsys):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    cli.main(["teach", "python is not on PATH; use uv run python"])
    cli.main(["teach", "prefer small functions"])
    agent = f'{sys.executable} {Path(__file__).parent / "fake_agent.py"} {{prompt}}'
    capsys.readouterr()

    rc = cli.main(["lab", "replay", "python-path-uv-run", "--prompt", "run the tests", "--check", "test -f done.txt",
                   "--signature", "command not found", "--agent-cmd", agent, "--yes"])
    text = capsys.readouterr().out
    assert rc == 0 and "failure in 3 of 3" in text and "failure in 0 of 3" in text and "Passes." in text
    ev = rules.find("github.com/acme/api", "python-path-uv-run")["evidence"]["replay"]
    assert ev["verdict"] == "passes" and ev["without_rule"]["failed"] == 3 and ev["with_rule"]["failed"] == 0
    assert ev["steps"]["with"] < ev["steps"]["without"]

    # A rule that does not address the failure must not pass.
    rc = cli.main(["lab", "replay", "prefer-small-functions", "--prompt", "run the tests", "--no-check",
                   "--signature", "command not found", "--agent-cmd", agent, "--runs", "2", "--yes"])
    assert rc == 1 and "No help." in capsys.readouterr().out

    # Replay runs stay out of the real session list, and the real repo is untouched.
    con = store.connect()
    store.sync(con)
    assert store.sessions(con, "github.com/acme/api") == []
    assert not (repo / "done.txt").exists()
    assert subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout == ""

    assert cli.main(["lab", "replay", "python-path-uv-run", "--prompt", "x", "--dry-run"]) == 0


def test_web_viewer(env):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    cli.main(["teach", "always run cargo fmt before finishing"])
    for p in payloads(repo, "w1")[:-1]:  # still running
        run_hook(p)
    srv = ui.serve("github.com/acme/api", lambda s: cli._refresh(s), 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def get(path):
        return urllib.request.urlopen(base + path, timeout=5).read().decode()

    def post(url, **form):
        req = urllib.request.Request(base + url, data=urllib.parse.urlencode(form).encode())
        try:
            return urllib.request.urlopen(req, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        live = get("/live")
        assert "claude-code" in live and "cargo build" in live and "retry cluster" in live
        assert "failed E0308" in get("/live/fragment")
        assert "Fix the timezone bug" in get("/sessions")
        sess = get("/session/w1")
        assert "Steps 4 to 8" in sess and "Save tag" in sess
        assert "run-cargo-fmt-before-finishing" in get("/rules?repo=github.com/acme/api")
        assert "Where agents struggle" in get("/overview") and "crates/auth/src/" in get("/overview")

        assert post("/tag", sid="w1", lo="4", hi="8", tag="avoidable", note="x") == 403  # no token
        assert post("/tag", token=ui.TOKEN, sid="w1", lo="4", hi="8", tag="avoidable", note="use Utc", detector="retry_cluster") == 200
        assert "use Utc" in get("/session/w1")
        assert post("/teach", token=ui.TOKEN, slug="github.com/acme/api", sentence="never commit generated files", path="") == 200
        assert rules.find("github.com/acme/api", "commit-generated-files")
        assert post("/rules/drop", token=ui.TOKEN, slug="github.com/acme/api", id="commit-generated-files") == 200
        assert rules.find("github.com/acme/api", "commit-generated-files")["status"] == "deprecated"

        # Escaping: a hostile prompt cannot inject markup into the page.
        run_hook({"session_id": "x1", "cwd": str(repo), "hook_event_name": "SessionStart"})
        run_hook({"session_id": "x1", "cwd": str(repo), "hook_event_name": "UserPromptSubmit", "prompt": "<script>alert(1)</script>"})
        assert "<script>alert(1)" not in get("/sessions") and "&lt;script&gt;" in get("/sessions")

        req = urllib.request.Request(base + "/sessions", headers={"Host": "evil.example"})
        try:
            urllib.request.urlopen(req, timeout=5)
            assert False
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        srv.shutdown()


def test_dashboard_closes_the_loop(env):
    """0.7.2: expandable steps, a rule made from a flagged step, the session diff, rule impact, setup check."""
    import re as _re
    repo = env["repo"]
    cli.main(["init", "--yes"])
    for p in payloads(repo, "d1"):
        run_hook(p)
    srv = ui.serve("github.com/acme/api", lambda s: cli._refresh(s), 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def get(path):
        return urllib.request.urlopen(base + path, timeout=10).read().decode()
    try:
        page = get("/session/d1")
        assert 'class="stp"' in page and "mismatched types" in page          # full error text, inside the step
        assert "Make this a rule" in page and 'name=signature value="E0308"' in page
        assert 'id="diff"' in page or "No snapshot" in page

        form = {"token": ui.TOKEN, "slug": "github.com/acme/api", "sid": "d1", "signature": "E0308",
                "sentence": "Use chrono Utc types for token expiry", "path": ""}
        req = urllib.request.Request(base + "/teach", data=urllib.parse.urlencode(form).encode())
        assert urllib.request.urlopen(req, timeout=10).status == 200
        r = [x for x in rules.load_all("github.com/acme/api") if x["rule"]["directive"].startswith("Use chrono")][0]
        assert r["provenance"]["signature"] == "E0308" and r["provenance"]["source_sessions"] == ["d1"]

        rp = get(f"/rules?repo=github.com/acme/api&id={r['id']}")
        assert "Did it work?" in rp and _re.search(r"Before.*1 of 1 session", rp, _re.S)

        live = get("/live")
        assert "Setup" in live and "claude-code hooks" in live
    finally:
        srv.shutdown()
