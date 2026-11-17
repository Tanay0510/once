"""A small stdio MCP server: playbooks on demand, and feedback without leaving the agent.

Hand-rolled JSON-RPC over newline-delimited stdio so the tool has no extra dependency.
Read-only except for record_blocker and record_feedback, which append to the local session log, and
review_rule and remember_rule, which change rules only on the developer's explicit say-so in the chat.
"""
import json
import os
import sys
import time

from . import SCHEMA, __version__, paths, repo

TOOLS = [
    {"name": "get_playbook",
     "description": "Get a step-by-step playbook for a recurring kind of task in this repository. "
                    "Call with no name to list the playbooks available.",
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}},
    {"name": "list_rules",
     "description": "List the reviewed conventions for this repository, optionally only those that apply to one file path.",
     "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
    {"name": "record_blocker",
     "description": "Record that you are stuck and why, so the developer can review it after the session.",
     "inputSchema": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
    {"name": "record_feedback",
     "description": "Record the developer's correction about what you just did. Use only when the developer asks "
                    "you to flag, note or remember a mistake.",
     "inputSchema": {"type": "object", "properties": {
         "note": {"type": "string", "description": "What should have been done instead"},
         "tag": {"type": "string", "enum": ["wrong", "avoidable", "missing_knowledge", "good"]}},
         "required": ["note"]}},
    {"name": "review_rule",
     "description": "Record the developer's decision on a suggested rule. Call only after the developer "
                    "has answered in the chat. decision is keep, drop, or edit (with their wording in text).",
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"},
         "decision": {"type": "string", "enum": ["keep", "drop", "edit"]},
         "text": {"type": "string", "description": "The developer's wording, for edit"},
         "reason": {"type": "string", "description": "Why, if they said, for drop"}},
         "required": ["id", "decision"]}},
    {"name": "remember_rule",
     "description": "Save a convention for this repository that the developer explicitly asked you to remember "
                    "(for example 'remember to use tox here'). Write it as one short imperative sentence. "
                    "Do not call this on your own initiative.",
     "inputSchema": {"type": "object", "properties": {
         "directive": {"type": "string"},
         "paths": {"type": "array", "items": {"type": "string"},
                   "description": "Globs it applies to; omit for the whole repository"}},
         "required": ["directive"]}},
]


def _current_session(slug: str) -> dict | None:
    d = paths.local() / "sessions"
    best = None
    if d.is_dir():
        for f in d.glob("*.json"):
            try:
                st = json.loads(f.read_text())
            except Exception:
                continue
            if st.get("slug") == slug and (best is None or f.stat().st_mtime > best[0]):
                best = (f.stat().st_mtime, st)
    return best[1] if best else None


def _append(slug: str, st: dict, ev: dict) -> bool:
    from . import hook, redact
    clean, _ = redact.redact(ev)
    clean.update({"schema": SCHEMA, "session_id": st["session_id"], "agent": st.get("agent"), "repo": slug,
                  "ts": time.time()})
    hook._append(slug, st["session_id"], clean)
    return True


def call(name: str, args: dict, cwd: str) -> str:
    from . import index, playbooks
    slug, root = repo.slug(cwd)
    if name == "get_playbook":
        pbs = playbooks.load_all(slug)
        if not args.get("name"):
            return "\n".join(f"- {p['id']}: {p['description']}" for p in pbs) or "No playbooks for this repository yet."
        pb = playbooks.find(slug, args["name"])
        return pb["body"].strip() if pb else f"No playbook named {args['name']!r}."
    if name == "list_rules":
        idx = index.load(slug)
        rs = index.repo_wide(idx) + (index.matching(idx, args["path"]) if args.get("path")
                                     else [r for r in idx["rules"] if not r["repo_wide"]])
        return index.render(rs) or "No rules for this repository yet."
    if name in ("review_rule", "remember_rule"):
        return _rule_call(name, args, slug, root)
    st = _current_session(slug)
    if st is None:
        return "No active session found for this repository; nothing was recorded."
    if name == "record_blocker":
        _append(slug, st, {"event": "blocker", "source": "agent", "note": str(args.get("reason", ""))[:1000]})
        return "Recorded."
    if name == "record_feedback":
        tag = args.get("tag") if args.get("tag") in ("wrong", "avoidable", "missing_knowledge", "good") else "wrong"
        _append(slug, st, {"event": "annotation", "source": "human", "via": "agent", "tag": tag,
                           "note": str(args.get("note", ""))[:1000], "since_last_prompt": True})
        return "Recorded. The developer will see it on that session."
    raise KeyError(name)


