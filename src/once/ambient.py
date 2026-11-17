"""once, where the developer already is: the agent chat, the status line, notifications, the menu bar, a digest.

The rule for everything here: say something only when there is a decision to make or a result worth
seeing, and say it in a place the developer is already looking. No command needed for the daily path.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import BIN, paths

UI_PORT = 4317


def config() -> dict:
    try:
        return json.loads((paths.home() / "config.json").read_text())
    except Exception:
        return {}


def enabled(key: str, default=True) -> bool:
    v = config().get(key, default)
    return v not in (False, "off", "false", 0)


# ------------------------------------------------------------------ teach by talking

_STRONG = re.compile(
    r"(?i)\b(remember (that|to)\b|remember:|from now on|going forward|next time\b|"
    r"in (this|our) (repo|codebase|project)\b|we (always|never|don'?t)\b|always use\b|never use\b|"
    r"(don'?t|do not) ever\b|you should (always|never)\b|make sure (you )?always\b)")
_LEAD = re.compile(r"(?i)^\s*(no|nope|wrong|stop|don'?t|do not|not that|actually|instead)\b[\s,.!:;-]*")
_ALT = re.compile(r"(?i)\b(use|run|call|prefer|put)\b.+\binstead\b|\binstead of\b|\b(use|run)\b\s+\S+")
# Only interjections are removed from the front; "don't", "never" and "stop using" carry the meaning.
_INTERJECTION = re.compile(
    r"(?i)^\s*((no|nope|wrong|stop|not that|actually|ok(ay)?|hm+|ugh)\s*[,.!:;-]+\s*|(actually|so|also|please|pls|hey)\s+)")
_CHAT_NOISE = re.compile(
    r"(?i)^\s*(please\s+|ok(ay)?[,.]?\s+|hey[,.]?\s+|so\s+|also[,.]?\s+)*"
    r"(remember( that| to|:)?|from now on|going forward|next time|in (this|our) (repo|codebase|project))?[\s,:;-]*")


def is_correction(text: str) -> bool:
    """A conservative test: does this prompt state how things should be done in this repo from now on?"""
    t = (text or "").strip()
    if not 8 <= len(t) <= 500 or "\n\n" in t or t.startswith(("/", "!", "#")):
        return False
    if _STRONG.search(t):
        return True
    if t.rstrip().endswith("?"):
        return False
    return bool(_LEAD.match(t) and _ALT.search(t))


def directive_from(text: str) -> str:
    """'no, use tox instead of pytest' -> 'Use tox instead of pytest.'"""
    t = " ".join((text or "").split())
    for _ in range(3):
        t2 = _INTERJECTION.sub("", t)
        t2 = _CHAT_NOISE.sub("", t2, count=1)
        if t2 == t:
            break
        t = t2
    t = re.sub(r"(?i)^(please|pls)\s+", "", t).strip(" ,;:-")
    if not t:
        return ""
    t = t[0].upper() + t[1:]
    if t[-1] not in ".!":
        t += "."
    return t[:300]


def capture_correction(slug: str, root, text: str, session: str | None, agent: str | None) -> dict | None:
    """Turn a correction typed in the chat into a suggested rule. Never active until the developer says yes."""
    from . import learn, rules
    directive = directive_from(text)
    if len(directive) < 8:
        return None
    dup = learn.duplicate_of(slug, directive)
    if dup:
        return {"duplicate_of": dup}
    rule = {"schema": 1, "id": rules.unique_id(slug, rules.slugify(directive)), "status": "candidate",
            "kind": "negative_constraint" if re.search(r"(?i)\b(never|don't|do not|avoid)\b", directive) else "invariant",
            "scope": {"path_globs": [], "symbol_triggers": [], "agents": ["*"]},
            "rule": {"directive": directive, "rationale": "You said this to the agent during a session."},
            "provenance": {"source_kind": "chat", "source_sessions": [session] if session else [],
                           "agent": agent, "created": time.strftime("%Y-%m-%d"), "chat_text": text.strip()[:500]},
            "enforcement": {"level": "instruct"}}
    if rules.lint(rule, root):
        return None
    rules.save(slug, rule)
    rules.commit(f"chat: suggest {rule['id']}")
    rules.compile_index(slug)
    learn.log({"candidate_id": rule["id"], "source_kind": "chat", "repo": slug, "session": session,
               "outcome": "queued", "draft_text": directive, "lint": "pass"})
    note = paths.local() / "last_capture.json"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(json.dumps({"slug": slug, "id": rule["id"], "text": directive, "ts": time.time()}))
    return rule


# ------------------------------------------------------------------ asking inside the agent

def _asked_file() -> Path:
    return paths.local() / "asked.json"


def review_prompt(idx: dict, session: str, max_items: int = 2, max_asks: int = 2) -> str:
    """Context that asks the agent to put pending suggestions to the developer, at most twice each."""
    pending = idx.get("pending") or []
    if not pending:
        return ""
    try:
        asked = json.loads(_asked_file().read_text())
    except Exception:
        asked = {}
    pick = [p for p in pending if asked.get(p["id"], 0) < max_asks][:max_items]
    if not pick:
        return ""
    for p in pick:
        asked[p["id"]] = asked.get(p["id"], 0) + 1
    try:
        _asked_file().parent.mkdir(parents=True, exist_ok=True)
        _asked_file().write_text(json.dumps(asked))
    except Exception:
        pass
    lines = "\n".join(f"- [{p['id']}] {p['text']}" for p in pick)
    one = len(pick) == 1
    return ("<once-review>\n"
            f"The developer's rule-learning tool (`once`) has {'a suggested rule' if one else 'suggested rules'} for this repository "
            f"waiting for their decision:\n{lines}\n"
            "In your first reply, before anything else, ask the developer in one short line per rule whether to keep "
            "it (yes, no, or their own wording). Then continue with their request. When they answer, call the "
            "once tool review_rule with the id and their decision. If that tool is not available, tell them "
            f"they can run `{BIN} review`. Ask only once in this session.\n</once-review>")


# ------------------------------------------------------------------ status line

def statusline(raw: dict) -> str:
    """One line for the bottom of Claude Code. Must be fast and must never fail loudly."""
    from . import index, repo
    cwd = ((raw.get("workspace") or {}).get("current_dir") or raw.get("cwd") or os.getcwd())
    slug, _ = repo.slug(cwd)
    if paths.off_flag().exists() or paths.off_flag(slug).exists():
        return "once ○ paused"
    idx = index.load(slug)
    n_rules, pending = len(idx.get("rules", [])), len(idx.get("pending") or [])
    parts = [f"once ● {n_rules} rule{'s' if n_rules != 1 else ''}"]
    if pending:
        parts.append(f"{pending} to review")
    sid = raw.get("session_id")
    if sid:
        try:
            from . import detectors
            f = paths.trajectories(slug) / f"{paths._safe(sid)}.jsonl"
            if f.exists() and f.stat().st_size < 4_000_000:
                evs = [{**json.loads(l), "seq": i} for i, l in enumerate(f.read_text().splitlines()) if l.strip()]
                given = {r for e in evs if e.get("event") == "rule_injected" for r in e.get("rule_ids", [])}
                flags = detectors.detect(evs)
                if given:
                    parts[0] = f"once ● {len(given)} of {n_rules} rules in use"
                if flags:
                    parts.append(f"{len(flags)} flagged")
        except Exception:
            pass
    try:
        cap = json.loads((paths.local() / "last_capture.json").read_text())
        if cap.get("slug") == slug and time.time() - cap.get("ts", 0) < 180:
            text = cap["text"] if len(cap["text"]) <= 40 else cap["text"][:39] + "…"
            parts.append(f"noted: {text} (I'll ask you next session)")
    except Exception:
        pass
    return " · ".join(parts)


def statusline_main() -> int:
    """`python -m once.statusline`: wraps any status line the developer already had."""
    data = sys.stdin.read()
    try:
        raw = json.loads(data) if data.strip() else {}
    except Exception:
        raw = {}
    ours = ""
    try:
        ours = statusline(raw if isinstance(raw, dict) else {})
    except Exception:
        ours = ""
    theirs = ""
    wrap = config().get("statusline_wrap")
    if wrap:
        try:
            r = subprocess.run(wrap, shell=True, input=data, capture_output=True, text=True, timeout=3)
            theirs = (r.stdout or "").splitlines()[0] if (r.stdout or "").strip() else ""
        except Exception:
            theirs = ""
    sys.stdout.write(" │ ".join(x for x in (theirs, ours) if x))
    return 0


# ------------------------------------------------------------------ notifications

def notify(title: str, body: str, open_url: str | None = None) -> bool:
    """A desktop notification, only for things that need a decision. Silent when switched off or unsupported."""
    if not enabled("notifications"):
        return False
    log = os.environ.get("ONCE_NOTIFY_LOG")
    if log:  # tests, and anyone who wants a record
        with open(log, "a") as f:
            f.write(json.dumps({"title": title, "body": body, "url": open_url}) + "\n")
        return True
    try:
        if sys.platform == "darwin":
            tn = shutil.which("terminal-notifier")
            if tn:
                cmd = [tn, "-title", title, "-message", body, "-group", "once"]
                if open_url:
                    cmd += ["-open", open_url]
            else:
                q = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
                cmd = ["osascript", "-e", f'display notification "{q(body)}" with title "{q(title)}"']
        elif shutil.which("notify-send"):
            cmd = ["notify-send", "-a", "once", title, body]
        else:
            return False
        subprocess.run(cmd, capture_output=True, timeout=5)
        return True
    except Exception:
        return False


# ------------------------------------------------------------------ the dashboard in the background

def ui_running(port: int = UI_PORT) -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def ensure_ui(port: int = UI_PORT, cwd: str | None = None) -> str:
    """Start the dashboard detached if it is not already running. Returns its address."""
    if not ui_running(port):
        subprocess.Popen([sys.executable, "-m", "once.cli", "ui", "--no-open", "--port", str(port)],
                         cwd=cwd or None, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(30):
            if ui_running(port):
                break
            time.sleep(0.1)
    return f"http://127.0.0.1:{port}"


# ------------------------------------------------------------------ menu bar (SwiftBar / xbar)

def _all_pending() -> dict:
    out = {}
    d = paths.local() / "index"
    for f in sorted(d.glob("*.json")) if d.is_dir() else []:
        try:
            idx = json.loads(f.read_text())
        except Exception:
            continue
        if idx.get("pending"):
            out[idx.get("slug") or f.stem] = idx["pending"]
    return out


def week_summary(days: int = 7, repo_slug: str | None = None) -> dict:
    from . import detectors, store
    con = store.connect()
    store.sync(con)
    since = time.time() - days * 86400
    q = "SELECT * FROM sessions WHERE started >= ?" + (" AND repo = ?" if repo_slug else "")
    rows = con.execute(q, (since, repo_slug) if repo_slug else (since,)).fetchall()
    steps = flagged = 0
    kinds, targets, spend, rules_used = {}, {}, 0.0, {}
    last = None
    for r in rows:
        evs = store.events(con, r["id"])
        fl = detectors.detect(evs)
        st = len(detectors.steps(evs))
        steps += st
        flagged += round(detectors.avoidable_ratio(evs, fl) * st)
        for f in fl:
            kinds[f["detector"]] = kinds.get(f["detector"], 0) + 1
            key = (f["detector"], (f.get("note") or "")[:60])
            targets[key] = targets.get(key, 0) + 1
        for e in evs:
            if e.get("event") == "session_end" and isinstance(e.get("cost_usd"), (int, float)):
                spend += e["cost_usd"]
            if e.get("event") == "rule_injected":
                for rid in e.get("rule_ids", []):
                    rules_used[rid] = rules_used.get(rid, 0) + 1
        if last is None or (r["started"] or 0) > (last["started"] or 0):
            last = r
    con.close()
    top = max(targets.items(), key=lambda kv: kv[1])[0] if targets else None
    return {"days": days, "sessions": len(rows), "steps": steps, "avoidable": flagged,
            "avoidable_share": flagged / steps if steps else 0.0, "by_detector": kinds,
            "top_repeat": {"detector": top[0], "what": top[1], "times": targets[top]} if top else None,
            "spend": spend, "rules_used": rules_used,
            "last": dict(last) if last else None}


def menubar(meta_bin: str | None = None) -> str:
    """SwiftBar/xbar plugin output. The first line is the menu bar title; the rest is the menu."""
    meta_bin = meta_bin or shutil.which(BIN) or f"{sys.executable} -m once.cli"
    parts = meta_bin.split()
    def action(*args, terminal=False, refresh=True):
        ps = " ".join(f"param{i}={a}" for i, a in enumerate(parts[1:] + list(args), 1))
        return f"bash={parts[0]} {ps} terminal={'true' if terminal else 'false'} refresh={'true' if refresh else 'false'}"
    paused = paths.off_flag().exists()
    pending = _all_pending()
    n = sum(len(v) for v in pending.values())
    title = "◇ once paused" if paused else (f"◆ {n}" if n else "◆")
    out = [f"{title} | sfimage=brain" if sys.platform == "darwin" else title, "---"]
    if n:
        out.append(f"{n} rule{'s' if n != 1 else ''} waiting for you")
        from . import repo
        for slug, items in pending.items():
            out.append(f"{slug}  ({len(items)})")
            root = repo.known_root(slug)
            for p in items[:5]:
                text = p["text"] if len(p["text"]) <= 60 else p["text"][:59] + "…"
                out.append(f"--{text}")
            out.append(f"--Review in browser | href=http://127.0.0.1:{UI_PORT}/review?repo={slug}")
            if root:
                out.append(f"--Review in terminal | bash=/bin/sh param1=-c param2=\"cd '{root}' && {meta_bin} review\" terminal=true")
        out.append("---")
    else:
        out += ["Nothing to review", "---"]
    try:
        w = week_summary(7)
        out.append(f"This week: {w['sessions']} session{'s' if w['sessions'] != 1 else ''}, "
                   f"{100 * w['avoidable_share']:.0f}% of steps avoidable")
        if w["last"]:
            l = w["last"]
            what = (l.get("prompt") or "session")[:50]
            out.append(f"Last: {what}  · {l.get('flags') or 0} flagged | href=http://127.0.0.1:{UI_PORT}/session/{l['id']}")
    except Exception:
        pass
    out += ["---", f"Open dashboard | {action('ui', '--detach')}",
            f"Weekly digest | {action('digest', terminal=True, refresh=False)}",
            f"Resume everywhere | {action('on', '--everywhere')}" if paused
            else f"Pause everywhere | {action('off', '--everywhere')}",
            "Refresh | refresh=true"]
    return "\n".join(out) + "\n"


def menubar_dirs() -> list[Path]:
    found = []
    try:
        r = subprocess.run(["defaults", "read", "com.ameba.SwiftBar", "PluginDirectory"], capture_output=True,
                           text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            found.append(Path(r.stdout.strip()).expanduser())
    except Exception:
        pass
    xbar = Path.home() / "Library/Application Support/xbar/plugins"
    if xbar.is_dir():
        found.append(xbar)
    if os.environ.get("ONCE_MENUBAR_DIR"):
        found.insert(0, Path(os.environ["ONCE_MENUBAR_DIR"]))
    return found


PLUGIN_NAME = "once.1m.sh"


def install_menubar() -> Path | None:
    dirs = menubar_dirs()
    if not dirs:
        return None
    d = dirs[0]
    d.mkdir(parents=True, exist_ok=True)
    f = d / PLUGIN_NAME
    f.write_text(f"#!/bin/sh\n# once menu bar plugin. Remove with: {BIN} menubar --uninstall\n"
                 f"exec '{sys.executable}' -m once.cli menubar\n")
    f.chmod(0o755)
    return f


def uninstall_menubar() -> list[Path]:
    gone = []
    for d in menubar_dirs():
        f = d / PLUGIN_NAME
        if f.exists():
            f.unlink()
            gone.append(f)
    return gone


# ------------------------------------------------------------------ weekly digest

def digest_text(days: int = 7) -> tuple[str, dict]:
    from . import index, repo
    w = week_summary(days)
    lines = [f"once: your agents this {'week' if days == 7 else f'{days} days'}", ""]
    if not w["sessions"]:
        lines.append("No sessions recorded.")
        return "\n".join(lines), w
    lines.append(f"- {w['sessions']} sessions, {w['steps']} steps, {w['avoidable']} of them avoidable "
                 f"({100 * w['avoidable_share']:.0f}%)")
    if w["spend"]:
        lines.append(f"- About ${w['spend']:.2f} spent by the agents")
    if w["top_repeat"]:
        t = w["top_repeat"]
        lines.append(f"- Most repeated problem: {t['detector'].replace('_', ' ')}"
                     + (f" ({t['what']})" if t["what"] else "") + f", {t['times']} times")
    used = sorted(w["rules_used"].items(), key=lambda kv: -kv[1])[:3]
    if used:
        lines.append("- Rules given most: " + ", ".join(f"{rid} ({n})" for rid, n in used))
    never = []
    for slug in {(w['last'] or {}).get('repo')} - {None}:
        ids = [r["id"] for r in index.load(slug).get("rules", [])]
        never = [i for i in ids if i not in w["rules_used"]]
    if never:
        lines.append(f"- Never given this week: {', '.join(never[:5])}")
    n = sum(len(v) for v in _all_pending().values())
    if n:
        lines.append(f"- {n} suggested rule{'s' if n != 1 else ''} waiting. Your agent will ask you, or run `{BIN} review`.")
    return "\n".join(lines), w


def _digest_state() -> Path:
    return paths.local() / "digest_state.json"


def digest_due(days: int = 7) -> bool:
    if config().get("digest", "weekly") in ("off", False):
        return False
    try:
        last = json.loads(_digest_state().read_text()).get("last", 0)
    except Exception:
        # first run: start the clock now rather than sending an empty digest on install
        try:
            _digest_state().parent.mkdir(parents=True, exist_ok=True)
            _digest_state().write_text(json.dumps({"last": time.time()}))
        except Exception:
            pass
        return False
    return time.time() - last >= days * 86400


def send_digest(text: str) -> list[str]:
    """Write the digest to a file, post it to Slack if a webhook is set, and notify. Returns where it went."""
    went = []
    d = paths.home() / "digests"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{time.strftime('%Y-%m-%d')}.md"
    f.write_text(text + "\n")
    went.append(str(f))
    hook = config().get("slack_webhook")
    if hook:
        try:
            import urllib.request
            req = urllib.request.Request(hook, data=json.dumps({"text": text}).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
            went.append("Slack")
        except Exception as e:
            went.append(f"Slack failed: {type(e).__name__}")
    first = next((l for l in text.splitlines()[2:] if l.strip()), "")
    if notify("once: weekly digest", first.lstrip("- "), f"file://{f}"):
        went.append("notification")
    _digest_state().parent.mkdir(parents=True, exist_ok=True)
    _digest_state().write_text(json.dumps({"last": time.time(), "file": str(f)}))
    return went
