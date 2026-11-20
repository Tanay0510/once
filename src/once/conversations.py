"""What was said in a session: your prompts and the agent's replies, read from the agent's own transcript.

once's hooks see prompts and tool calls but not the agent's replies, so the replies come from the transcript
each agent writes anyway (Claude Code, Codex, Antigravity), read when a conversation is opened and never
copied into once's store. Tool steps come from once's own record, where flags and failures already are.
Every piece of text is masked for secrets before it is shown. The transcript formats are not documented
contracts: anything unrecognised is skipped, and with no transcript the page falls back to the prompts.
"""
import glob
import json
import os
import re
from datetime import datetime

from . import detectors, redact

_CACHE: dict = {}
MAX_BYTES = 300_000_000  # read line by line; only the text is kept
MAX_TEXT = 20_000


def _ts(s) -> float | None:
    if isinstance(s, (int, float)):
        return float(s) / (1000 if s > 1e12 else 1)
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def transcript_path(evs: list[dict], sid: str, agent: str) -> str | None:
    for ev in reversed(evs):
        p = ev.get("transcript_path")
        if isinstance(p, str) and p and os.path.isfile(p):
            return p
    if agent == "claude-code" and sid and re.fullmatch(r"[\w-]{8,80}", sid):
        hits = glob.glob(os.path.join(os.path.expanduser("~/.claude/projects"), "*", f"{sid}.jsonl"))
        if hits:
            return hits[0]
    if agent == "antigravity" and sid and re.fullmatch(r"[\w-]{8,80}", sid):
        hits = glob.glob(os.path.expanduser(f"~/.gemini/*/brain/{sid}/.system_generated/logs/transcript_full.jsonl"))
        if hits:
            return hits[0]
    return None


def _lines(path: str):
    with open(path, "rb") as f:
        for line in f:
            try:
                d = json.loads(line)
            except Exception:
                continue
            if isinstance(d, dict):
                yield d


def _texts(content, kinds=("text", "input_text", "output_text")) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(c.get("text", "") for c in content
                           if isinstance(c, dict) and c.get("type") in kinds and isinstance(c.get("text"), str)).strip()
    return ""


_NOISE = ("<system-reminder>", "<local-command", "<command-", "Caveat:", "[Request interrupted", "<environment_context>",
          "<user_instructions>", "# AGENTS.md", "<permissions", "<user_shell_command>", "<turn_aborted>")


def _noise(text: str) -> bool:
    t = text.lstrip()
    return not t or t.startswith(_NOISE)


def _command(text: str) -> str | None:
    m = re.search(r"<command-name>\s*(/[^<\s]+)\s*</command-name>(?:.*?<command-args>(.*?)</command-args>)?", text, re.S)
    return (m.group(1) + (" " + m.group(2).strip() if m.group(2) and m.group(2).strip() else "")) if m else None


def read_claude(path: str) -> list[dict]:
    out = []
    for d in _lines(path):
        if d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
            continue
        t, m = d.get("type"), d.get("message") if isinstance(d.get("message"), dict) else {}
        if t == "user":
            content = m.get("content")
            if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
                continue
            text = _texts(content)
            cmd = _command(text) if text.lstrip().startswith("<command-") else None
            if cmd:
                text = cmd
            elif _noise(text):
                continue
            out.append({"role": "user", "text": text, "ts": _ts(d.get("timestamp"))})
        elif t == "assistant":
            text = _texts(m.get("content"), ("text",))
            if text:
                out.append({"role": "agent", "text": text, "ts": _ts(d.get("timestamp")), "mid": m.get("id")})
    return out


def read_codex(path: str) -> list[dict]:
    """Codex rollout files. Newer ones carry event_msg user_message / agent_message, which are what was
    shown in the chat; older ones only response_item messages, which also hold injected context."""
    events, items = [], []
    for d in _lines(path):
        p = d.get("payload") if isinstance(d.get("payload"), dict) else d
        ts = _ts(d.get("timestamp") or p.get("timestamp"))
        if d.get("type") == "event_msg":
            if p.get("type") == "user_message" and isinstance(p.get("message"), str):
                events.append({"role": "user", "text": p["message"], "ts": ts})
            elif p.get("type") == "agent_message" and isinstance(p.get("message"), str):
                events.append({"role": "agent", "text": p["message"], "ts": ts})
        elif p.get("type") == "message" and p.get("role") in ("user", "assistant"):
            text = _texts(p.get("content"))
            if text and not _noise(text):
                items.append({"role": "user" if p["role"] == "user" else "agent", "text": text, "ts": ts})
    return [m for m in (events or items) if not _noise(m["text"])]


_REQ = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)


