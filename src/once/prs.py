"""Which pull request each session led to.

Signals, strongest first, so a guess never looks like a fact:
  by hand      you linked it on the session page
  gh pr create the agent created the PR and its output printed the link
  branch       the session worked on the PR's branch (asks `gh`, cached)
  commit       a commit made during the session is in the PR
  tool output  a PR link the agent printed some other way (gh pr view, an MCP call)

The first and the last two need nothing but what the hooks already record, so they work offline
and with any agent. PR titles, state and checks come from `gh`, when it is installed and signed in.
"""
import json
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime

from . import paths

PR_URL = re.compile(r"^https://github\.com/([^/]+/[^/]+)/pull/(\d+)$")
DEFAULT_BRANCHES = {"", "main", "master", "develop", "dev", "trunk", "HEAD"}
MAX_AGE = 300
FIELDS = "number,title,state,url,headRefName,createdAt,mergedAt,closedAt,additions,deletions,statusCheckRollup,commits"
_busy: set = set()
_lock = threading.Lock()


# ---------------------------------------------------------------- links you made by hand

def _links_file():
    return paths.local() / "pr_links.json"


def manual() -> dict:
    try:
        d = json.loads(_links_file().read_text())
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def parse_ref(text: str, slug: str | None) -> str | None:
    """A PR link, or a bare number / #number for the session's own GitHub repo."""
    t = (text or "").strip()
    m = re.match(r"^#?(\d+)$", t)
    if m and slug and slug.startswith("github.com/"):
        return f"https://{slug}/pull/{int(m.group(1))}"
    m = re.match(r"^(?:https?://)?github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)", t)
    return f"https://github.com/{m.group(1)}/pull/{m.group(2)}" if m else None


def set_manual(sid: str, url: str | None) -> None:
    """url None: forget a link made by hand. url "": this session has no PR, whatever the signals say."""
    d = manual()
    if url is None:
        d.pop(sid, None)
    else:
        d[sid] = url
    f = _links_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(d, indent=1))


# ---------------------------------------------------------------- what GitHub says, through gh

def gh_available() -> bool:
    return shutil.which("gh") is not None


def _cache_file(slug: str):
    return paths.local() / "prs" / (re.sub(r"[^\w.-]+", "_", slug) + ".json")


def cached(slug: str) -> tuple[list[dict], float | None]:
    try:
        d = json.loads(_cache_file(slug).read_text())
        return d.get("prs") or [], d.get("fetched")
    except Exception:
        return [], None


def last_error(slug: str) -> tuple[str, float] | None:
    """What gh said the last time a refresh failed, so the page can say it instead of waiting forever."""
    try:
        d = json.loads(_cache_file(slug).read_text())
        return (d["error"], d.get("error_at") or 0) if d.get("error") else None
    except Exception:
        return None


def _gh_list(slug: str, limit: int, fields: str):
    return subprocess.run(["gh", "pr", "list", "--repo", slug.split("/", 1)[1], "--state", "all",
                           "--limit", str(limit), "--json", fields], capture_output=True, text=True, timeout=30)


def _save(slug: str, **d) -> None:
    f = _cache_file(slug)
    f.parent.mkdir(parents=True, exist_ok=True)
    old = {}
    try:
        old = json.loads(f.read_text())
    except Exception:
        pass
    f.write_text(json.dumps({**old, **d}))


def fetch(slug: str, limit: int = 40) -> tuple[bool, str]:
    """Ask gh for the repo's recent PRs and cache them. Returns (ok, message)."""
    if not slug.startswith("github.com/"):
        return False, "not a GitHub repo"
    if not gh_available():
        return False, "gh is not installed"
    try:
        r = _gh_list(slug, limit, FIELDS)
        if r.returncode != 0 and "exceeds the maximum" in r.stderr:  # commit lists make the query too big: drop them
            r = _gh_list(slug, limit, FIELDS.replace(",commits", ""))
    except Exception as e:
        _save(slug, error=f"gh failed: {e}"[:200], error_at=time.time())
        return False, f"gh failed: {e}"
    if r.returncode != 0:
        msg = (r.stderr.strip().splitlines() or ["gh failed"])[-1][:200]
        _save(slug, error=msg, error_at=time.time())
        return False, msg
    try:
        raw = json.loads(r.stdout)
    except Exception:
        return False, "gh returned something that is not JSON"
    prs = []
    for p in raw:
        prs.append({"number": p.get("number"), "title": p.get("title") or "", "state": (p.get("state") or "").lower(),
                    "url": p.get("url"), "head": p.get("headRefName") or "", "created": _ts(p.get("createdAt")),
                    "merged": _ts(p.get("mergedAt")), "closed": _ts(p.get("closedAt")),
                    "additions": p.get("additions"), "deletions": p.get("deletions"),
                    "checks": checks_state(p.get("statusCheckRollup")),
                    "commits": [c.get("oid") for c in (p.get("commits") or []) if isinstance(c, dict) and c.get("oid")][:250]})
    _cache_file(slug).parent.mkdir(parents=True, exist_ok=True)
    _cache_file(slug).write_text(json.dumps({"fetched": time.time(), "prs": prs}))
    return True, f"{len(prs)} PRs"


