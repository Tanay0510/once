"""Antigravity CLI (agy) adapter.

Contract read from https://antigravity.google/docs/hooks/ (Nov 2026). Not yet run against a live agy.
Differences from Claude Code that shape this adapter:
  - hooks live in ~/.gemini/config/hooks.json under a named group; stdin is camelCase and does not
    name the event, so the event is passed on the command line
  - every hook must print a JSON object; context for the model is injected at PreInvocation as an
    ephemeral message, which lasts one turn, so repo-wide rules are re-sent on each invocation
  - PreToolUse must answer with a decision. once answers "allow", which hands the call on to the next hook
    or the default policy (it grants nothing), and "deny" only for a block-level rule the call breaks
  - PostToolUse cannot speak to the model (its reply is an empty object), so remind- and check-level
    findings are held and delivered at the next PreInvocation
  - MCP servers: ~/.gemini/config/mcp_config.json ({"mcpServers": {name: {command, args}}}), no CLI to add one
  - rules: .agents/rules/*.md with frontmatter `trigger: glob` + `globs:`; skills: .agents/skills/<name>/SKILL.md
    (the folder Codex reads skills from too). Both are excluded through .git/info/exclude.
"""
import json
import os
import re
import sys
from pathlib import Path

from . import claude_code as _cc

NAME = "antigravity"
MARK = "once.hook"
GROUP = "once"
INJECT_EVENT = "PreInvocation"
EVENTS = ("SessionStart", "PreInvocation", "PreToolUse", "PostToolUse", "Stop")
MATCHED = ("PreToolUse", "PostToolUse")  # the events that take a matcher
HEADLESS = "agy --output-format json --mode accept-edits -p {prompt}"

# Names seen in live agy 1.5.7 payloads (read_file, edit_file, write_file, ...) and the ones its docs use.
TOOL_KIND = {"run_command": "shell", "read_file": "read", "view_file": "read", "edit_file": "edit", "write_file": "edit",
             "create_file": "edit", "write_to_file": "edit", "replace_file_content": "edit",
             "multi_replace_file_content": "edit", "list_dir": "search", "list_directory": "search",
             "find_by_name": "search", "find_files": "search", "grep_search": "search", "grep": "search",
             "search_web": "web", "read_url_content": "web", "invoke_subagent": "subagent"}

CAPABILITIES = {"inject_at_start": True, "inject_on_path": False, "native_path_rules": True,
                "reinject_each_turn": True, "sees_edit_tools": True, "async_hooks": False, "headless": True,
                "enforce": True, "enforce_inline": True, "enforce_deferred": True}
RULES_DIR = ".agents/rules"
SKILLS_DIR = ".agents/skills"
PREFIX = "once-"


def normalize(raw: dict, event: str | None) -> dict:
    """camelCase agy payload -> the field names the shared hook code expects."""
    ws = raw.get("workspacePaths") or []
    tc = raw.get("toolCall") if isinstance(raw.get("toolCall"), dict) else None
    return {"hook_event_name": event or "", "session_id": raw.get("conversationId") or "",
            "cwd": ws[0] if ws else os.getcwd(), "transcript_path": raw.get("transcriptPath"),
            "model": raw.get("modelName"), "prompt": raw.get("lastUserInput") or "",
            "tool_name": (tc or {}).get("name"), "tool_input": (tc or {}).get("args") or {},
            "error": raw.get("error") or "", "has_tool": tc is not None, "step_idx": raw.get("stepIdx"),
            "reason": raw.get("terminationReason"), "fully_idle": raw.get("fullyIdle")}


def _path_arg(args: dict) -> str:
    for k, v in args.items():
        if isinstance(v, str) and v and ("path" in k.lower() or "file" in k.lower()) and "\n" not in v and len(v) < 400:
            return v
    return ""


