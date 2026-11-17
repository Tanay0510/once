"""A stand-in agent for `once lab run`: reports a session through the real hook, and fixes the bug if FAKE_FIX=1."""
import json
import os
import subprocess
import sys
import uuid

cwd, sid = os.getcwd(), str(uuid.uuid4())


def hook(payload):
    return subprocess.run([sys.executable, "-m", "once.hook", "claude-code"], text=True, capture_output=True,
                          input=json.dumps({"session_id": sid, "cwd": cwd, **payload})).stdout


ctx = hook({"hook_event_name": "SessionStart", "source": "startup"})
hook({"hook_event_name": "UserPromptSubmit", "prompt": sys.argv[1]})
hook({"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_input": {"file_path": os.path.join(cwd, "calc.py")},
      "tool_response": {}})
mode = os.environ.get("FAKE_FIX")
if mode == "1" or (mode == "ctx" and (ctx.strip() or os.path.exists("CLAUDE.md"))):
    open("calc.py", "w").write("def add(a, b):\n    return a + b\n")
    hook({"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {"file_path": os.path.join(cwd, "calc.py")},
          "tool_response": {}})
hook({"hook_event_name": "SessionEnd", "reason": "exit"})
print(json.dumps({"total_cost_usd": 0.02}))
