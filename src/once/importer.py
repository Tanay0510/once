"""Import sessions Claude Code already recorded, so there is history from the first minute.

Experimental: the transcript format is not a documented contract. Lines that do not look as
expected are skipped. Sessions already captured live are left alone.
"""
import json
import os
import time
from datetime import datetime
from pathlib import Path

from . import SCHEMA, paths, redact, repo
from .adapters import claude_code as cc


def projects_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"


def _ts(v) -> float:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except Exception:
        return time.time()


def convert(path: Path) -> tuple[str, str, list[dict]] | None:
    """One transcript file -> (slug, session id, normalized events)."""
    lines = []
    for l in path.read_text(errors="ignore").splitlines():
        try:
            d = json.loads(l)
            if isinstance(d, dict):
                lines.append(d)
        except Exception:
            continue
    cwd = next((d.get("cwd") for d in lines if d.get("cwd")), None)
    sid = next((d.get("sessionId") for d in lines if d.get("sessionId")), path.stem)
    if not cwd or not os.path.isdir(cwd):
        return None
    slug, root = repo.slug(cwd)
    rootS = str(root) if root else None
    evs = [{"event": "session_start", "source": "import", "cwd": cwd, "ts": _ts(lines[0].get("timestamp")) if lines else time.time()}]
    pending = {}
    for d in lines:
        m = d.get("message") if isinstance(d.get("message"), dict) else {}
        ts = _ts(d.get("timestamp"))
        content = m.get("content")
        if d.get("type") == "user" and isinstance(content, str) and content.strip() and not d.get("isMeta"):
            evs.append({"event": "user_prompt", "prompt": content, "ts": ts})
        if not isinstance(content, list):
            continue
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") == "text" and d.get("type") == "user" and c.get("text", "").strip():
                evs.append({"event": "user_prompt", "prompt": c["text"], "ts": ts})
            elif c.get("type") == "tool_use":
                pending[c.get("id")] = c
            elif c.get("type") == "tool_result" and c.get("tool_use_id") in pending:
                use = pending.pop(c["tool_use_id"])
                body = c.get("content")
                text = body if isinstance(body, str) else " ".join(x.get("text", "") for x in body or [] if isinstance(x, dict))
                raw = {"hook_event_name": "PostToolUseFailure" if c.get("is_error") else "PostToolUse",
                       "tool_name": use.get("name"), "tool_input": use.get("input") or {}, "error": text,
                       "tool_response": {}}
                for ev in cc.parse(raw, rootS):
                    evs.append({**ev, "ts": ts})
    if len(evs) < 3:
        return None
    evs.append({"event": "session_end", "reason": "import", "ts": evs[-1]["ts"]})
    return slug, sid, evs


def run(limit: int = 200, only_slug: str | None = None, progress=print) -> dict:
    stats = {"found": 0, "imported": 0, "skipped_existing": 0, "skipped_other": 0}
    base = projects_dir()
    if not base.is_dir():
        return stats
    files = sorted(base.glob("*/*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)[:limit]
    for f in files:
        stats["found"] += 1
        try:
            got = convert(f)
        except Exception:
            got = None
        if not got:
            stats["skipped_other"] += 1
            continue
        slug, sid, evs = got
        if only_slug and slug != only_slug:
            stats["skipped_other"] += 1
            continue
        dest = paths.trajectories(slug) / f"{paths._safe(sid)}.jsonl"
        if dest.exists():
            stats["skipped_existing"] += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "w") as out:
            for ev in evs:
                clean, n = redact.redact(ev)
                clean.update({"schema": SCHEMA, "session_id": sid, "agent": "claude-code", "repo": slug, "imported": True})
                if n:
                    clean["redactions"] = n
                out.write(json.dumps(clean, separators=(",", ":")) + "\n")
        stats["imported"] += 1
    return stats