def parse(raw: dict, root: str | None) -> list[dict]:
    name = raw.get("hook_event_name", "")
    if name == "SessionStart":
        return [{"event": "session_start", "source": "startup", "model": raw.get("model"), "cwd": raw.get("cwd"),
                 "transcript_path": raw.get("transcript_path")}]
    if name == "PreInvocation":
        return [{"event": "user_prompt", "prompt": raw["prompt"]}] if raw.get("prompt") else []
    if name == "PostToolUse":
        if not raw.get("has_tool"):
            return []  # agy also fires this with no tool call; there is nothing to record
        tool, args = raw.get("tool_name") or "", raw.get("tool_input") or {}
        kind = TOOL_KIND.get(tool, "mcp" if "__" in tool else "other")
        cmd = args.get("CommandLine") or args.get("commandLine") or args.get("command") or ""
        rel = _cc._rel(_path_arg(args), root)
        target = cmd or rel or args.get("Query") or args.get("query") or args.get("Pattern") or tool
        err = str(raw.get("error") or "")
        res = None
        if kind == "shell" and not err:
            # agy's PostToolUse says nothing about a command's exit code; its transcript does, at this step.
            res = command_result(raw.get("transcript_path"), raw.get("step_idx"))
            if res and res[0] != 0:
                err = f"exit code {res[0]}\n{res[1]}"
        failed = bool(err)
        ev = {"event": "tool_result", "tool": kind, "tool_name": tool, "target": str(target)[:300],
              "paths": [rel] if rel else [], "ok": not failed}
        if kind == "shell":
            ev["command"] = str(cmd)[:500]
            ev["paths"], ev["writes"] = _cc.shell_paths(str(cmd), root)
        if not failed:
            _cc.pr_fields(ev, tool, str(cmd), res[1] if res else raw.get("tool_output"))
        if failed:
            ev["error_digest"], ev["error_head"] = _cc._digest(err)
            ev["error"] = err[-600:]
        return [ev]
    if name == "Stop":
        return [{"event": "turn_end", "reason": raw.get("reason"), "fully_idle": raw.get("fully_idle")}]
    return []


_EXITED = re.compile(r"Command exited with code (-?\d+)\.?\s*(?:Command output:\s*)?(.*)", re.S)


def command_result(transcript_path, step_idx, tail: int = 262_144) -> tuple[int, str] | None:
    """(exit code, output) of the command whose result is transcript step `step_idx`. Reads only the end of the
    transcript, so the hook stays fast however long the conversation gets."""
    if not transcript_path or step_idx is None or not os.path.isfile(transcript_path):
        return None
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - tail))
            lines = f.read().decode(errors="ignore").splitlines()
        for line in reversed(lines):
            if f'"step_index": {int(step_idx)},' not in line and f'"step_index":{int(step_idx)},' not in line:
                continue
            m = _EXITED.search(str(json.loads(line).get("content") or ""))
            return (int(m.group(1)), m.group(2).strip()[-600:]) if m else None
    except Exception:
        return None
    return None


def reply(event_name: str, context: str) -> str:
    if event_name == "PreInvocation" and context:
        return json.dumps({"injectSteps": [{"ephemeralMessage": context}]})
    return fallback(event_name)


def fallback(event_name: str) -> str:
    """What to print when there is nothing to say. agy requires a JSON object from every hook."""
    if event_name == "PreToolUse":
        return json.dumps({"decision": "allow"})  # no opinion: the next hook or the default policy decides
    if event_name == "Stop":
        return json.dumps({"decision": "allow"})  # any value but "continue" lets the agent stop as it intended
    return "{}"


def pre_tool_paths(raw: dict, root: str | None) -> list[str]:
    args = raw.get("tool_input") or {}
    cmd = args.get("CommandLine") or args.get("commandLine") or args.get("command") or ""
    if cmd:
        return _cc.shell_paths(str(cmd), root)[0]
    p = _path_arg(args)
    return [_cc._rel(p, root)] if p else []


def edit_text(raw: dict) -> str:
    args = raw.get("tool_input") or {}
    cmd = args.get("CommandLine") or args.get("commandLine") or args.get("command")
    if cmd:
        return str(cmd)
    parts = [args.get(k) for k in ("CodeContent", "codeContent", "ReplacementContent", "replacementContent", "content",
                                    "Replacement", "Content", "Contents")]
    for chunk in args.get("ReplacementChunks") or args.get("replacementChunks") or []:
        if isinstance(chunk, dict):
            parts.append(chunk.get("ReplacementContent") or chunk.get("replacementContent"))
    return "\n".join(str(p) for p in parts if p)


def deny(reason: str) -> str:
    return json.dumps({"decision": "deny", "reason": reason})


# ---------------------------------------------------------------- setup

def settings_path() -> Path:
    return Path(os.environ.get("ONCE_AGY_HOOKS") or Path.home() / ".gemini" / "config" / "hooks.json")