def refresh_async(slug: str) -> bool:
    """Refresh in the background when the cache is older than MAX_AGE. The page never waits on GitHub."""
    _, at = cached(slug)
    err = last_error(slug)
    if err and time.time() - err[1] < MAX_AGE:
        return False  # it failed a moment ago; the page shows why, and "check now" retries
    if at and time.time() - at < MAX_AGE or not slug.startswith("github.com/") or not gh_available():
        return False
    with _lock:
        if slug in _busy:
            return False
        _busy.add(slug)

    def run():
        try:
            fetch(slug)
        finally:
            with _lock:
                _busy.discard(slug)
    threading.Thread(target=run, daemon=True).start()
    return True


def _ts(s) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def checks_state(rollup) -> str:
    if not isinstance(rollup, list) or not rollup:
        return ""
    states = [str(c.get("conclusion") or c.get("state") or c.get("status") or "").upper() for c in rollup if isinstance(c, dict)]
    if any(s in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE") for s in states):
        return "failing"
    if any(s in ("", "PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING") for s in states):
        return "pending"
    return "passing"


# ---------------------------------------------------------------- what a session did

def session_signals(evs: list[dict]) -> dict:
    created, seen, branches, heads = [], [], [], []
    for ev in evs:
        kind = ev.get("event")
        if kind in ("session_start", "git_state"):
            b = ev.get("branch") or ""
            if b and b not in branches:
                branches.append(b)
            h = ev.get("commit") if kind == "session_start" else ev.get("head")
            if h:
                heads.append(h)
        elif kind == "tool_result" and ev.get("pr_urls"):
            for u in ev["pr_urls"]:
                (created if ev.get("pr_created") else seen).append(u)
    return {"created": list(dict.fromkeys(created)), "seen": [u for u in dict.fromkeys(seen) if u not in created],
            "branches": branches, "start": heads[0] if heads else "", "end": heads[-1] if heads else ""}


def session_commits(root, start: str, end: str) -> set:
    if not root or not start or not end or start == end:
        return set()
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-list", "--max-count=200", f"{start}..{end}"],
                           capture_output=True, text=True, timeout=5)
        return set(r.stdout.split()) if r.returncode == 0 else set()
    except Exception:
        return set()


def from_url(url: str) -> dict:
    m = PR_URL.match(url or "")
    return {"number": int(m.group(2)) if m else None, "url": url, "title": "", "state": "", "head": "",
            "repo": f"github.com/{m.group(1).lower()}" if m else ""}


def link(slug: str, sid: str, evs: list[dict], started: float | None, prs: list[dict], root=None,
         by_hand: dict | None = None) -> tuple[dict | None, str]:
    """(PR, how it was linked) for one session, or (None, "")."""
    by_url = {p["url"].lower(): p for p in prs if p.get("url")}
    by_hand = manual() if by_hand is None else by_hand
    if sid in by_hand:
        u = by_hand[sid]
        return ((by_url.get(u.lower()) or from_url(u)), "by hand") if u else (None, "")
    sig = session_signals(evs)
    if sig["created"]:
        u = sig["created"][-1]
        return by_url.get(u.lower()) or from_url(u), "gh pr create"
    work = [b for b in sig["branches"] if b not in DEFAULT_BRANCHES]
    if work:
        cands = sorted((p for p in prs if p.get("head") in work), key=lambda p: p.get("created") or 0)
        if cands:  # the same branch name can be reused: take the first PR opened after the session began
            after = [p for p in cands if (p.get("created") or 0) >= (started or 0) - 3600]
            return (after[0] if after else cands[-1]), "branch"
    mine = session_commits(root, sig["start"], sig["end"])
    if mine:
        for p in prs:
            if mine & set(p.get("commits") or []):
                return p, "commit"
    own = [u for u in sig["seen"] if from_url(u)["repo"] == slug]
    if own:
        return by_url.get(own[-1].lower()) or from_url(own[-1]), "tool output"
    return None, ""


def links(slug: str, sid: str, evs: list[dict], started: float | None, prs: list[dict], root=None,
          by_hand: dict | None = None) -> list[tuple[dict, str]]:
    """Every PR this session led to: a long session can create several. Falls back to the single best link."""
    by_hand = manual() if by_hand is None else by_hand
    if sid not in by_hand:
        created = session_signals(evs)["created"]
        if len(created) > 1:
            by_url = {p["url"].lower(): p for p in prs if p.get("url")}
            return [(by_url.get(u.lower()) or from_url(u), "gh pr create") for u in created]
    pr, how = link(slug, sid, evs, started, prs, root, by_hand)
    return [(pr, how)] if pr else []
