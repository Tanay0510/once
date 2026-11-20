"""OpenAI Codex CLI adapter.

Contract read from the Codex hooks reference (developers.openai.com/codex/hooks, hooks stable since 0.124)
and its documented behaviour, Nov 2026. Not yet run against a live codex.

What shapes this adapter:
  - hooks live in ~/.codex/hooks.json, in the same shape as Claude Code's: event -> [{matcher, hooks: [...]}];
    stdin carries session_id, cwd, hook_event_name, model, transcript_path, prompt, tool_name, tool_input,
    tool_response, so the payload needs no renaming
  - a user hook is hashed and stays untrusted, and does not run, until the developer approves it once in
    Codex with /hooks; any change to the command makes it untrusted again, so the command is kept stable
  - SessionStart takes plain text on stdout as developer context; PreToolUse takes
    hookSpecificOutput.additionalContext, and fires for shell and apply_patch alike (the patch text is in
    tool_input.command). It may deny a call (permissionDecision "deny"); once does that only for block-level
    rules and otherwise decides nothing about the call
  - PostToolUse may answer with decision "block" and a reason, which replaces the tool result the model sees:
    that is how check-level rules talk back; remind-level ones use additionalContext
  - no async hooks: every hook runs in line, so the hook must stay fast (it takes about 30 ms)
  - edits arrive as apply_patch, whose input is a patch; file paths are read from its headers
"""
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import claude_code as _cc

NAME = "codex"
MARK = "once.hook"
INJECT_EVENT = "SessionStart"
MIN_VERSION = (0, 124)  # hooks became stable; older versions need [features] codex_hooks = true
HEADLESS = "codex exec --json --full-auto --skip-git-repo-check {prompt}"
MCP_NAME = "once"

# event -> regex matcher (Codex matchers are regular expressions)
EVENTS = {
    "SessionStart": None,
    "UserPromptSubmit": None,
    "PreToolUse": "^(Bash|apply_patch|Edit|Write)$",  # path rules before a command or patch; block-level rules
    "PostToolUse": None,
    "Stop": None,
    "SessionEnd": None,
}

TOOL_KIND = {"Bash": "shell", "shell": "shell", "local_shell": "shell", "exec_command": "shell",
             "apply_patch": "edit", "Edit": "edit", "Write": "edit", "Read": "read", "view_image": "read",
             "web_search": "web", "update_plan": "other"}

CAPABILITIES = {"inject_at_start": True, "inject_on_path": True, "native_path_rules": False,
                "sees_edit_tools": True, "async_hooks": False, "headless": True, "needs_trust": True,
                "enforce": True, "enforce_inline": True}  # hooks are synchronous: enforce in the PostToolUse hook itself


def normalize(raw: dict, event: str | None) -> dict:
    if event and not raw.get("hook_event_name"):
        raw = {**raw, "hook_event_name": event}
    return raw


_PATCH_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)


def _patch_paths(ti: dict) -> list[str]:
    text = ti.get("input") or ti.get("patch") or ti.get("command") or ""
    if isinstance(text, list):
        text = " ".join(map(str, text))
    return [p.strip() for p in _PATCH_FILE.findall(str(text))][:6]


_EXIT = re.compile(r"(?i)(?:exit code|exit_code|\"exit_code\")[\":\s]+(-?\d+)")


def _failed(resp) -> tuple[bool, str]:
    """Codex reports a shell result as a dict or as text such as 'Exit code: 1\\nOutput: ...'."""
    if isinstance(resp, str):
        try:
            j = json.loads(resp)
            if isinstance(j, dict):
                return _failed(j)
        except Exception:
            pass
        m = _EXIT.search(resp[:300])
        if m and int(m.group(1)) != 0:
            return True, resp
        return False, ""
    if isinstance(resp, dict):
        meta = resp.get("metadata") if isinstance(resp.get("metadata"), dict) else {}
        code = resp.get("exit_code", meta.get("exit_code"))
        if isinstance(code, int) and code != 0:
            return True, str(resp.get("output") or resp.get("stderr") or resp.get("stdout") or "")
        return _cc._failed(resp)
    return False, ""


