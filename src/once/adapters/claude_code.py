"""Claude Code adapter.

Hook contract checked against https://code.claude.com/docs/en/hooks (Nov 2026):
settings live in ~/.claude/settings.json; exit 0 with one JSON object on stdout adds context via
hookSpecificOutput.additionalContext; any other exit code is a non-blocking error with a notice.
The field names for PostToolUse / PostToolUseFailure results are read defensively: confirm them
against a real session (`once doctor --payloads`) before relying on failure detection.
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

NAME = "claude-code"
MARK = "once.hook"  # identifies our entries in settings.json
NATIVE_DIR = ".claude/rules/once"

TOOL_KIND = {"Bash": "shell", "Read": "read", "Edit": "edit", "Write": "edit", "MultiEdit": "edit",
             "NotebookEdit": "edit", "Grep": "search", "Glob": "search", "WebFetch": "web",
             "WebSearch": "web", "Task": "subagent", "Agent": "subagent"}

INJECT_EVENT = "SessionStart"
HEADLESS = ('claude -p {prompt} --permission-mode acceptEdits --allowedTools Bash Read Edit Write Grep Glob '
            '--output-format json --max-budget-usd {budget}')


def normalize(raw: dict, event: str | None) -> dict:
    return raw


def fallback(event_name: str) -> str:
    return ""


# event -> (matcher, async). Capture is async wherever the agent allows; only injection may block.
EVENTS = {
    "SessionStart": (None, False),
    "PreToolUse": ("Bash|Edit|Write|MultiEdit|NotebookEdit", False),  # path rules before shell edits; block-level rules
    "UserPromptSubmit": (None, True),
    "PostToolUse": (None, True),
    "PostToolUseFailure": (None, True),
    "SessionEnd": (None, False),
}

# One more group, synchronous, so remind- and check-level rules can answer right after an edit lands.
# Capture stays in the async PostToolUse group above; this one only enforces.
ENFORCE = {"PostToolUse": ("Edit|Write|MultiEdit|NotebookEdit", "enforce")}

CAPABILITIES = {"inject_at_start": True, "inject_on_path": True, "native_path_rules": True,
                "sees_edit_tools": True, "async_hooks": True, "headless": False, "enforce": True}


# ---------------------------------------------------------------- capture (hot path)

def _digest(text: str) -> tuple[str, str]:
    lines = [l.strip() for l in (text or "").splitlines() if l.strip()]
    line = next((l for l in lines if re.search(r"(?i)\berror\b|\bfailed\b|exception|traceback", l)),
                lines[0] if lines else "")
    norm = re.sub(r"(/[\w.\-]+)+", "<path>", line.lower())
    norm = re.sub(r":\d+(:\d+)?", ":N", norm)
    norm = re.sub(r"0x[0-9a-f]+|\b\d+(\.\d+)?(ms|s)\b", "<n>", norm)
    norm = re.sub(r"\s+", " ", norm)[:200]
    code = re.search(r"\b[A-Z]{1,6}\d{3,5}\b", text or "")
    head = code.group(0) if code else line[:60]
    return (hashlib.sha1(norm.encode()).hexdigest()[:10] if norm else ""), head


def _failed(resp) -> tuple[bool, str]:
    """Best-effort read of a tool result. Returns (failed, error text)."""
    if isinstance(resp, str):
        return False, ""
    if not isinstance(resp, dict):
        return False, ""
    for k in ("exit_code", "exitCode", "returncode", "code"):
        if isinstance(resp.get(k), int) and resp[k] != 0:
            return True, str(resp.get("stderr") or resp.get("stdout") or resp.get("error") or "")
    if resp.get("is_error") or resp.get("isError") or resp.get("error"):
        return True, str(resp.get("error") or resp.get("stderr") or resp.get("content") or "")
    if resp.get("interrupted"):
        return True, "interrupted"
    return False, ""


_WRITES = re.compile(r"(>>?|\btee\b|\bsed\s+-i|\bmv\b|\bcp\b|\brm\b|\btouch\b|\bpatch\b|\bgit\s+(apply|checkout|restore)\b)")


def shell_paths(command: str, root: str | None, limit: int = 6) -> tuple[list[str], bool]:
    """Files a shell command touches, relative to the repo. Claude often edits with echo/sed, not Edit."""
    if not command or not root:
        return [], False
    import shlex
    try:
        toks = shlex.split(command, comments=False, posix=True)
    except ValueError:
        toks = command.split()
    out = []
    for t in toks:
        t = t.strip("'\"`();|&<>")
        if not t or t.startswith("-") or len(t) > 240 or "\n" in t:
            continue
        cand = t if os.path.isabs(t) else os.path.join(root, t)
        if os.path.isfile(cand):
            rel = _rel(cand, root)
            if not rel.startswith("..") and rel not in out:
                out.append(rel)
        if len(out) >= limit:
            break
    return out, bool(_WRITES.search(command))


def _rel(path: str, root: str | None) -> str:
    if not path:
        return ""
    if root and os.path.isabs(path):
        try:
            return os.path.relpath(path, root)
        except ValueError:
            return path
    return path


def parse(raw: dict, root: str | None) -> list[dict]:
    """Native hook payload -> normalized events (without ts/session, which the hook adds)."""
    name = raw.get("hook_event_name", "")
    if name == "SessionStart":
        return [{"event": "session_start", "source": raw.get("source"), "model": raw.get("model"),
                 "cwd": raw.get("cwd"), "transcript_path": raw.get("transcript_path"),
                 "permission_mode": raw.get("permission_mode")}]
    if name == "UserPromptSubmit":
        return [{"event": "user_prompt", "prompt": raw.get("prompt") or raw.get("user_prompt") or ""}]
    if name in ("PostToolUse", "PostToolUseFailure"):
        tool = raw.get("tool_name", "")
        ti = raw.get("tool_input") if isinstance(raw.get("tool_input"), dict) else {}
        kind = TOOL_KIND.get(tool, "mcp" if tool.startswith("mcp__") else "other")
        fp = ti.get("file_path") or ti.get("notebook_path") or ti.get("path") or ""
        rel = _rel(fp, root)
        target = ti.get("command") or rel or ti.get("pattern") or ti.get("url") or ti.get("description") or tool
        if kind == "search" and ti.get("pattern"):
            target = f"{ti['pattern']}" + (f"  in {rel}" if rel else "")
        resp = raw.get("tool_response", raw.get("tool_result"))
        failed, err = (True, str(raw.get("error") or raw.get("tool_error") or "")) \
            if name == "PostToolUseFailure" else _failed(resp)
        if failed and not err and isinstance(resp, dict):
            err = str(resp.get("stderr") or resp.get("stdout") or "")
        ev = {"event": "tool_result", "tool": kind, "tool_name": tool, "target": str(target)[:300],
              "paths": [rel] if rel else [], "ok": not failed, "tool_use_id": raw.get("tool_use_id")}
        if kind == "edit":  # hashes only: enough to see an edit undone later, without storing content
            o, n = ti.get("old_string"), ti.get("new_string", ti.get("content"))
            ev["edit_sig"] = {"old": hashlib.sha1(o.encode()).hexdigest()[:12] if isinstance(o, str) and o else None,
                              "new": hashlib.sha1(n.encode()).hexdigest()[:12] if isinstance(n, str) and n else None}
        if kind == "shell":
            ev["command"] = str(ti.get("command", ""))[:500]
            ev["paths"], ev["writes"] = shell_paths(str(ti.get("command", "")), root)
        if failed:
            ev["error_digest"], ev["error_head"] = _digest(err)
            ev["error"] = err[-600:]
        return [ev]
    if name == "SessionEnd":
        return [{"event": "session_end", "reason": raw.get("reason")}]
    return []


def pre_tool_paths(raw: dict, root: str | None) -> list[str]:
    """Paths a pending tool call will touch; used to deliver path-scoped rules before a shell command."""
    ti = raw.get("tool_input") if isinstance(raw.get("tool_input"), dict) else {}
    if raw.get("tool_name") == "Bash":
        return shell_paths(str(ti.get("command", "")), root)[0]
    fp = ti.get("file_path") or ti.get("path") or ""
    return [_rel(fp, root)] if fp else []


def reply(event_name: str, context: str) -> str:
    if not context:
        return ""
    return json.dumps({"hookSpecificOutput": {"hookEventName": event_name, "additionalContext": context}})


def deny(reason: str) -> str:
    """A block-level rule refuses the tool call before it lands; the reason goes to the agent."""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                              "permissionDecisionReason": reason}})


def enforce_reply(check: str, remind: str) -> str:
    """After an edit: a check-level finding is returned as an error the agent must address; a reminder is context."""
    if check:
        out = {"decision": "block", "reason": check}
        if remind:
            out["hookSpecificOutput"] = {"hookEventName": "PostToolUse", "additionalContext": remind}
        return json.dumps(out)
    return reply("PostToolUse", remind)


def edit_text(raw: dict) -> str:
    """The text a tool call writes, or the command it runs: what a detector is matched against."""
    ti = raw.get("tool_input") if isinstance(raw.get("tool_input"), dict) else {}
    if raw.get("tool_name") == "Bash":
        return str(ti.get("command") or "")
    if isinstance(ti.get("edits"), list):  # MultiEdit
        return "\n".join(str(e.get("new_string") or "") for e in ti["edits"] if isinstance(e, dict))
    return str(ti.get("new_string") or ti.get("content") or ti.get("new_source") or "")


# ---------------------------------------------------------------- setup (not on the hot path)

def settings_path() -> Path:
    override = os.environ.get("ONCE_CLAUDE_SETTINGS")
    if override:
        return Path(override)
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "settings.json"


def hook_command() -> str:
    return f'"{sys.executable}" -m {MARK} {NAME}'


def _load_settings() -> dict:
    p = settings_path()
    if not p.exists():
        return {}
    return json.loads(p.read_text() or "{}")


def installed() -> dict:
    """event -> True/False for whether our hook is registered."""
    try:
        hooks = _load_settings().get("hooks", {})
    except Exception:
        return {e: False for e in EVENTS}
    out = {}
    for ev in EVENTS:
        out[ev] = any(MARK in h.get("command", "") and not h.get("command", "").endswith(" enforce")
                      for g in hooks.get(ev, []) for h in g.get("hooks", []))
    for ev, (_, arg) in ENFORCE.items():
        out[f"{ev} ({arg})"] = any(h.get("command", "").endswith(f"{MARK} {NAME} {arg}")
                                   for g in hooks.get(ev, []) for h in g.get("hooks", []))
    return out


def install() -> list[str]:
    """Register user-level hooks. Idempotent; keeps a one-time backup of the settings file."""
    p = settings_path()
    s = _load_settings()
    if p.exists():
        bak = p.with_suffix(".json.once.bak")
        if not bak.exists():
            bak.write_text(p.read_text())
    hooks = s.setdefault("hooks", {})
    changed = []
    for ev, (matcher, is_async) in EVENTS.items():
        groups = hooks.setdefault(ev, [])
        groups[:] = [g for g in groups if not any(MARK in h.get("command", "") for h in g.get("hooks", []))]
        handler = {"type": "command", "command": hook_command(), "timeout": 10}
        if is_async:
            handler["async"] = True
        group = {"hooks": [handler]}
        if matcher:
            group["matcher"] = matcher
        groups.append(group)
        changed.append(ev)
    for ev, (matcher, arg) in ENFORCE.items():
        hooks.setdefault(ev, []).append({"matcher": matcher, "hooks": [
            {"type": "command", "command": f"{hook_command()} {arg}", "timeout": 10}]})
        changed.append(f"{ev} ({arg})")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s, indent=2) + "\n")
    return changed


def uninstall() -> list[str]:
    p = settings_path()
    if not p.exists():
        return []
    s = _load_settings()
    removed = []
    hooks = s.get("hooks", {})
    for ev in list(hooks):
        before = len(hooks[ev])
        hooks[ev] = [g for g in hooks[ev] if not any(MARK in h.get("command", "") for h in g.get("hooks", []))]
        if len(hooks[ev]) != before:
            removed.append(ev)
        if not hooks[ev]:
            del hooks[ev]
    if not hooks:
        s.pop("hooks", None)
    p.write_text(json.dumps(s, indent=2) + "\n")
    return removed


SKILL_PREFIX = "once-"
MCP_NAME = "once"


def mcp_command() -> list[str]:
    return [sys.executable, "-m", "once.mcp_server"]


def register_mcp(remove: bool = False) -> tuple[bool, str]:
    """Register the stdio MCP server at user scope through Claude Code's own CLI."""
    import shutil
    import subprocess
    if os.environ.get("ONCE_CLAUDE_SETTINGS") or os.environ.get("ONCE_SKIP_MCP"):
        return False, "skipped (test mode)"
    exe = shutil.which("claude")
    if not exe:
        return False, "claude not on PATH"
    try:
        subprocess.run([exe, "mcp", "remove", "-s", "user", MCP_NAME], capture_output=True, text=True, timeout=30)
        if remove:
            return True, "removed"
        r = subprocess.run([exe, "mcp", "add", "-s", "user", MCP_NAME, "--", *mcp_command()],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0, (r.stdout or r.stderr).strip().splitlines()[-1] if (r.stdout or r.stderr).strip() else ""
    except Exception as e:
        return False, str(e)


def emit_skills(playbooks: list[dict], repo_root: Path) -> list[Path]:
    """Playbooks as skills: the short description is always visible, the body loads on demand."""
    base = repo_root / ".claude" / "skills"
    remove_skills(repo_root)
    written = []
    for pb in playbooks:
        d = base / f"{SKILL_PREFIX}{pb['id']}"
        d.mkdir(parents=True, exist_ok=True)
        desc = " ".join(str(pb.get("description", "")).split())
        (d / "SKILL.md").write_text(f"---\nname: {SKILL_PREFIX}{pb['id']}\ndescription: {json.dumps(desc)}\n---\n\n"
                                    f"{pb['body'].strip()}\n\n<!-- generated by once; edits here are overwritten -->\n")
        written.append(d / "SKILL.md")
    return written


def remove_skills(repo_root: Path) -> int:
    import shutil
    base = repo_root / ".claude" / "skills"
    n = 0
    if base.is_dir():
        for d in base.glob(f"{SKILL_PREFIX}*"):
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n


COMMANDS = {
    "once-teach": ("Add a rule for this repository to once",
                   "Run the shell command `once teach` with the following sentence as one quoted argument, then show "
                   "its output and do nothing else.\n\nSentence: $ARGUMENTS"),
    "once-flag": ("Flag what you just did as a mistake, for once",
                  "Call the `record_feedback` tool of the `once` MCP server. Use this as the note: $ARGUMENTS\n"
                  "Choose the tag that fits: wrong, avoidable or missing_knowledge. Confirm in one line and do nothing else."),
    "once-why": ("Show which rules from `once` apply in this session",
                 "Run the shell command `once why` and show its output. Do nothing else."),
    "once-off": ("Stop `once` giving rules for the rest of this session",
                 "Run the shell command `once off --session` and show its output. Do nothing else. "
                 "From now on, do not apply the repository conventions that `once` gave you earlier in this session."),
}


def commands_dir() -> Path:
    return settings_path().parent / "commands"


def install_commands() -> list[str]:
    """In-agent slash commands, so teaching and flagging do not need a second terminal."""
    d = commands_dir()
    d.mkdir(parents=True, exist_ok=True)
    for name, (desc, body) in COMMANDS.items():
        (d / f"{name}.md").write_text(f"---\ndescription: {desc}\n---\n{body}\n\n<!-- generated by once -->\n")
    return [f"/{n}" for n in COMMANDS]


def remove_commands() -> int:
    n = 0
    for name in COMMANDS:
        f = commands_dir() / f"{name}.md"
        if f.exists() and "generated by once" in f.read_text():
            f.unlink()
            n += 1
    return n


def emit_native(index_rules: list[dict], repo_root: Path) -> list[Path]:
    """Path-scoped rules as native rule files Claude Code loads when a matching file is read."""
    d = repo_root / NATIVE_DIR
    written = []
    if d.is_dir():
        for old in d.glob("*.md"):
            old.unlink()
    scoped = [r for r in index_rules if not r.get("repo_wide") and r.get("level", "instruct") == "instruct"]
    if not scoped:
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
        return written
    d.mkdir(parents=True, exist_ok=True)
    for r in scoped:
        body = ["---", "paths:"] + [f'  - "{g}"' for g in r["globs"]] + ["---", "", f"# {r['id']}", "", r["text"]]
        if r.get("why"):
            body += ["", f"Why: {r['why']}"]
        body += ["", "<!-- generated by once; edits here are overwritten -->"]
        f = d / f"{r['id']}.md"
        f.write_text("\n".join(body) + "\n")
        written.append(f)
    return written


def remove_native(repo_root: Path) -> int:
    d = repo_root / NATIVE_DIR
    n = 0
    if d.is_dir():
        for f in d.glob("*.md"):
            f.unlink()
            n += 1
        if not any(d.iterdir()):
            d.rmdir()
    return n


# ---------------------------------------------------------------- status line

STATUS_MARK = "once.statusline"


def statusline_command() -> str:
    return f'"{sys.executable}" -m {STATUS_MARK}'


def statusline_installed() -> bool:
    try:
        return STATUS_MARK in ((_load_settings().get("statusLine") or {}).get("command") or "")
    except Exception:
        return False


def install_statusline() -> str | None:
    """Put the `once` line at the bottom of Claude Code. A status line the user already had keeps running, first.

    Returns the command that was wrapped, if any."""
    p = settings_path()
    s = _load_settings()
    cur = s.get("statusLine") or {}
    wrapped = None
    if cur.get("command") and STATUS_MARK not in cur["command"]:
        wrapped = cur["command"]
        from .. import paths
        cfgf = paths.home() / "config.json"
        try:
            cfg = json.loads(cfgf.read_text())
        except Exception:
            cfg = {}
        cfg["statusline_wrap"] = wrapped
        cfgf.parent.mkdir(parents=True, exist_ok=True)
        cfgf.write_text(json.dumps(cfg, indent=2))
    s["statusLine"] = {"type": "command", "command": statusline_command(), "padding": 0}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s, indent=2) + "\n")
    return wrapped


def remove_statusline() -> bool:
    """Take the `once` line out and give back the one it wrapped."""
    p = settings_path()
    if not p.exists():
        return False
    s = _load_settings()
    cur = s.get("statusLine") or {}
    if STATUS_MARK not in (cur.get("command") or ""):
        return False
    from .. import paths
    cfgf = paths.home() / "config.json"
    try:
        cfg = json.loads(cfgf.read_text())
    except Exception:
        cfg = {}
    wrapped = cfg.pop("statusline_wrap", None)
    if wrapped:
        s["statusLine"] = {"type": "command", "command": wrapped}
        cfgf.write_text(json.dumps(cfg, indent=2))
    else:
        s.pop("statusLine", None)
    p.write_text(json.dumps(s, indent=2) + "\n")
    return True