def hook_command(event: str) -> str:
    return f'"{sys.executable}" -m {MARK} {NAME} {event}'


def _load() -> dict:
    p = settings_path()
    return json.loads(p.read_text() or "{}") if p.exists() else {}


def installed() -> dict:
    try:
        g = _load().get(GROUP) or {}
    except Exception:
        g = {}
    return {e: MARK in json.dumps(g.get(e, [])) for e in EVENTS}


def install() -> list[str]:
    p = settings_path()
    s = _load()
    if p.exists():
        bak = p.with_suffix(".json.once.bak")
        if not bak.exists():
            bak.write_text(p.read_text())
    group = {"enabled": True}
    for e in EVENTS:
        h = {"type": "command", "command": hook_command(e), "timeout": 10}
        group[e] = [{"matcher": ".*", "hooks": [h]}] if e in MATCHED else [h]
    s[GROUP] = group
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(s, indent=2) + "\n")
    return list(EVENTS)


def uninstall() -> list[str]:
    p = settings_path()
    if not p.exists():
        return []
    s = _load()
    had = list(EVENTS) if GROUP in s else []
    s.pop(GROUP, None)
    p.write_text(json.dumps(s, indent=2) + "\n")
    return had


def mcp_path() -> Path:
    return settings_path().parent / "mcp_config.json"


def register_mcp(remove: bool = False):
    """Antigravity has no command to add a server: the entry is written into mcp_config.json, others kept."""
    p = mcp_path()
    try:
        cfg = json.loads(p.read_text() or "{}") if p.exists() else {}
    except Exception:
        return False, f"{p} is not valid JSON; add the server by hand"
    servers = cfg.setdefault("mcpServers", {})
    if remove:
        if servers.pop(_cc.MCP_NAME, None) is None:
            return False, "not registered"
    else:
        cmd = _cc.mcp_command()
        servers[_cc.MCP_NAME] = {"command": cmd[0], "args": cmd[1:]}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, indent=2) + "\n")
    return True, "removed" if remove else f"added to {p}"


def mcp_registered() -> bool:
    try:
        return _cc.MCP_NAME in (json.loads(mcp_path().read_text()).get("mcpServers") or {})
    except Exception:
        return False


def install_commands():
    return []


def remove_commands():
    return 0


def emit_native(index_rules, repo_root):
    """Path-scoped rules as workspace rules Antigravity activates by glob. Instruct-level rules only."""
    d = Path(repo_root) / RULES_DIR
    remove_native(repo_root)
    written = []
    for r in index_rules:
        if r.get("repo_wide") or r.get("level", "instruct") != "instruct" or not r.get("globs"):
            continue
        d.mkdir(parents=True, exist_ok=True)
        body = ["---", "trigger: glob", f'globs: "{", ".join(r["globs"])}"', "---", "", f"# {r['id']}", "", r["text"]]
        if r.get("why"):
            body += ["", f"Why: {r['why']}"]
        body += ["", "<!-- generated by once; edits here are overwritten -->"]
        f = d / f"{PREFIX}{r['id']}.md"
        f.write_text("\n".join(body) + "\n")
        written.append(f)
    return written


def remove_native(repo_root):
    d = Path(repo_root) / RULES_DIR
    n = 0
    if d.is_dir():
        for f in d.glob(f"{PREFIX}*.md"):
            f.unlink()
            n += 1
    return n


def emit_skills(playbooks, repo_root):
    """Playbooks as skills in .agents/skills, where Antigravity and Codex both look."""
    base = Path(repo_root) / SKILLS_DIR
    remove_skills(repo_root)
    written = []
    for pb in playbooks:
        d = base / f"{PREFIX}{pb['id']}"
        d.mkdir(parents=True, exist_ok=True)
        desc = " ".join(str(pb.get("description", "")).split())
        (d / "SKILL.md").write_text(f"---\nname: {PREFIX}{pb['id']}\ndescription: {json.dumps(desc)}\n---\n\n"
                                    f"{pb['body'].strip()}\n\n<!-- generated by once; edits here are overwritten -->\n")
        written.append(d / "SKILL.md")
    return written


def remove_skills(repo_root):
    import shutil
    base = Path(repo_root) / SKILLS_DIR
    n = 0
    if base.is_dir():
        for d in base.glob(f"{PREFIX}*"):
            shutil.rmtree(d, ignore_errors=True)
            n += 1
    return n