def read_antigravity(path: str) -> list[dict]:
    out = []
    for d in _lines(path):
        t, content = d.get("type"), d.get("content")
        if t == "USER_INPUT" and d.get("source", "USER_EXPLICIT") == "USER_EXPLICIT" and isinstance(content, str):
            m = _REQ.search(content)
            text = m.group(1) if m else content
            if text.strip():
                out.append({"role": "user", "text": text.strip(), "ts": _ts(d.get("created_at"))})
        elif t == "PLANNER_RESPONSE" and isinstance(content, str) and content.strip():
            out.append({"role": "agent", "text": content.strip(), "ts": _ts(d.get("created_at"))})
    return out


READERS = {"claude-code": read_claude, "codex": read_codex, "antigravity": read_antigravity}


def guess_reader(path: str):
    if path.endswith("transcript_full.jsonl"):
        return read_antigravity
    if "/.codex/" in path or os.path.basename(path).startswith("rollout-"):
        return read_codex
    return read_claude


def messages(path: str | None, agent: str) -> list[dict]:
    """Masked messages, cached until the file changes."""
    if not path or not os.path.isfile(path):
        return []
    try:
        st = os.stat(path)
    except OSError:
        return []
    if st.st_size > MAX_BYTES:
        return []
    key = (path, st.st_mtime, st.st_size)
    if key in _CACHE:
        return _CACHE[key]
    try:
        raw = (READERS.get(agent) or guess_reader(path))(path)
    except Exception:
        raw = []
    out = []
    for m in raw:
        text = redact.redact_text(m["text"][:MAX_TEXT])[0]
        if out and m["role"] == "agent" and out[-1]["role"] == "agent" and m.get("mid") and out[-1].get("mid") == m.get("mid"):
            out[-1]["text"] += "\n\n" + text  # one reply streamed as several entries
            continue
        out.append({**m, "text": text})
    if len(_CACHE) > 400:
        _CACHE.clear()
    _CACHE[key] = out
    return out


def thread(evs: list[dict], sid: str, agent: str) -> tuple[list[dict], bool]:
    """The conversation as items: user, agent and steps (consecutive tool calls folded into one).
    Returns (items, whether the agent's own transcript was found)."""
    path = transcript_path(evs, sid, agent)
    msgs = messages(path, agent)
    have = bool(msgs)
    steps = detectors.steps(evs)
    flags = detectors.detect(evs)
    corrections = [ev.get("text", "") for ev in evs if ev.get("event") == "correction_noted"]
    seq = []
    if not have:  # no transcript: the prompts once recorded, no replies
        msgs = [{"role": "user", "text": redact.redact_text(s.get("prompt", ""))[0], "ts": s.get("ts")}
                for s in steps if s["event"] == "user_prompt"]
    for i, m in enumerate(msgs):
        seq.append((m.get("ts") or 0, 0, i, {"kind": m["role"], "text": m["text"], "ts": m.get("ts")}))
    for s in steps:
        if s["event"] == "tool_result":
            seq.append((s.get("ts") or 0, 1, s["step"], {"kind": "step", "step": s}))
    if not any(x[0] for x in seq if x[1] == 0):  # messages without times: keep transcript order, steps after
        seq = [(0, 0, i, x[3]) for i, x in enumerate(seq) if x[1] == 0] + [x for x in seq if x[1] == 1]
    seq.sort(key=lambda x: (x[0], x[1], x[2]))
    items: list[dict] = []
    for *_, it in seq:
        if it["kind"] == "step":
            if items and items[-1]["kind"] == "steps":
                items[-1]["steps"].append(it["step"])
            else:
                items.append({"kind": "steps", "steps": [it["step"]], "ts": it["step"].get("ts")})
        else:
            if it["kind"] == "user":
                head = it["text"].strip()[:120]
                it["correction"] = any(c and (c[:80] in it["text"] or head[:80] in c) for c in corrections)
            items.append(it)
    for it in items:
        if it["kind"] == "steps":
            nums = {s["step"] for s in it["steps"]}
            it["flags"] = [f for f in flags if nums & set(range(f["step_from"], f["step_to"] + 1))]
            it["failed"] = sum(1 for s in it["steps"] if s.get("ok") is False)
    return items, have


def search_hit(evs: list[dict], sid: str, agent: str, q: str, replies: bool = True) -> bool:
    q = q.lower().strip()
    if not q:
        return True
    if any(q in (ev.get("prompt") or "").lower() for ev in evs if ev.get("event") == "user_prompt"):
        return True
    if replies:
        return any(q in m["text"].lower() for m in messages(transcript_path(evs, sid, agent), agent))
    return False


def as_markdown(items: list[dict], title: str) -> str:
    out = [f"# {title}", ""]
    for it in items:
        if it["kind"] == "user":
            out += ["**You:**", "", it["text"], ""]
        elif it["kind"] == "agent":
            out += ["**Agent:**", "", it["text"], ""]
        else:
            out += ["_" + ", ".join(f"{s.get('tool')}: {s.get('target', '')[:80]}" for s in it["steps"][:8])
                    + (" …" if len(it["steps"]) > 8 else "") + "_", ""]
    return "\n".join(out)


def turns(evs: list[dict]) -> int:
    return sum(1 for ev in evs if ev.get("event") == "user_prompt")