def parse(raw: dict, root: str | None) -> list[dict]:
    name = raw.get("hook_event_name", "")
    if name == "SessionStart":
        return [{"event": "session_start", "source": raw.get("source") or raw.get("matcher_value"),
                 "model": raw.get("model"), "cwd": raw.get("cwd"), "transcript_path": raw.get("transcript_path"),
                 "permission_mode": raw.get("permission_mode")}]
    if name == "UserPromptSubmit":
        return [{"event": "user_prompt", "prompt": raw.get("prompt") or ""}]
    if name == "PostToolUse":
        tool = raw.get("tool_name") or ""
        ti = raw.get("tool_input") if isinstance(raw.get("tool_input"), dict) else {}
        kind = TOOL_KIND.get(tool, "mcp" if tool.startswith("mcp__") else "other")
        cmd = ti.get("command") or ti.get("cmd") or ""
        if isinstance(cmd, list):  # ["bash", "-lc", "pytest -q"]
            cmd = cmd[-1] if len(cmd) >= 3 and cmd[0] in ("bash", "sh", "zsh") else " ".join(map(str, cmd))
        if tool == "apply_patch":
            paths = [_cc._rel(p, root) for p in _patch_paths(ti)]
            cmd = ""  # tool_input.command is the patch text, not a command
        else:
            fp = ti.get("file_path") or ti.get("path") or ""
            paths = [_cc._rel(fp, root)] if fp else []
        target = cmd or (paths[0] if paths else "") or ti.get("query") or tool
        failed, err = _failed(raw.get("tool_response"))
        ev = {"event": "tool_result", "tool": kind, "tool_name": tool, "target": str(target)[:300],
              "paths": paths, "ok": not failed}
        if kind == "shell":
            ev["command"] = str(cmd)[:500]
            ev["paths"], ev["writes"] = _cc.shell_paths(str(cmd), root)
        if not failed:
            _cc.pr_fields(ev, tool, str(cmd), raw.get("tool_response"))
        if failed:
            ev["error_digest"], ev["error_head"] = _cc._digest(err)
            ev["error"] = err[-600:]
        return [ev]
    if name == "Stop":
        return [{"event": "turn_end"}]
    if name == "SessionEnd":
        return [{"event": "session_end", "reason": raw.get("reason")}]
    return []


def pre_tool_paths(raw: dict, root: str | None) -> list[str]:
    ti = raw.get("tool_input") if isinstance(raw.get("tool_input"), dict) else {}
    if raw.get("tool_name") in ("apply_patch", "Edit", "Write"):
        fp = ti.get("file_path") or ti.get("path")
        return [_cc._rel(p, root) for p in (_patch_paths(ti) or ([fp] if fp else []))]
    cmd = ti.get("command") or ""
    if isinstance(cmd, list):
        cmd = cmd[-1] if cmd else ""
    return _cc.shell_paths(str(cmd), root)[0]


def edit_text(raw: dict) -> str:
    """What a detector is matched against: the lines a patch adds, or the shell command."""
    ti = raw.get("tool_input") if isinstance(raw.get("tool_input"), dict) else {}
    if raw.get("tool_name") == "apply_patch" or _PATCH_FILE.search(str(ti.get("command") or ti.get("input") or "")):
        text = str(ti.get("command") or ti.get("input") or ti.get("patch") or "")
        return "\n".join(l[1:] for l in text.splitlines() if l.startswith("+") and not l.startswith("+++"))
    if raw.get("tool_name") in ("Edit", "Write"):
        return str(ti.get("new_string") or ti.get("content") or "")
    cmd = ti.get("command") or ti.get("cmd") or ""
    return " ".join(map(str, cmd)) if isinstance(cmd, list) else str(cmd)


def deny(reason: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": reason}})


def enforce_reply(check: str, remind: str) -> str:
    if check:
        out = {"decision": "block", "reason": check}
        if remind:
            out["hookSpecificOutput"] = {"hookEventName": "PostToolUse", "additionalContext": remind}
        return json.dumps(out)
    return reply("PostToolUse", remind)


def reply(event_name: str, context: str) -> str:
    if not context:
        return ""
    if event_name == "SessionStart":
        return context  # plain text on stdout becomes developer context
    return json.dumps({"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": context}})


def fallback(event_name: str) -> str:
    return ""


# ---------------------------------------------------------------- setup

def settings_path() -> Path:
    return Path(os.environ.get("ONCE_CODEX_HOOKS") or Path.home() / ".codex" / "hooks.json")


