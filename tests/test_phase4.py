import json
import os
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

from conftest import payloads, run_hook
from once import cli, detectors, importer, paths, playbooks, regress, rules, snapshot, store
from once.adapters import antigravity as agy

HERE = Path(__file__).parent
SLUG = "github.com/acme/api"
AGENT = f"{sys.executable} {HERE / 'fake_agent.py'} {{prompt}}"


def agy_hook(event, payload):
    r = subprocess.run([sys.executable, "-m", "once.hook", "antigravity", event], text=True, capture_output=True,
                       input=payload if isinstance(payload, str) else json.dumps(payload), env=os.environ.copy())
    assert r.returncode == 0
    return json.loads(r.stdout)  # agy needs a JSON object from every hook, whatever happens


def test_antigravity_adapter(env, tmp_path, monkeypatch):
    repo = env["repo"]
    hooks = tmp_path / "gemini" / "hooks.json"
    hooks.parent.mkdir()
    hooks.write_text(json.dumps({"someone-else": {"Stop": [{"command": "x"}]}}))
    monkeypatch.setenv("ONCE_AGY_HOOKS", str(hooks))
    agy.install()
    agy.install()
    cfg = json.loads(hooks.read_text())
    assert cfg["someone-else"] == {"Stop": [{"command": "x"}]} and all(agy.installed().values())
    assert cfg["once"]["PostToolUse"][0]["matcher"] == ".*" and "antigravity Stop" in cfg["once"]["Stop"][0]["command"]
    # PreToolUse answers "allow": it hands the call on to the default policy and grants nothing
    assert agy_hook("PreToolUse", {"conversationId": "none", "toolCall": {"name": "run_command", "args": {"CommandLine": "ls"}}}) \
        == {"decision": "allow"}

    cli.main(["init", "--yes"])
    cli.main(["teach", "always run cargo fmt before finishing"])
    cli.main(["teach", "--path", "crates/auth/**/*.rs", "use chrono DateTime with Utc; never SystemTime"])
    base = {"conversationId": "g1", "workspacePaths": [str(repo)], "modelName": "Gemini X", "lastUserInput": "Fix expiry"}
    f = str(repo / "crates/auth/src/jwt.rs")

    assert agy_hook("SessionStart", base) == {}
    first = agy_hook("PreInvocation", {**base, "invocationNum": 1})
    msg = first["injectSteps"][0]["ephemeralMessage"]
    assert "cargo fmt" in msg and "SystemTime" not in msg
    assert agy_hook("PostToolUse", {**base, "toolCall": None}) == {}
    assert agy_hook("PostToolUse", {**base, "toolCall": {"name": "view_file", "args": {"AbsolutePath": f}}, "stepIdx": 1}) == {}
    for _ in range(2):
        agy_hook("PostToolUse", {**base, "toolCall": {"name": "run_command", "args": {"CommandLine": "cargo build"}},
                                 "error": "error[E0308]: mismatched types"})
    second = agy_hook("PreInvocation", {**base, "invocationNum": 2})
    assert "cargo fmt" in second["injectSteps"][0]["ephemeralMessage"]
    # the path rule is a glob-triggered workspace rule Antigravity loads itself, so it is not resent each turn
    assert "SystemTime" not in second["injectSteps"][0]["ephemeralMessage"]
    rule_file = next((repo / ".agents/rules").glob("once-*.md")).read_text()
    assert "trigger: glob" in rule_file and 'globs: "crates/auth/**/*.rs"' in rule_file and "SystemTime" in rule_file
    assert ".agents/rules/once-*.md" in (repo / ".git/info/exclude").read_text()
    assert agy_hook("Stop", {**base, "executionNum": 1, "fullyIdle": True}) == {"decision": "allow"}
    assert agy_hook("Stop", "not json") == {"decision": "allow"} and agy_hook("PostToolUse", "") == {}
    monkeypatch.setenv("ONCE_DISABLE", "1")
    assert agy_hook("PreInvocation", base) == {}
    monkeypatch.delenv("ONCE_DISABLE")

    con = store.connect()
    store.sync(con)
    row = store.sessions(con, SLUG)[0]
    evs = store.events(con, "g1")
    assert row["agent"] == "antigravity" and row["model"] == "Gemini X" and row["rules_given"] == 2
    assert [e["prompt"] for e in evs if e["event"] == "user_prompt"] == ["Fix expiry"]  # repeated input recorded once
    assert [s["tool"] for s in detectors.steps(evs)[1:]] == ["read", "shell", "shell"]
    assert [fl["detector"] for fl in detectors.detect(evs)] == ["retry_cluster"]
    assert {e["channel"] for e in evs if e["event"] == "rule_injected"} == {"each_turn", "native"}
    agy.uninstall()
    assert json.loads(hooks.read_text()) == {"someone-else": {"Stop": [{"command": "x"}]}}


