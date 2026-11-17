"""Stands in for a headless coding agent in replay tests.

It talks to the real hook exactly as an agent would. If the session-start context contains the
rule, it gets the task right first time; otherwise it fails the same way twice before succeeding.
"""
import json
import os
import subprocess
import sys
import uuid

cwd = os.getcwd()
sid = str(uuid.uuid4())


def hook(payload):
    r = subprocess.run([sys.executable, "-m", "once.hook", "claude-code"], text=True, capture_output=True,
                       input=json.dumps({"session_id": sid, "cwd": cwd, **payload}))
    return r.stdout


ctx = hook({"hook_event_name": "SessionStart", "source": "startup"})
hook({"hook_event_name": "UserPromptSubmit", "prompt": sys.argv[1]})
knows = "uv run" in ctx
if not knows:
    for _ in range(2):
        hook({"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "tool_input": {"command": "python -m pytest"},
              "error": "Exit code 127\nzsh: command not found: python"})
hook({"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "uv run pytest"},
      "tool_response": {"stdout": "ok"}})
open("done.txt", "w").write("ok")
hook({"hook_event_name": "SessionEnd", "reason": "exit"})
print(json.dumps({"total_cost_usd": 0.01}))