def hook_command() -> str:
    # Kept identical across installs: Codex re-asks for trust whenever the command text changes.
    return f'"{sys.executable}" -m {MARK} {NAME}'


def _load() -> dict:
    p = settings_path()
    try:
        return json.loads(p.read_text() or "{}") if p.exists() else {}
    except Exception:
        return {}


def installed() -> dict:
    hooks = _load().get("hooks") or {}
    return {e: any(MARK in h.get("command", "") and f" {NAME}" in h.get("command", "")
                   for g in hooks.get(e, []) for h in g.get("hooks", [])) for e in EVENTS}


def install() -> list[str]:
    p = settings_path()
    s = _load()
    if p.exists():
        bak = p.with_suffix(".json.once.bak")
        if not bak.exists():
            bak.write_text(p.read_text())
    hooks = s.setdefault("hooks", {})
    for ev, matcher in EVENTS.items():
        groups = hooks.setdefault(ev, [])
        groups[:] = [g for g in groups if not any(MARK in h.get("command", "") for h in g.get("hooks", []))]
        group = {"hooks": [{"type": "command", "command": hook_command(), "timeout": 10}]}
        if matcher:
            group["matcher"] = matcher
        groups.append(group)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s, indent=2) + "\n")
    return list(EVENTS)


def uninstall() -> list[str]:
    p = settings_path()
    if not p.exists():
        return []
    s = _load()
    hooks = s.get("hooks") or {}
    removed = []
    for ev in list(hooks):
        before = len(hooks[ev])
        hooks[ev] = [g for g in hooks[ev] if not any(MARK in h.get("command", "") for h in g.get("hooks", []))]
        if len(hooks[ev]) != before:
            removed.append(ev)
        if not hooks[ev]:
            del hooks[ev]
    if "hooks" in s and not hooks:
        s.pop("hooks")
    p.write_text(json.dumps(s, indent=2) + "\n")
    return removed


def version() -> tuple[int, ...] | None:
    exe = shutil.which("codex")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20,
                             stdin=subprocess.DEVNULL).stdout
        m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out or "")
        return tuple(int(x) for x in m.groups() if x is not None) if m else None
    except Exception:
        return None


def register_mcp(remove: bool = False) -> tuple[bool, str]:
    """Register the MCP server through Codex's own CLI, so review_rule and remember_rule work in chat."""
    if os.environ.get("ONCE_CODEX_HOOKS") or os.environ.get("ONCE_SKIP_MCP"):
        return False, "skipped (test mode)"
    exe = shutil.which("codex")
    if not exe:
        return False, "codex not on PATH"
    try:
        subprocess.run([exe, "mcp", "remove", MCP_NAME], capture_output=True, text=True, timeout=30,
                       stdin=subprocess.DEVNULL)
        if remove:
            return True, "removed"
        r = subprocess.run([exe, "mcp", "add", MCP_NAME, "--", *_cc.mcp_command()], capture_output=True,
                           text=True, timeout=30, stdin=subprocess.DEVNULL)
        return r.returncode == 0, ((r.stdout or r.stderr).strip().splitlines() or [""])[-1]
    except Exception as e:
        return False, str(e)


def install_commands():
    return []


def remove_commands():
    return 0


def emit_native(index_rules, repo_root):
    return []  # Codex has no path-scoped rule files; rules arrive through the hooks


def remove_native(repo_root):
    return 0


def emit_skills(playbooks, repo_root):
    from . import antigravity
    return antigravity.emit_skills(playbooks, repo_root)  # .agents/skills is shared by Codex and Antigravity


def remove_skills(repo_root):
    from . import antigravity
    return antigravity.remove_skills(repo_root)


# ---------------------------------------------------------------- headless output

def parse_output(out: str) -> tuple[float | None, str | None]:
    """`codex exec --json` prints one JSON event per line. Returns (cost, error); Codex reports no cost."""
    err = None
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        t = str(e.get("type") or (e.get("msg") or {}).get("type") or "")
        if t in ("error", "turn.failed", "stream_error") or t.endswith(".failed"):
            eo = e.get("error")
            msg = (eo.get("message") if isinstance(eo, dict) else eo) or e.get("message") \
                or (e.get("msg") or {}).get("message")
            err = str(msg or t)[:400]
    return None, err