def test_snapshots_leave_the_index_alone(env):
    repo = env["repo"]
    (repo / "staged.txt").write_text("s")
    subprocess.run(["git", "-C", str(repo), "add", "staged.txt"], check=True)
    before = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert snapshot.take(str(repo), "s1", "start")
    (repo / "crates/auth/src/jwt.rs").write_text("fn main() { /* changed */ }\n")
    (repo / "new.txt").write_text("n")
    assert snapshot.take(str(repo), "s1", "end")
    assert snapshot.changed(str(repo), "s1") == ["crates/auth/src/jwt.rs", "new.txt"]
    after = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert before.splitlines() == ["A  staged.txt"]
    assert sorted(after.splitlines()) == sorted([" M crates/auth/src/jwt.rs", "A  staged.txt", "?? new.txt"])  # staging untouched


def test_import_transcripts(env, tmp_path, monkeypatch, capsys):
    repo = env["repo"]
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    d = tmp_path / "cfg" / "projects" / "proj"
    d.mkdir(parents=True)
    common = {"cwd": str(repo), "sessionId": "old1", "timestamp": "2026-10-01T10:00:00Z"}
    lines = [
        {**common, "type": "user", "message": {"role": "user", "content": "Fix the build with token=sk-abcdefghijklmnopqrstuvwx"}},
        {**common, "type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "cargo build"}}]}},
        {**common, "type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "error[E0308]: mismatched types"}]}},
        {**common, "type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "cargo build"}}]}},
        {**common, "type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "is_error": True, "content": [{"type": "text", "text": "error[E0308]: mismatched types"}]}]}},
        {**common, "type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t3", "name": "Read", "input": {"file_path": str(repo / "crates/auth/src/jwt.rs")}}]}},
        {**common, "type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t3", "content": "fn main() {}"}]}},
    ]
    (d / "old1.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\nnot json\n")
    assert cli.main(["import"]) == 0 and "1 imported" in capsys.readouterr().out
    con = store.connect()
    row = store.sessions(con, SLUG)[0]
    evs = store.events(con, "old1")
    assert row["steps"] == 4 and row["flags"] == 1 and evs[0]["imported"] is True
    assert "sk-abcdefghijkl" not in json.dumps(evs)
    assert cli.main(["import"]) == 0 and "1 already captured" in capsys.readouterr().out


def test_ignored_rule_detector(env, capsys):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    cli.main(["teach", "never use SystemTime in this repo"])
    rid = rules.active(SLUG)[0]["id"]
    assert cli.main(["rules", "detect", rid, r"SystemTime::now\("]) == 0
    assert rules.find(SLUG, rid)["enforcement"]["detector"]["backtest"]["files_matching_in_tree"] == 0
    assert cli.main(["rules", "detect", rid, "(unclosed"]) == 2
    f = str(repo / "crates/auth/src/jwt.rs")
    b = {"session_id": "ig1", "cwd": str(repo)}
    run_hook({**b, "hook_event_name": "SessionStart"})
    run_hook({**b, "hook_event_name": "UserPromptSubmit", "prompt": "add expiry"})
    run_hook({**b, "hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_response": {},
              "tool_input": {"file_path": f, "old_string": "x", "new_string": "let exp = Utc::now();"}})
    run_hook({**b, "hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_response": {},
              "tool_input": {"file_path": f, "old_string": "y", "new_string": "let exp = SystemTime::now() + d;"}})
    con = store.connect()
    store.sync(con)
    fl = detectors.detect(store.events(con, "ig1"))
    assert [(x["detector"], x["step_from"], x["rule_id"]) for x in fl] == [("ignored_rule", 3, rid)]
    capsys.readouterr()
    cli.main(["last", "--no-input"])
    assert f"went against rule {rid}" in capsys.readouterr().out


def _rule_with_replay(directive, rid):
    cli.main(["teach", directive])
    capsys_out = cli.main(["lab", "replay", rid, "--prompt", "run the tests", "--no-check", "--signature", "command not found",
                           "--agent-cmd", AGENT, "--runs", "2", "--yes"])
    return capsys_out