def _rule_call(name: str, args: dict, slug: str, root) -> str:
    from . import learn, rules
    from .cli import _refresh
    if name == "review_rule":
        rid = str(args.get("id", "")).strip()
        r = next((x for x in rules.load_all(slug, include_shared=False) if x.get("id") == rid), None)
        if r is None:
            return f"No rule with id {rid!r} in this repository."
        if r.get("status") != "candidate":
            return f"{rid} was already decided ({r.get('status')})."
        d = args.get("decision")
        if d == "drop":
            learn.decide(slug, r, "reject", "developer (in chat)", reason=args.get("reason"))
            _refresh(slug, root)
            return f"Dropped {rid}. It will not be suggested again."
        text = (args.get("text") or "").strip() if d == "edit" else None
        rec = learn.decide(slug, r, "accept", "developer (in chat)", new_text=text or None)
        _refresh(slug, root)
        return f"Kept {rid}" + (" with the developer's wording" if rec["decision"] == "accepted_with_edit" else "") \
            + ". It applies from the next session on."
    directive = " ".join(str(args.get("directive", "")).split())
    if not directive:
        return "Nothing to remember: directive is empty."
    globs_ = [g for g in (args.get("paths") or []) if isinstance(g, str)][:4]
    dup = learn.duplicate_of(slug, directive)
    if dup:
        r = next((x for x in rules.load_all(slug, include_shared=False) if x.get("id") == dup), None)
        if r and r.get("status") == "candidate":  # the same correction was already noted from the chat
            learn.decide(slug, r, "accept", "developer (in chat)", new_text=directive)
            _refresh(slug, root)
            return f"Saved as {dup}. It applies from the next session on."
        return f"Already known as {dup}."
    rule = rules.new_manual(directive, globs_, "developer (in chat)")
    rule["provenance"]["source_kind"] = "chat"
    rule["id"] = rules.unique_id(slug, rule["id"])
    errs = rules.lint(rule, root)
    if errs:
        return "Not saved: " + "; ".join(errs)
    rules.save(slug, rule)
    rules.commit(f"chat: remember {rule['id']}")
    learn.log({"candidate_id": rule["id"], "source_kind": "chat", "repo": slug, "decision": "accepted",
               "edit_ratio": 0.0, "final_text": directive})
    _refresh(slug, root)
    return f"Saved as {rule['id']}. It applies from the next session on."


def handle(msg: dict, cwd: str) -> dict | None:
    mid, method = msg.get("id"), msg.get("method", "")
    if mid is None:
        return None  # a notification
    try:
        if method == "initialize":
            res = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2024-11-05"),
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": "once", "version": __version__}}
        elif method == "ping":
            res = {}
        elif method == "tools/list":
            res = {"tools": TOOLS}
        elif method == "tools/call":
            p = msg.get("params") or {}
            try:
                text, err = call(p.get("name", ""), p.get("arguments") or {}, cwd), False
            except KeyError:
                text, err = f"Unknown tool: {p.get('name')}", True
            res = {"content": [{"type": "text", "text": text}], "isError": err}
        else:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
        return {"jsonrpc": "2.0", "id": mid, "result": res}
    except Exception as e:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}


def main() -> int:
    cwd = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        out = handle(msg, cwd) if isinstance(msg, dict) else None
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
