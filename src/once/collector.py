"""The central collector: one small server a team runs, which every developer's `once` reports to.

It answers what no single machine can: across people and repos, which agents waste the most steps, which rules
are given and broken where, and how the suggestions people review fare (the outer loop's ground, pooled).

What leaves a machine, and only after `once collector connect`:
  - per session: a salted hash of its id, repo, agent, start time, step count, flags by detector, avoidable
    ratio, the ids of the rules given and broken, the folders flagged steps touched, outcome, cost and tokens
  - per rule: id, wording, level, status, how often given and ignored
  - per review decision: source, decision, edit ratio, pipeline version
Never: prompts, commands, error text, file contents or paths below the folder level. The server stores what it
receives in one SQLite file. It binds to 127.0.0.1 unless told otherwise; put it behind TLS for a team.
"""
import hashlib
import html
import json
import os
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, detectors, paths, rules, store

MAX_BODY = 8 * 1024 * 1024
PUSH_EVERY = 300  # seconds between automatic pushes from the session-end hook

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions(dev TEXT, sid TEXT, repo TEXT, agent TEXT, started REAL, steps INTEGER,
    flags TEXT, avoidable REAL, rules_given TEXT, broken TEXT, folders TEXT, verified INTEGER, cost REAL,
    tokens INTEGER, received REAL, PRIMARY KEY(dev, sid));
CREATE TABLE IF NOT EXISTS rules(dev TEXT, repo TEXT, id TEXT, directive TEXT, level TEXT, status TEXT,
    scope_level TEXT, given INTEGER, ignored INTEGER, received REAL, PRIMARY KEY(dev, repo, id));
CREATE TABLE IF NOT EXISTS decisions(dev TEXT, key TEXT, repo TEXT, source TEXT, decision TEXT, edit_ratio REAL,
    pipeline TEXT, ts REAL, PRIMARY KEY(dev, key));