def test_regression_suite_and_transfer(env, capsys):
    cli.main(["init", "--yes"])
    _rule_with_replay("python is not on PATH; use uv run python", "python-path-uv-run")
    r = rules.find(SLUG, "python-path-uv-run")
    assert r["evidence"]["replay"]["verdict"] == "passes" and r["evidence"]["replay"]["prompt"] == "run the tests"
    assert regress.classify(3, 0, 3) == "needed" and regress.classify(0, 0, 3) == "obsolete"
    assert regress.classify(3, 3, 3) == "broken" and regress.classify(0, 3, 3) == "harmful"
    capsys.readouterr()

    assert cli.main(["lab", "regress", "--agent-cmd", AGENT, "--runs", "2", "--yes"]) == 0
    assert "1 needed" in capsys.readouterr().out
    # Another agent: the same rule, the other adapter's name. It still fixes the failure, so it transfers.
    assert cli.main(["lab", "regress", "--agent", "antigravity", "--agent-cmd", AGENT, "--runs", "2", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Transfer to antigravity: 1 of 1 rules" in out
    hist = rules.find(SLUG, "python-path-uv-run")["evidence"]["regress"]
    assert [(h["agent"], h["state"]) for h in hist] == [("claude-code", "needed"), ("antigravity", "needed")]

    # A model that no longer makes the mistake: the rule turns obsolete, and is retired only the second time.
    solved = f"{sys.executable} {HERE / 'dead_agent.py'} {{prompt}}"
    always = f"{sys.executable} -c \"import sys; sys.argv=['x', sys.argv[1]]; exec(open(r'{HERE / 'fake_agent.py'}').read().replace('knows = ', 'knows = True or '))\" {{prompt}}"
    for expect_status in ("active", "deprecated"):
        cli.main(["lab", "regress", "--agent-cmd", always, "--runs", "2", "--apply", "--yes"])
        assert rules.find(SLUG, "python-path-uv-run")["status"] == expect_status
    assert "retire" in capsys.readouterr().out
    assert not solved == always


def test_verify_candidate_config_export_and_draft_playbooks(env, capsys):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    cand = rules.new_manual("python is not on PATH; use uv run python", [], "t")
    cand.update({"status": "candidate"})
    cand["provenance"]["replay_hint"] = {"prompt": "run the tests", "commit": head, "signature": "command not found"}
    rules.save(SLUG, cand)
    a = Namespace(agent=None, agent_cmd=AGENT, model=None, budget=0.5)
    assert cli._verify_candidate(SLUG, repo, rules.find(SLUG, cand["id"]), a).startswith("Passes.")
    assert rules.find(SLUG, cand["id"])["evidence"]["replay"]["verdict"] == "passes"
    cand2 = rules.new_manual("prefer small functions", [], "t")
    cand2["status"] = "candidate"
    rules.save(SLUG, cand2)
    assert "no replayable session" in cli._verify_candidate(SLUG, repo, rules.find(SLUG, cand2["id"]), a)

    capsys.readouterr()
    assert cli.main(["config", "auto_learn", "true"]) == 0 and cli.main(["config", "daily_cost_cap", "0.5"]) == 0
    assert json.loads((paths.home() / "config.json").read_text()) == {"auto_learn": True, "daily_cost_cap": 0.5}
    assert cli.main(["config", "nope", "1"]) == 2
    cli.main(["config", "auto_learn", "false"])

    cli.main(["review", "--accept", cand["id"]])
    cli.main(["teach", "--path", "crates/auth/**/*.rs", "use chrono Utc types"])
    capsys.readouterr()
    assert cli.main(["export"]) == 0
    md = capsys.readouterr().out
    assert "## Repository conventions" in md and "replayed 3x: failed 3 without, 0 with" in md and "crates/auth/**/*.rs" in md
    assert "sess" not in md and str(paths.home()) not in md
    assert cli.main(["export", "--format", "json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["rules"]) == 2

    for i in range(6):  # two files that always change together
        (repo / "a.py").write_text(f"a{i}")
        (repo / "b.py").write_text(f"b{i}")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"c{i}"], check=True)
    assert cli.main(["lab", "cochange", "--write"]) == 0
    pbs = playbooks.load_all(SLUG)
    assert pbs and all(p["draft"] for p in pbs) and "b.py" in pbs[0]["body"] + pbs[-1]["body"]
    cli.main(["playbooks", "sync"])
    assert not (repo / ".claude" / "skills").exists()  # drafts are never published
