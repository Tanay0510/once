import json
import os
import subprocess
import sys

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    """An isolated home, a fake Claude settings file and a small git repo to stand in."""
    home = tmp_path / "home"
    settings = tmp_path / "claude" / "settings.json"
    repo = tmp_path / "proj"
    (repo / "crates" / "auth" / "src").mkdir(parents=True)
    (repo / "crates" / "auth" / "src" / "jwt.rs").write_text("fn main() {}\n")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text("jobs:\n  t:\n    steps:\n      - run: cargo test\n")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "git@github.com:Acme/API.git"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"], check=True)
    monkeypatch.setenv("ONCE_HOME", str(home))
    monkeypatch.setenv("ONCE_OLD_HOME", str(tmp_path / "no-old-install"))
    monkeypatch.setenv("ONCE_CLAUDE_SETTINGS", str(settings))
    monkeypatch.delenv("ONCE_DISABLE", raising=False)
    monkeypatch.chdir(repo)
    return {"home": home, "settings": settings, "repo": repo}


def run_hook(payload, agent="claude-code"):
    """Run the hook the way an agent does: a subprocess with the payload on stdin."""
    data = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run([sys.executable, "-m", "once.hook", agent], input=data, text=True,
                          capture_output=True, env=os.environ.copy())


def payloads(repo, sid="s1"):
    base = {"session_id": sid, "cwd": str(repo), "transcript_path": "/tmp/t.jsonl", "permission_mode": "default"}
    f = str(repo / "crates/auth/src/jwt.rs")
    fail = {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "tool_input": {"command": "cargo build"},
            "error": "error[E0308]: mismatched types\n --> crates/auth/src/jwt.rs:12:5"}
    seq = [
        {"hook_event_name": "SessionStart", "source": "startup", "model": "claude-test"},
        {"hook_event_name": "UserPromptSubmit", "prompt": "Fix the timezone bug in token expiry"},
        {"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_input": {"file_path": f}, "tool_response": {"content": "x"}},
        {"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {"file_path": f}, "tool_response": {"ok": True}},
        fail,
        {"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {"file_path": f}, "tool_response": {}},
        {**fail, "error": "error[E0308]: mismatched types\n --> crates/auth/src/jwt.rs:14:9"},
        {"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {"file_path": f}, "tool_response": {}},
        {**fail, "error": "error[E0308]: mismatched types\n --> crates/auth/src/jwt.rs:15:9"},
        {"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {"file_path": f}, "tool_response": {}},
        {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "cargo build"}, "tool_response": {"stdout": "ok"}},
        {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "cargo test"}, "tool_response": {"stdout": "ok"}},
        {"hook_event_name": "SessionEnd", "reason": "exit"},
    ]
    return [{**base, **p} for p in seq]
