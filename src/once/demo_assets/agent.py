"""The scripted agent `once demo` uses: talks to the real hook exactly as Claude Code does, and spends nothing.

Without the convention it tries the obvious test command, fails twice the same way, then finds `make check`.
With the convention in its session-start context, it runs `make check` first time.
"""
import json
import os
import subprocess
import sys
import uuid

cwd = os.getcwd()
sid = "demo-" + uuid.uuid4().hex[:10]


def hook(payload):
    r = subprocess.run([sys.executable, "-m", "once.hook", "claude-code"], text=True, capture_output=True,
                       input=json.dumps({"session_id": sid, "cwd": cwd, "transcript_path": "", **payload}))
    return r.stdout


ctx = hook({"hook_event_name": "SessionStart", "source": "startup", "model": "demo-agent"})
prompt = sys.argv[1] if len(sys.argv) > 1 else "Add a greeting for French"
hook({"hook_event_name": "UserPromptSubmit", "prompt": prompt})
core = os.path.join(cwd, "toy", "core.py")
hook({"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_input": {"file_path": core},
      "tool_response": {"content": "..."}})
with open(core, "a") as f:
    f.write('\nGREETINGS["fr"] = "Bonjour"\n')
hook({"hook_event_name": "PostToolUse", "tool_name": "Edit",
      "tool_input": {"file_path": core, "old_string": "", "new_string": 'GREETINGS["fr"] = "Bonjour"'},
      "tool_response": {"ok": True}})
if "make check" not in ctx:
    err = ("FileNotFoundError: fixture tests/fixtures/greetings.json not found\n"
           "  File \"tests/test_core.py\", line 7, in setUp")
    for _ in range(2):
        hook({"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
              "tool_input": {"command": "python3 -m unittest"}, "error": err})
    hook({"hook_event_name": "PostToolUse", "tool_name": "Read", "tool_input": {"file_path": os.path.join(cwd, "Makefile")},
          "tool_response": {"content": "..."}})
subprocess.run(["make", "-s", "check"], cwd=cwd, capture_output=True)
hook({"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "make check"},
      "tool_response": {"stdout": "OK"}})
hook({"hook_event_name": "SessionEnd", "reason": "exit"})
print(json.dumps({"total_cost_usd": 0}))
