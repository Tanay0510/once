"""0.8: enforcement, MCP, rule files and skills for Codex and Antigravity; the team's central collector.
Hook replies follow each agent's hooks reference; not yet checked against a live codex or agy."""
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request

from conftest import payloads, run_hook
from once import cli, collector, paths, playbooks, rules, store
from once.adapters import antigravity as agy

SLUG = "github.com/acme/api"


def cx(payload):
    r = run_hook(payload, "codex")
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout) if r.stdout.strip().startswith("{") else r.stdout


def ag(event, payload):
    r = subprocess.run([sys.executable, "-m", "once.hook", "antigravity", event], text=True, capture_output=True,
                       input=json.dumps(payload), env=os.environ.copy())
    assert r.returncode == 0
    return json.loads(r.stdout)


def _enforced_rule(level):
    cli.main(["teach", "--path", "crates/auth/**", "never use SystemTime in the auth crate"])
    rid = rules.active(SLUG)[0]["id"]
    cli.main(["rules", "detect", rid, r"SystemTime::now"])
    cli.main(["rules", "promote", rid, level, "--force"])
    return rid


PATCH = ("*** Begin Patch\n*** Update File: crates/auth/src/jwt.rs\n@@\n-let t = Utc::now();\n"
         "+let t = SystemTime::now();\n*** End Patch\n")