CREATE TABLE IF NOT EXISTS devs(dev TEXT PRIMARY KEY, version TEXT, last_push REAL);
"""


# ================================================================ server

def _db(data: Path) -> sqlite3.Connection:
    data.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(data / "collector.db", timeout=10, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def server_token(data: Path, given: str | None = None) -> str:
    """The token clients send. Taken from --token or ONCE_COLLECTOR_TOKEN, else made once and kept in the data dir."""
    tok = given or os.environ.get("ONCE_COLLECTOR_TOKEN")
    f = data / "token"
    if tok:
        return tok
    if f.exists():
        return f.read_text().strip()
    data.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(24)
    f.write_text(tok)
    os.chmod(f, 0o600)
    return tok


def ingest(con, body: dict) -> dict:
    dev = str(body.get("developer") or "")[:80]
    if not dev:
        raise ValueError("developer is missing")
    now = time.time()
    n = {"sessions": 0, "rules": 0, "decisions": 0}
    for s in body.get("sessions") or []:
        con.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (dev, str(s["sid"])[:64], str(s.get("repo", ""))[:200], str(s.get("agent", ""))[:40],
                     float(s.get("started") or 0), int(s.get("steps") or 0), json.dumps(s.get("flags") or {}),
                     float(s.get("avoidable") or 0), json.dumps(s.get("rules_given") or []),
                     json.dumps(s.get("broken") or []), json.dumps(s.get("folders") or []),
                     None if s.get("verified") is None else int(bool(s["verified"])),
                     float(s.get("cost") or 0), int(s.get("tokens") or 0), now))
        n["sessions"] += 1
    for r in body.get("rules") or []:
        con.execute("INSERT OR REPLACE INTO rules VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (dev, str(r.get("repo", ""))[:200], str(r["id"])[:64], str(r.get("directive", ""))[:400],
                     str(r.get("level", "instruct")), str(r.get("status", "")), str(r.get("scope_level", "repo")),
                     int(r.get("given") or 0), int(r.get("ignored") or 0), now))
        n["rules"] += 1
    for d in body.get("decisions") or []:
        con.execute("INSERT OR REPLACE INTO decisions VALUES(?,?,?,?,?,?,?,?)",
                    (dev, str(d["key"])[:64], str(d.get("repo", ""))[:200], str(d.get("source", "")),
                     str(d.get("decision", "")), d.get("edit_ratio"), str(d.get("pipeline", "")), float(d.get("ts") or 0)))
        n["decisions"] += 1
    con.execute("INSERT OR REPLACE INTO devs VALUES(?,?,?)", (dev, str(body.get("version", ""))[:20], now))
    con.commit()
    return n


def summary(con, repo: str | None = None, days: int = 30) -> dict:
    since = time.time() - days * 86400
    q = "SELECT * FROM sessions WHERE started >= ?" + (" AND repo = ?" if repo else "")
    rows = con.execute(q, (since, repo) if repo else (since,)).fetchall()
    by_dev, by_agent, by_repo, hot, dets, broken = {}, {}, {}, {}, {}, {}
    for r in rows:
        for key, d in ((r["dev"], by_dev), (r["agent"], by_agent), (r["repo"], by_repo)):
            x = d.setdefault(key, {"sessions": 0, "avoidable": 0.0, "flags": 0, "verified": 0, "checked": 0, "cost": 0.0})
            x["sessions"] += 1
            x["avoidable"] += r["avoidable"]
            x["flags"] += sum(json.loads(r["flags"]).values())
            x["cost"] += r["cost"] or 0
            if r["verified"] is not None:
                x["checked"] += 1
                x["verified"] += r["verified"]
        for det, k in json.loads(r["flags"]).items():
            dets[det] = dets.get(det, 0) + k
        for f in json.loads(r["folders"]):
            hot[(r["repo"], f)] = hot.get((r["repo"], f), 0) + 1
        for rid in json.loads(r["broken"]):
            broken[rid] = broken.get(rid, 0) + 1
    for d in (by_dev, by_agent, by_repo):
        for x in d.values():
            x["avoidable"] = round(x["avoidable"] / x["sessions"], 3) if x["sessions"] else 0
            x["verified_rate"] = round(x["verified"] / x["checked"], 3) if x["checked"] else None
            x["cost"] = round(x["cost"], 2)
    rq = "SELECT id, repo, directive, level, scope_level, SUM(given) g, SUM(ignored) i, COUNT(DISTINCT dev) devs " \
         "FROM rules WHERE status='active'" + (" AND repo=?" if repo else "") + " GROUP BY repo, id ORDER BY g DESC LIMIT 50"
    rule_rows = [dict(x) for x in con.execute(rq, (repo,) if repo else ())]
    dq = "SELECT source, decision, COUNT(*) n FROM decisions" + (" WHERE repo=?" if repo else "") + " GROUP BY source, decision"
    dec = {}
    for x in con.execute(dq, (repo,) if repo else ()):
        dec.setdefault(x["source"], {})[x["decision"]] = x["n"]
    return {"days": days, "repo": repo, "sessions": len(rows), "developers": by_dev, "agents": by_agent, "repos": by_repo,
            "detectors": dets, "hotspots": [{"repo": k[0], "folder": k[1], "sessions": v}
                                            for k, v in sorted(hot.items(), key=lambda kv: -kv[1])[:15]],
            "rules": rule_rows, "broken": broken, "decisions": dec,
            "last_push": {x["dev"]: x["last_push"] for x in con.execute("SELECT dev, last_push FROM devs")}}


def _page(sm: dict, token_q: str) -> str:
    e = lambda s: html.escape(str(s if s is not None else ""))
    pct = lambda v: "–" if v is None else f"{100 * v:.0f}%"
    css = ("body{margin:0;background:#0B0B0C;color:#F3F1EC;font:14px/1.5 system-ui,sans-serif}main{max-width:1200px;margin:0 auto;"
           "padding:24px}h1{font-size:20px}h2{font-size:15px;margin:28px 0 8px;color:#A9A59C;font-weight:500}table{width:100%;"
           "border-collapse:collapse;font-size:13.5px}th,td{text-align:left;padding:7px 10px;border-bottom:1px solid "
           "rgba(243,241,236,.08)}th{color:#6F6C66;font-weight:500}td.n{font-family:ui-monospace,Menlo,monospace}"
           ".rose{color:#F08A7E}.amb{color:#E9A23B}a{color:#E9A23B}.sub{color:#6F6C66}")
    def table(head, rows):
        return ("<table><tr>" + "".join(f"<th>{h}</th>" for h in head) + "</tr>"
                + "".join("<tr>" + "".join(f"<td{' class=n' if i else ''}>{c}</td>" for i, c in enumerate(r)) + "</tr>" for r in rows)
                + "</table>") if rows else '<p class="sub">Nothing yet.</p>'
    group = lambda d, label: table([label, "Sessions", "Avoidable", "Flags", "Verified", "Cost"], [
        (e(k), v["sessions"], pct(v["avoidable"]), v["flags"], pct(v["verified_rate"]), f"${v['cost']:.2f}" if v["cost"] else "–")
        for k, v in sorted(d.items(), key=lambda kv: -kv[1]["sessions"])])
    repos = "".join(f' · <a href="/?repo={urllib.parse.quote(r)}{token_q}">{e(r)}</a>' for r in sm["repos"])
    return (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>once · team</title><style>{css}</style></head><body><main>"
            f"<h1><span class=amb>once</span> team · last {sm['days']} days</h1>"
            f"<p class=sub>{sm['sessions']} sessions from {len(sm['developers'])} developers"
            f"{' in ' + e(sm['repo']) if sm['repo'] else ''}. <a href='/{token_q.replace('&', '?', 1)}'>all repos</a>{repos}</p>"
            f"<h2>By agent</h2>{group(sm['agents'], 'Agent')}"
            f"<h2>By developer</h2>{group(sm['developers'], 'Developer')}"
            f"<h2>By repo</h2>{group(sm['repos'], 'Repo')}"
            "<h2>Where agents struggle</h2>" + table(["Repo", "Folder", "Sessions with flags"], [
                (e(h["repo"]), e(h["folder"]), h["sessions"]) for h in sm["hotspots"]])
            + "<h2>Rules across the team</h2>" + table(["Rule", "Repo", "Level", "Given", "Ignored", "Developers"], [
                (f"{e(r['id'])}<br><span class=sub>{e(r['directive'])}</span>", e(r["repo"]), e(r["level"]), r["g"],
                 f"<span class=rose>{r['i']}</span>" if r["i"] else 0, r["devs"]) for r in sm["rules"]])
            + "<h2>How suggestions fare</h2>" + table(["Source", "Accepted", "Edited", "Rejected"], [
                (e(s), d.get("accepted", 0), d.get("accepted_with_edit", 0), d.get("rejected", 0)) for s, d in sm["decisions"].items()])
            + "<p class=sub style='margin-top:28px'>Only counts, rule wording, folders and outcomes reach this server; "
            "never prompts, commands, error text or file contents.</p></main></body></html>")


def make_handler(con, token: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authed(self, q=None) -> bool:
            got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip() or (q or {}).get("token", "")
            return bool(got) and secrets.compare_digest(got, token)

        def do_GET(self):
            u = urllib.parse.urlparse(self.path)
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            if u.path == "/healthz":
                return self._send(200, {"ok": True, "version": __version__})
            if not self._authed(q):
                return self._send(401, {"error": "a token is needed: Authorization: Bearer <token>, or ?token= in a browser"})
            if u.path == "/v1/summary":
                return self._send(200, summary(con, q.get("repo"), int(q.get("days", 30))))
            if u.path == "/":
                tq = f"&token={urllib.parse.quote(q['token'])}" if q.get("token") else ""
                return self._send(200, _page(summary(con, q.get("repo"), int(q.get("days", 30))), tq), "text/html; charset=utf-8")
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authed():
                return self._send(401, {"error": "a token is needed"})
            if self.path != "/v1/push":
                return self._send(404, {"error": "not found"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._send(413, {"error": "too large"})
            try:
                return self._send(200, ingest(con, json.loads(self.rfile.read(n) or b"{}")))
            except (ValueError, KeyError, TypeError) as ex:
                return self._send(400, {"error": str(ex)})
    return H


def serve(data: Path, host: str = "127.0.0.1", port: int = 4400, token: str | None = None):
    con = _db(data)
    srv = ThreadingHTTPServer((host, port), make_handler(con, server_token(data, token)))
    srv.daemon_threads = True
    return srv


# ================================================================ client

def _cfg() -> dict:
    try:
        return json.loads((paths.home() / "config.json").read_text())
    except Exception:
        return {}


def _save_cfg(c: dict):
    f = paths.home() / "config.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(c, indent=2))


def _state_file() -> Path:
    return paths.local() / "collector_state.json"


def _state() -> dict:
    try:
        return json.loads(_state_file().read_text())
    except Exception:
        return {"pushed": {}, "decisions_ts": 0, "last": 0}


def configured() -> bool:
    c = _cfg()
    return bool(c.get("collector_url") and c.get("collector_token")) and c.get("collector_push", True) not in (False, "off")


def _hash(dev: str, sid: str) -> str:
    salt = _cfg().get("collector_salt", "")
    return hashlib.sha256(f"{salt}:{dev}:{sid}".encode()).hexdigest()[:20]


def _call(method: str, path: str, body: dict | None = None, url: str | None = None, token: str | None = None) -> dict:
    c = _cfg()
    base = (url or c.get("collector_url") or "").rstrip("/")
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {token or c.get('collector_token', '')}",
                                          "Content-Type": "application/json", "User-Agent": f"once/{__version__}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read() or b"{}")


def payload(full: bool = False) -> dict:
    """What a push would send: sessions not pushed yet (or changed), every rule, decisions since the last push."""
    c = _cfg()
    dev = c.get("collector_name") or "unknown"
    st = _state()
    con = store.connect()
    store.sync(con)
    sessions = []
    for row in store.sessions(con, None, 20000):
        if (row["repo"] or "").startswith(("replay/", "local/")):
            continue
        mark = f"{row['last_event']}:{row['steps']}:{row['tags']}"
        if not full and st["pushed"].get(row["id"]) == mark:
            continue
        evs = store.events(con, row["id"])
        flags = detectors.detect(evs)
        stp = {s["step"]: s for s in detectors.steps(evs)}
        folders = sorted({str(Path(p).parent) + "/" for f in flags for k in range(f["step_from"], f["step_to"] + 1)
                          for p in stp.get(k, {}).get("paths", []) if p and not p.startswith("..")})
        oc = next((e for e in evs if e.get("event") == "outcome"), None)
        end = next((e for e in evs if e.get("event") == "session_end" and e.get("usage")), None)
        u = (end or {}).get("usage") or {}
        by = {}
        for f in flags:
            by[f["detector"]] = by.get(f["detector"], 0) + 1
        sessions.append({"sid": _hash(dev, row["id"]), "_local": row["id"], "_mark": mark, "repo": row["repo"],
                         "agent": row["agent"], "started": row["started"], "steps": row["steps"], "flags": by,
                         "avoidable": round(detectors.avoidable_ratio(evs, flags), 3),
                         "rules_given": sorted({i for e in evs if e.get("event") == "rule_injected" for i in e.get("rule_ids", [])}),
                         "broken": sorted({e.get("rule_id") for e in evs if e.get("event") in ("rule_violation", "rule_enforced")
                                           and e.get("rule_id")}),
                         "folders": folders[:10], "verified": (oc or {}).get("verified"), "cost": (oc or {}).get("cost_usd"),
                         "tokens": sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                                     "cache_creation_input_tokens"))})
    con.close()
    rule_rows = []
    from .cli import _rule_usage
    for slug in rules.known_slugs():
        if slug.startswith("local/"):
            continue
        use = _rule_usage(slug)
        for r in rules.all_levels(slug):
            if r.get("status") not in ("active", "deprecated"):
                continue
            u = use.get(r["id"], {})
            rule_rows.append({"repo": slug, "id": r["id"], "directive": (r.get("rule") or {}).get("directive", ""),
                              "level": (r.get("enforcement") or {}).get("level", "instruct"), "status": r.get("status"),
                              "scope_level": r.get("_level", "repo"), "given": u.get("fired", 0), "ignored": u.get("ignored", 0)})
    from . import learn
    decisions = [{"key": hashlib.sha256(f"{r.get('candidate_id')}:{r.get('ts')}".encode()).hexdigest()[:20],
                  "repo": r.get("repo"), "source": r.get("source_kind"), "decision": r["decision"],
                  "edit_ratio": r.get("edit_ratio"), "pipeline": r.get("pipeline_version"), "ts": r.get("ts", 0)}
                 for r in learn.history() if r.get("decision") and (full or r.get("ts", 0) > st.get("decisions_ts", 0))
                 and not str(r.get("repo") or "").startswith("local/")]
    return {"developer": dev, "version": __version__, "sessions": sessions, "rules": rule_rows, "decisions": decisions}


def push(full: bool = False, dry_run: bool = False) -> dict:
    body = payload(full)
    wire = {**body, "sessions": [{k: v for k, v in s.items() if not k.startswith("_")} for s in body["sessions"]]}
    if dry_run:
        return {"would_send": {"sessions": len(body["sessions"]), "rules": len(body["rules"]),
                               "decisions": len(body["decisions"])}, "sample": wire["sessions"][:1]}
    res = _call("POST", "/v1/push", wire)
    st = _state()
    for s in body["sessions"]:
        st["pushed"][s["_local"]] = s["_mark"]
    st["decisions_ts"] = max([d["ts"] for d in body["decisions"]] + [st.get("decisions_ts", 0)])
    st["last"] = time.time()
    _state_file().parent.mkdir(parents=True, exist_ok=True)
    _state_file().write_text(json.dumps(st))
    return res


def due() -> bool:
    return configured() and time.time() - _state().get("last", 0) > PUSH_EVERY


# ================================================================ CLI

def cli(a, say, nxt) -> int:
    from . import BIN
    act = a.action or "status"
    if act == "serve":
        data = Path(a.data or (paths.home() / "collector")).expanduser()
        if a.dry_run:
            say(f"Would serve on http://{a.host}:{a.port} with data in {data}")
            return 0
        srv = serve(data, a.host, a.port, a.token)
        tok = server_token(data, a.token)
        say(f"once collector on http://{a.host}:{srv.server_address[1]}  (data: {data})")
        say(f"Team members connect with:\n  {BIN} collector connect http://<this host>:{srv.server_address[1]} --token {tok}")
        say(f"Team dashboard: http://{a.host}:{srv.server_address[1]}/?token=<token>")
        if a.host not in ("127.0.0.1", "localhost"):
            say("Listening beyond this machine: put it behind TLS (a reverse proxy) so the token is not sent in clear text.")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            say("\nStopped.")
        return 0
    c = _cfg()
    if act == "connect":
        if not a.url or not a.token:
            say(f"Usage: {BIN} collector connect <url> --token <token> [--name <you>]")
            return 2
        try:
            _call("GET", "/healthz", url=a.url, token=a.token)
            _call("GET", "/v1/summary?days=1", url=a.url, token=a.token)
        except urllib.error.HTTPError as ex:
            say(f"Not connected: the collector answered {ex.code}" + (" (wrong token)." if ex.code == 401 else "."))
            return 1
        except Exception as ex:
            say(f"Not connected: {ex}")
            return 1
        if a.dry_run:
            say("The collector answers and accepts the token. Nothing was saved.")
            return 0
        import subprocess
        name = a.name or (subprocess.run(["git", "config", "user.name"], capture_output=True, text=True).stdout.strip()
                          or os.environ.get("USER", "dev"))
        c.update(collector_url=a.url.rstrip("/"), collector_token=a.token, collector_name=name,
                 collector_salt=c.get("collector_salt") or secrets.token_hex(8), collector_push=True)
        _save_cfg(c)
        say(f"Connected to {a.url} as {name}. Session summaries go there after each session;"
            f" prompts, commands, error text and file contents never do.")
        nxt(f"{BIN} collector push --dry-run    see exactly what is sent")
        return 0
    if act == "disconnect":
        for k in ("collector_url", "collector_token", "collector_name"):
            c.pop(k, None)
        _save_cfg(c)
        say("Disconnected. Nothing more is sent; what the collector already has stays there.")
        return 0
    if not configured():
        say(f"Not connected to a collector. Start one with  {BIN} collector serve, then  {BIN} collector connect <url> --token <t>")
        return 1
    if act == "push":
        try:
            res = push(full=a.all, dry_run=a.dry_run)
        except Exception as ex:
            say(f"Push failed: {ex}")
            return 1
        if a.json:
            print(json.dumps(res))
        elif a.dry_run:
            w = res["would_send"]
            say(f"Would send {w['sessions']} sessions, {w['rules']} rules, {w['decisions']} review decisions. One session as sent:")
            say(json.dumps(res["sample"][0] if res["sample"] else {}, indent=2))
        else:
            n = lambda k, w: f"{res.get(k, 0)} {w}{'' if res.get(k, 0) == 1 else 's'}"
            say(f"Sent {n('sessions', 'session')}, {n('rules', 'rule')}, {n('decisions', 'review decision')}.")
        return 0
    if act == "team":
        try:
            sm = _call("GET", "/v1/summary" + (f"?repo={urllib.parse.quote(a.repo)}" if a.repo else ""))
        except Exception as ex:
            say(f"Could not reach the collector: {ex}")
            return 1
        if a.json:
            print(json.dumps(sm))
            return 0
        say(f"Team, last {sm['days']} days: {sm['sessions']} sessions from {len(sm['developers'])} developers")
        for title, key in (("Agent", "agents"), ("Developer", "developers"), ("Repo", "repos")):
            say(f"\n  {title:<28} {'Sessions':>8} {'Avoidable':>9} {'Flags':>6} {'Verified':>8}")
            for k, v in sorted(sm[key].items(), key=lambda kv: -kv[1]["sessions"])[:10]:
                vr = f"{100 * v['verified_rate']:.0f}%" if v["verified_rate"] is not None else "-"
                say(f"  {k[:28]:<28} {v['sessions']:>8} {100 * v['avoidable']:>8.0f}% {v['flags']:>6} {vr:>8}")
        if sm["broken"]:
            say("\n  Rules broken most: " + ", ".join(f"{k} ({v})" for k, v in sorted(sm["broken"].items(), key=lambda kv: -kv[1])[:5]))
        say(f"\nIn a browser: {c['collector_url']}/?token=<token>")
        return 0
    st = _state()
    say(f"Connected to {c['collector_url']} as {c.get('collector_name')}. Last push: "
        + (time.strftime("%b %d %H:%M", time.localtime(st["last"])) if st.get("last") else "never") + ".")
    nxt(f"{BIN} collector team    what the team's agents did")
    return 0
