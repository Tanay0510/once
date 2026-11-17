"""SQLite index over the trajectory files. The JSONL is the record; this is a rebuildable view."""
import json
import sqlite3
import time

from . import detectors, paths


def connect() -> sqlite3.Connection:
    paths.local().mkdir(parents=True, exist_ok=True)
    try:
        return _open()
    except sqlite3.DatabaseError:
        # The database is only a view of the session files: a corrupt one is set aside and rebuilt from them.
        bad = paths.db()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for suffix in ("", "-wal", "-shm"):
            f = bad.with_name(bad.name + suffix)
            if f.exists():
                f.rename(f.with_name(f"{f.name}.corrupt-{stamp}"))
        return _open()


def _open() -> sqlite3.Connection:
    con = sqlite3.connect(paths.db(), timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, offset INTEGER NOT NULL, lines INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS sessions(
        id TEXT PRIMARY KEY, repo TEXT, agent TEXT, model TEXT, cwd TEXT, commit_sha TEXT,
        started REAL, last_event REAL, ended INTEGER DEFAULT 0, prompt TEXT,
        steps INTEGER DEFAULT 0, flags INTEGER DEFAULT 0, tags INTEGER DEFAULT 0,
        rules_given INTEGER DEFAULT 0, held_out INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS events(
        session_id TEXT, seq INTEGER, ts REAL, event TEXT, tool TEXT, target TEXT, ok INTEGER,
        raw TEXT, PRIMARY KEY(session_id, seq));
    CREATE INDEX IF NOT EXISTS ev_session ON events(session_id, seq);
    CREATE TABLE IF NOT EXISTS actions(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, payload TEXT, undone INTEGER DEFAULT 0);
    """)
    return con


def sync(con: sqlite3.Connection | None = None) -> int:
    """Ingest new trajectory lines. Idempotent: an event is keyed by (session, line number)."""
    own = con is None
    con = con or connect()
    base = paths.local() / "trajectories"
    added, touched = 0, set()
    if base.is_dir():
        for f in base.rglob("*.jsonl"):
            row = con.execute("SELECT offset, lines FROM files WHERE path=?", (str(f),)).fetchone()
            offset, lines = (row["offset"], row["lines"]) if row else (0, 0)
            size = f.stat().st_size
            if size < offset:  # file was replaced; start over
                con.execute("DELETE FROM events WHERE session_id=?", (f.stem,))
                offset, lines = 0, 0
            if size == offset:
                continue
            with open(f, "rb") as fh:
                fh.seek(offset)
                chunk = fh.read()
            end = chunk.rfind(b"\n")
            if end < 0:
                continue  # a partial line is still being written
            for line in chunk[: end + 1].splitlines():
                try:
                    e = json.loads(line)
                except Exception:
                    lines += 1
                    continue
                if not isinstance(e, dict):
                    lines += 1
                    continue
                e["seq"] = lines
                txt = lambda v: v if v is None or isinstance(v, str) else json.dumps(v)  # never let one odd line stop the sync
                ts = e.get("ts") if isinstance(e.get("ts"), (int, float)) else None
                con.execute("INSERT OR REPLACE INTO events VALUES(?,?,?,?,?,?,?,?)",
                            (txt(e.get("session_id")) or f.stem, lines, ts, txt(e.get("event")), txt(e.get("tool")),
                             txt(e.get("target")), None if e.get("ok") is None else int(bool(e["ok"])),
                             json.dumps(e)))
                touched.add(txt(e.get("session_id")) or f.stem)
                lines += 1
                added += 1
            con.execute("INSERT OR REPLACE INTO files VALUES(?,?,?)", (str(f), offset + end + 1, lines))
    for sid in touched:
        try:
            _refresh_session(con, sid)
        except Exception:  # one odd session must not stop the others from being indexed
            pass
    con.commit()
    if own:
        con.close()
    return added


def events(con, session_id: str) -> list[dict]:
    return [json.loads(r["raw"]) for r in
            con.execute("SELECT raw FROM events WHERE session_id=? ORDER BY seq", (session_id,))]


def _refresh_session(con, sid: str):
    evs = events(con, sid)
    if not evs:
        return
    start = next((e for e in evs if e.get("event") == "session_start"), evs[0])
    prompt = next((e.get("prompt") for e in evs if e.get("event") == "user_prompt"), None)
    flags = detectors.detect(evs)
    given = {rid for e in evs if e.get("event") == "rule_injected" for rid in e.get("rule_ids", [])}
    # Headless Claude Code sends no model at session start; the usage read at the end names it.
    model = start.get("model") or next((m for e in evs if e.get("event") == "session_end"
                                        for m in ((e.get("usage") or {}).get("models") or [])[:1]), None)
    con.execute("""INSERT OR REPLACE INTO sessions
        (id, repo, agent, model, cwd, commit_sha, started, last_event, ended, prompt, steps, flags, tags, rules_given, held_out)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (sid, start.get("repo"), start.get("agent"), model, start.get("cwd"), start.get("commit"),
         evs[0].get("ts"), evs[-1].get("ts"), int(any(e.get("event") == "session_end" for e in evs)),
         prompt, len(detectors.steps(evs)), len(flags),
         len(detectors.annotations(evs)), len(given),
         int(bool(start.get("held_out")))))


def sessions(con, repo: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
    if repo:
        return con.execute("SELECT * FROM sessions WHERE repo=? ORDER BY started DESC LIMIT ?", (repo, limit)).fetchall()
    return con.execute("SELECT * FROM sessions ORDER BY started DESC LIMIT ?", (limit,)).fetchall()


def log_action(con, kind: str, payload: dict):
    con.execute("INSERT INTO actions(ts, kind, payload) VALUES(?,?,?)", (time.time(), kind, json.dumps(payload)))
    con.commit()


def last_action(con):
    return con.execute("SELECT * FROM actions WHERE undone=0 ORDER BY id DESC LIMIT 1").fetchone()