def test_codex_check_and_block(env):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    _enforced_rule("check")
    base = {"session_id": "cxe", "cwd": str(repo), "transcript_path": None}
    cx({**base, "hook_event_name": "SessionStart", "source": "startup"})
    out = cx({**base, "hook_event_name": "PostToolUse", "tool_name": "apply_patch", "tool_input": {"command": PATCH},
              "tool_response": "Success"})
    assert out["decision"] == "block" and "SystemTime" in out["reason"]
    # The patch is recorded as an edit of the file it names, not as a command.
    con = store.connect()
    store.sync(con)
    ev = [e for e in store.events(con, "cxe") if e.get("event") == "tool_result"][-1]
    assert ev["tool"] == "edit" and ev["target"] == "crates/auth/src/jwt.rs"
    assert any(e.get("event") == "rule_enforced" for e in store.events(con, "cxe"))
    # A patch that only removes the forbidden form is fine.
    clean = PATCH.replace("-let t = Utc::now();\n+let t = SystemTime::now();", "-let t = SystemTime::now();\n+let t = Utc::now();")
    assert cx({**base, "hook_event_name": "PostToolUse", "tool_name": "apply_patch", "tool_input": {"command": clean},
               "tool_response": "Success"}) == ""

    rid = rules.active(SLUG)[0]["id"]
    cli.main(["rules", "promote", rid, "block", "--force"])
    deny = cx({**base, "hook_event_name": "PreToolUse", "tool_name": "apply_patch", "tool_input": {"command": PATCH}})
    assert deny["hookSpecificOutput"]["permissionDecision"] == "deny"
    ok = cx({**base, "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert ok == "" or "permissionDecision" not in ok.get("hookSpecificOutput", {})


def test_antigravity_block_deferred_check_mcp_rules_and_skills(env, tmp_path, monkeypatch):
    repo = env["repo"]
    hooks = tmp_path / "gemini" / "config" / "hooks.json"
    monkeypatch.setenv("ONCE_AGY_HOOKS", str(hooks))
    (hooks.parent).mkdir(parents=True)
    (hooks.parent / "mcp_config.json").write_text(json.dumps({"mcpServers": {"theirs": {"command": "x"}}}))
    agy.install()
    cli.main(["init", "--yes"])
    assert agy.register_mcp()[0] and agy.mcp_registered()
    mcp = json.loads((hooks.parent / "mcp_config.json").read_text())["mcpServers"]
    assert mcp["theirs"] == {"command": "x"} and mcp["once"]["args"][-1] == "once.mcp_server"

    cli.main(["playbooks", "new", "add-endpoint", "Use when adding an HTTP endpoint"])
    cli.main(["playbooks", "sync"])
    skill = repo / ".agents/skills/once-add-endpoint/SKILL.md"
    assert skill.exists() and "description:" in skill.read_text()
    assert ".agents/skills/once-*/" in (repo / ".git/info/exclude").read_text()

    rid = _enforced_rule("check")
    assert not list((repo / ".agents/rules").glob("once-*.md"))  # enforced rules are not given as context
    base = {"conversationId": "ge", "workspacePaths": [str(repo)], "lastUserInput": "fix expiry"}
    ag("SessionStart", base)
    ag("PreInvocation", base)
    edit = {**base, "toolCall": {"name": "replace_file_content",
                                 "args": {"TargetFile": str(repo / "crates/auth/src/jwt.rs"),
                                          "ReplacementContent": "let t = SystemTime::now();"}}}
    assert ag("PreToolUse", edit) == {"decision": "allow"}   # check level: not refused, checked after
    assert ag("PostToolUse", edit) == {}                     # agy's PostToolUse cannot speak to the model
    nxt = ag("PreInvocation", {**base, "lastUserInput": "go on"})
    msg = nxt["injectSteps"][0]["ephemeralMessage"]
    assert "goes against a convention" in msg and "SystemTime" in msg
    again = ag("PreInvocation", {**base, "lastUserInput": "and now"})  # delivered once
    assert "goes against a convention" not in json.dumps(again)

    cli.main(["rules", "promote", rid, "block", "--force"])
    deny = ag("PreToolUse", edit)
    assert deny["decision"] == "deny" and "SystemTime" in deny["reason"]

    cli.main(["uninstall"])
    assert not agy.mcp_registered() and json.loads((hooks.parent / "mcp_config.json").read_text())["mcpServers"] == \
        {"theirs": {"command": "x"}}
    assert not skill.exists()


def test_collector_end_to_end_and_private(env, tmp_path, capsys):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    cli.main(["teach", "always run cargo fmt before finishing"])
    for p in payloads(repo, "k1"):
        run_hook(p)
    srv = collector.serve(tmp_path / "coll", token="team-secret")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert cli.main(["collector", "connect", url, "--token", "wrong"]) == 1
        assert cli.main(["collector", "connect", url, "--token", "team-secret", "--name", "ana"]) == 0
        body = collector.payload()
        wire = json.dumps(body)
        # Never: prompts, commands, error text, file paths below the folder.
        for secret in ("Fix the timezone bug", "cargo build", "mismatched types", "jwt.rs"):
            assert secret not in wire, secret
        s = body["sessions"][0]
        assert s["flags"] == {"retry_cluster": 1} and s["folders"] == ["crates/auth/src/"] and s["sid"] != "k1"
        capsys.readouterr()
        assert cli.main(["collector", "push"]) == 0 and "Sent 1 sessions" in capsys.readouterr().out
        assert collector.payload()["sessions"] == []  # nothing new to send
        assert cli.main(["collector", "push"]) == 0
        sm = collector._call("GET", "/v1/summary")
        assert sm["sessions"] == 1 and sm["developers"]["ana"]["sessions"] == 1 and sm["agents"]["claude-code"]["flags"] == 1
        assert sm["hotspots"][0]["folder"] == "crates/auth/src/"
        assert any(r["id"] == "run-cargo-fmt-before-finishing" for r in sm["rules"])
        page = urllib.request.urlopen(f"{url}/?token=team-secret", timeout=5).read().decode()
        assert "ana" in page and "crates/auth/src/" in page
        try:
            urllib.request.urlopen(f"{url}/v1/summary", timeout=5)
            assert False
        except urllib.error.HTTPError as e:
            assert e.code == 401
        assert json.loads(urllib.request.urlopen(f"{url}/healthz", timeout=5).read())["ok"]
        capsys.readouterr()
        assert cli.main(["collector", "team"]) == 0 and "ana" in capsys.readouterr().out
        assert cli.main(["collector", "disconnect"]) == 0 and not collector.configured()
    finally:
        srv.shutdown()
