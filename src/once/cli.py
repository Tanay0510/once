"""The developer-facing CLI. Every command leads with the answer and ends with one next step."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import BIN, SCHEMA, __version__, adapters, detectors, index, paths, repo, rules, store

TAGS = {"w": "wrong", "a": "avoidable", "m": "missing_knowledge", "g": "good"}
HELP = f"""{BIN} - memory for your coding agents

Try       demo
Setup     init, doctor, import, config, completion
Daily     (no command), last, teach, why, tag, ui, digest
Review    learn, review, rules, playbooks, export
Team      share, pr-check, collector
Around    menubar, editor, statusline
Control   off, on, undo, purge, uninstall
Builder   lab tasks, sweep, run, replay, regress, exp, cochange, stats, bootstrap, meta, cost

Examples
  {BIN}                        what is pending in this repo
  {BIN} teach "use uv run"     add a rule now
  {BIN} last                   the last session, with flagged steps
  {BIN} why src/api/router.py  which rules apply to a file
  {BIN} ui                     the same, in the browser, with live sessions

Every command takes --json, --quiet and --dry-run.
Advanced: {BIN} lab --help
"""


# ------------------------------------------------------------------ helpers

def _ctx():
    slug, root = repo.slug(os.getcwd())
    return slug, root


def _say(*a):
    print(*a)


def _next(text):
    print(f"\nNext: {text}")


def _ago(ts):
    if not ts:
        return "never"
    d = time.time() - ts
    for unit, sec in (("day", 86400), ("h", 3600), ("min", 60)):
        if d >= sec:
            n = int(d // sec)
            return f"{n} {unit}{'s' if unit == 'day' and n != 1 else ''} ago"
    return "just now"


def _who():
    try:
        r = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() or os.environ.get("USER", "you")
    except Exception:
        return os.environ.get("USER", "you")


def _agents_found():
    found = {}
    for name, binary in adapters.KNOWN.items():
        path = shutil.which(binary)
        if not path:
            continue
        ver = ""
        try:
            r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20,
                               stdin=subprocess.DEVNULL)
            ver = (r.stdout or r.stderr).strip().splitlines()[0][:40] if (r.stdout or r.stderr).strip() else ""
        except Exception:
            pass
        found[name] = ver
    return found


def _refresh(slug, root=None):
    """Rebuild the index, native rule files and skills. Runs after every change; nobody types it."""
    from . import playbooks
    root = root or repo.known_root(slug)
    root = Path(root) if root else None
    if root:
        repo.remember_root(slug, root)  # first, so the index includes the rules committed to the repo
    n = rules.compile_index(slug)
    written = []
    if root:
        cc = adapters.get("claude-code")
        ag = adapters.get("antigravity")
        others = [x for x in _connected() if x != "claude-code"]
        off = paths.off_flag().exists() or paths.off_flag(slug).exists()
        if off:
            cc.remove_native(root)
            cc.remove_skills(root)
            ag.remove_native(root)
            ag.remove_skills(root)
        else:
            idx_rules = index.load(slug)["rules"]
            pbs = [p_ for p_ in playbooks.load_all(slug) if not p_.get("draft")]
            written = cc.emit_native(idx_rules, root)
            if written:
                repo.ensure_excluded(root, cc.NATIVE_DIR + "/")
            if cc.emit_skills(pbs, root):
                repo.ensure_excluded(root, f".claude/skills/{cc.SKILL_PREFIX}*/")
            # Antigravity reads glob rules from .agents/rules; it and Codex read skills from .agents/skills.
            if "antigravity" in others and ag.emit_native(idx_rules, root):
                repo.ensure_excluded(root, f"{ag.RULES_DIR}/{ag.PREFIX}*.md")
            if set(others) & {"antigravity", "codex"} and ag.emit_skills(pbs, root):
                repo.ensure_excluded(root, f"{ag.SKILLS_DIR}/{ag.PREFIX}*/")
    return n, written


def _connected() -> list[str]:
    """Adapters whose hooks are fully installed."""
    out = []
    for name in adapters.IMPLEMENTED:
        try:
            # Connected means sessions are captured; the enforcement group is optional and doctor names it.
            inst = adapters.get(name).installed()
            if all(ok for ev, ok in inst.items() if "(" not in ev):
                out.append(name)
        except Exception:
            pass
    return out


_TEST_ENV = {"antigravity": "ONCE_AGY_HOOKS", "codex": "ONCE_CODEX_HOOKS"}


def _installable(name: str) -> bool:
    """In tests (a sandboxed Claude settings file) only touch another agent's config if it is sandboxed too."""
    return not os.environ.get("ONCE_CLAUDE_SETTINGS") or bool(os.environ.get(_TEST_ENV.get(name, "_")))


def _agent_cmd(a) -> str:
    return (getattr(a, "agent_cmd", None) or os.environ.get("ONCE_HEADLESS")
            or adapters.get(getattr(a, "agent", None) or "claude-code").HEADLESS)


def _cfg_path():
    return paths.home() / "config.json"


def _cfg() -> dict:
    try:
        return json.loads(_cfg_path().read_text())
    except Exception:
        return {}


def _pending_episodes(slug) -> int:
    try:
        from . import learn
        return len(learn.episodes(slug))
    except Exception:
        return 0


def _pick_session(con, slug, n=1):
    rows = store.sessions(con, slug, limit=max(n, 1))
    if len(rows) >= n:
        return rows[n - 1], False
    rows = store.sessions(con, None, limit=max(n, 1))
    return (rows[n - 1], True) if len(rows) >= n else (None, False)


# ------------------------------------------------------------------ commands

def cmd_init(a):
    slug, root = _ctx()
    found = _agents_found()
    usable = [n for n in found if n in adapters.IMPLEMENTED]
    pending = [n for n in found if n not in adapters.IMPLEMENTED]
    check = repo.detect_check_command(root) if root else None
    if a.json:
        print(json.dumps({"agents": found, "repo": slug, "check_command": check and check[0]}))
    else:
        _say(f"Found agents    {', '.join(f'{n} {v}'.strip() for n, v in found.items()) or 'none on PATH'}")
        _say(f"This repo       {slug}" + (f"  ({repo.detect_stack(root)})" if root else "  (not a git repo)"))
        _say(f"Check command   {check[0]}  (read from {check[1]})" if check else "Check command   not found in CI config")
        _say("\nThis will:")
        _say(f"  - add hooks to {adapters.get('claude-code').settings_path()} for claude-code")
        for other in usable:
            if other != "claude-code":
                _say(f"  - add hooks to {adapters.get(other).settings_path()} for {other}")
        _say("  - register the `once` MCP server with " + ", ".join(["Claude Code"] + [o.capitalize() if o != "codex" else "Codex"
                                                                       for o in usable if o != "claude-code"])
             + " (playbooks and in-session feedback)")
        _say("  - add four slash commands to Claude Code: /once-teach, /once-flag, /once-why, /once-off")
        _say("  - add a status line to the bottom of Claude Code (an existing status line keeps running)")
        _say(f"  - create {paths.repo_memory(slug)}/")
        if root:
            _say("  - add one line to .git/info/exclude when the first path-scoped rule is written")
        _say(f"Nothing in this repo's tracked files changes. Undo with: {BIN} uninstall")
        if pending:
            _say(f"Not yet supported, skipped: {', '.join(pending)}")
    if a.dry_run:
        return 0
    if not a.yes:
        if not sys.stdin.isatty():
            _say("\nRun again with --yes to continue.")
            return 1
        if input("\nContinue? [Y/n] ").strip().lower() not in ("", "y", "yes"):
            _say("Nothing changed.")
            return 1
    t0 = time.time()
    from . import migrate
    moved = migrate.migrate(root)
    if moved and not a.json:
        _say("\nMoving over from the old name (meta):")
        for m in moved:
            _say(f"  - {m}")
    cc = adapters.get("claude-code")
    if "claude-code" not in found and not a.json:
        _say("\nclaude was not found on PATH; hooks are registered anyway and take effect once it is installed.")
    cc.install()
    others = [o for o in usable if o != "claude-code" and _installable(o)]
    for other in others:
        adapters.get(other).install()
    mcp_ok, mcp_msg = cc.register_mcp()
    for other in others:  # codex: `codex mcp add`; antigravity: an entry in ~/.gemini/config/mcp_config.json
        adapters.get(other).register_mcp()
    cc.install_commands()
    wrapped = cc.install_statusline()
    paths.local().mkdir(parents=True, exist_ok=True)
    (paths.local() / "agent_versions.json").write_text(json.dumps(found))
    mem = paths.repo_memory(slug)
    (mem / "rules").mkdir(parents=True, exist_ok=True)
    (mem / "playbooks").mkdir(exist_ok=True)
    cfg = mem / "repo.yaml"
    if not cfg.exists():
        import yaml
        cfg.write_text(yaml.safe_dump({"schema": SCHEMA, "check_command": check[0] if check else None,
                                       "stack": repo.detect_stack(root) if root else None}, sort_keys=False))
    rules.commit(f"init {slug}")
    _refresh(slug, root)
    store.sync()
    if not a.json:
        _say(f"\nDone in {time.time() - t0:.0f}s. {BIN} is observing. Your agents behave exactly as before")
        _say("until you add or approve a rule.")
        if not mcp_ok and "test mode" not in mcp_msg:
            _say(f"\nThe in-agent tools were not registered ({mcp_msg}). To add them by hand:")
            _say(f"  claude mcp add -s user {cc.MCP_NAME} -- {' '.join(cc.mcp_command())}")
        if wrapped:
            _say(f"\nYour status line still runs; the `once` line is added after it.")
        if "codex" in others:
            _codex_notes()
        _say("\nFrom now on you rarely need a command:")
        _say("  - tell your agent when it gets something wrong (\"no, use tox\"); it is kept as a suggestion")
        _say("  - your agent asks you about suggestions at the start of the next session; answer yes or no")
        _say("  - the status line at the bottom of Claude Code shows rules in use and anything waiting")
        _say(f"\nOptional: a menu bar icon ({BIN} menubar --install) or the editor sidebar ({BIN} editor install).")
        sh = Path(os.environ.get("SHELL", "")).name
        if sh in COMPLETION:
            rc = {"zsh": "~/.zshrc", "bash": "~/.bashrc", "fish": "~/.config/fish/config.fish"}[sh]
            line = f'{BIN} completion fish | source' if sh == "fish" else f'eval "$({BIN} completion {sh})"'
            _say(f"Tab completion for rule ids and sessions: add  {line}  to {rc}")
        _next(f"just work. Or  {BIN} lab bootstrap  for first suggestions from this repo's CI and lint config (no model calls).")
    return 0


def _codex_notes():
    cx = adapters.get("codex")
    v = cx.version()
    _say("\nCodex runs a new hook only after you approve it. Once now: start codex, type /hooks,")
    _say("and trust the six hooks that run `once.hook codex`. Until then Codex sessions are not recorded.")
    if v and v < cx.MIN_VERSION:
        _say(f"Your codex is {'.'.join(map(str, v))}; hooks need 0.124 or newer (or [features] codex_hooks = true in")
        _say("~/.codex/config.toml). Update codex for them to run.")


def cmd_status(a):
    slug, root = _ctx()
    con = store.connect()
    store.sync(con)
    week = time.time() - 7 * 86400
    rows = [r for r in store.sessions(con, slug, 500) if (r["started"] or 0) >= week]
    flagged = [r for r in rows if r["flags"] and not r["tags"]]
    all_rules = rules.load_all(slug)
    act = [r for r in all_rules if r.get("status") == "active"]
    cand = [r for r in all_rules if r.get("status") == "candidate"]
    connected = _connected()
    hooks_ok = bool(connected)
    last = max((r["last_event"] or 0 for r in store.sessions(con, None, 1)), default=0)
    errs = sum(1 for _ in open(paths.error_log())) if paths.error_log().exists() else 0
    off = paths.off_flag().exists() or paths.off_flag(slug).exists()
    data = {"repo": slug, "memory": "off" if off else "on", "hooks_installed": hooks_ok,
            "sessions_week": len(rows), "sessions_with_flags": len(flagged),
            "rules_active": len(act), "rules_waiting": len(cand), "last_capture": last, "hook_errors": errs}
    if a.json:
        print(json.dumps(data))
        return 0
    agents = ", ".join(connected) if hooks_ok else "no agents connected"
    _say(f"{slug}   {agents}   memory {'off' if off else 'on'}\n")
    _say(f"  Sessions   {len(rows)} this week" + (f", {len(flagged)} with flagged steps" if flagged else ""))
    _say(f"  Rules      {len(act)} active" + (f", {len(cand)} waiting for review" if cand else ""))
    health = "ok" if hooks_ok and not errs else ("hooks not installed" if not hooks_ok else f"{errs} hook errors logged")
    _say(f"  Health     {health}, last capture {_ago(last)}")
    if not hooks_ok:
        _next(f"{BIN} init    connect your agents (one time)")
    elif errs:
        _next(f"{BIN} doctor    see what went wrong")
    elif cand:
        _next(f"{BIN} review    {len(cand)} suggested rule{'s' if len(cand) != 1 else ''} waiting")
    elif flagged:
        _next(f"{BIN} last    {len(flagged)} session{'s' if len(flagged) != 1 else ''} with flagged steps to look at")
    elif act and _pending_episodes(slug):
        _next(f"{BIN} learn    {_pending_episodes(slug)} episode{'s' if _pending_episodes(slug) != 1 else ''} to turn into suggested rules")
    elif not act:
        _next(f'{BIN} teach "..."    add your first rule')
    elif not rows:
        _next("just work. Sessions appear here by themselves.")
    return 0


def _timeline(evs, flags):
    st = detectors.steps(evs)
    in_flag = {}
    for f in flags:
        for s in range(f["step_from"], f["step_to"] + 1):
            in_flag[s] = f
    rows, i = [], 0
    while i < len(st):
        s = st[i]
        f = in_flag.get(s["step"])
        if f and f["step_from"] != f["step_to"]:
            rows.append((" !", f"{f['step_from']}-{f['step_to']}", "", f["note"]))
            i += f["step_to"] - f["step_from"] + 1
            continue
        mark = " !" if f else "  "
        if s["event"] == "user_prompt":
            rows.append((mark, str(s["step"]), "prompt", detectors._short(s.get("prompt"), 60)))
        else:
            res = "" if s.get("ok") is not False else "   failed" + (f" {s['error_head']}" if s.get("error_head") else "")
            why = f"   <- {f['note']}" if f and f["detector"] != "failed_command" else ""
            rows.append((mark, str(s["step"]), s.get("tool", ""), detectors._short(s.get("target"), 56) + res + why))
        i += 1
    if len(rows) > 44:  # collapse long uneventful runs
        out, run = [], []
        for r in rows:
            if r[0].strip():
                if len(run) > 8:
                    out += run[:3] + [("  ", "…", "", f"{len(run) - 6} uneventful steps")] + run[-3:]
                else:
                    out += run
                run = []
                out.append(r)
            else:
                run.append(r)
        out += run[:3] + ([("  ", "…", "", f"{len(run) - 6} uneventful steps")] + run[-3:] if len(run) > 8 else run[3:])
        rows = out
    return rows


def cmd_last(a):
    slug, _ = _ctx()
    con = store.connect()
    store.sync(con)
    row, other = _pick_session(con, slug, a.n)
    if row is None:
        cc = adapters.get("claude-code")
        if a.json:
            print(json.dumps({"session": None}))
            return 1
        _say("No sessions captured yet.")
        if not all(cc.installed().values()):
            _say(f"\nThe hooks are not installed.\n\nFix: {BIN} init")
        else:
            _say("\nThe hooks are installed but have not fired. Start an agent session in this repo,")
            _say(f"or run  {BIN} doctor  if you already have.")
        return 1
    if getattr(a, "web", False):
        import urllib.parse
        import webbrowser
        from . import ambient
        url = f"{ambient.ensure_ui(a.port, os.getcwd())}/session/{urllib.parse.quote(row['id'])}"
        if not a.dry_run:
            webbrowser.open(url)
        print(json.dumps({"url": url}) if a.json else url)
        return 0
    evs = store.events(con, row["id"])
    flags = detectors.detect(evs)
    notes = detectors.annotations(evs)
    given = [rid for e in evs if e.get("event") == "rule_injected" for rid in e.get("rule_ids", [])]
    if a.json:
        print(json.dumps({"session": dict(row), "steps": detectors.steps(evs), "flags": flags,
                          "annotations": notes, "rules_given": given,
                          "avoidable_ratio": round(detectors.avoidable_ratio(evs, flags), 3)}))
        return 0
    dur = max(0, (row["last_event"] or 0) - (row["started"] or 0))
    when = time.strftime("%b %d %H:%M", time.localtime(row["started"] or 0))
    label = next((e.get("label") for e in evs if e.get("event") == "session_start" and e.get("label")), None)
    _say(f"{row['agent']}, {when}, {max(1, round(dur / 60))} min, {row['steps']} steps"
         + (f"   [{row['repo']}]" if other else "")
         + (f"   [lab run: {label.get('task')}, {label.get('arm')}]" if label else ""))
    if row["prompt"]:
        _say(f'"{detectors._short(row["prompt"], 70)}"')
    _say("")
    for mark, num, kind, text in _timeline(evs, flags):
        _say(f"{mark} {num:>5}  {kind:<7} {text}")
    _say("")
    if row["cwd"] and not next((e for e in evs if e.get("event") == "outcome"), None):
        from . import snapshot
        r_ = repo.root(row["cwd"]) if os.path.isdir(row["cwd"]) else None
        ch = snapshot.changed(str(r_), row["id"]) if r_ else None
        if ch:
            _say(f"Files changed in this session: {', '.join(ch[:8])}" + (f" and {len(ch) - 8} more" if len(ch) > 8 else ""))
    oc = next((e for e in evs if e.get("event") == "outcome"), None)
    if oc:
        v = {True: "passed the PR's tests", False: "FAILED the PR's tests", None: "not checked"}[oc.get("verified")]
        _say(f"Outcome: {v}" + (f", ${oc['cost_usd']:.2f}" if oc.get("cost_usd") else ""))
        if oc.get("missed_files"):
            _say(f"The real fix also changed: {', '.join(oc['missed_files'])}")
        if oc.get("wrote_tests") is False:
            _say("The real fix added tests; the agent wrote none.")
    end = next((e for e in evs if e.get("event") == "session_end" and e.get("usage")), None)
    if end:
        u = end["usage"]
        _say(f"Tokens: {u['input_tokens'] + u['cache_creation_input_tokens'] + u['cache_read_input_tokens']:,} in, "
             f"{u['output_tokens']:,} out, {u['model_calls']} model calls")
    if given:
        _say(f"Rules given: {', '.join(dict.fromkeys(given))}")
    enf = [e for e in evs if e.get("event") == "rule_enforced"]
    if enf:  # a refused call leaves no step behind, so say it here
        words = {"block": "refused before it landed", "check": "sent back as an error to fix", "remind": "reminded after"}
        for e in enf:
            _say(f"Enforced: {e.get('rule_id')} ({e.get('level')}): an edit or command {words.get(e.get('level'), 'enforced')}"
                 + (f", {e['path']}" if e.get("path") else ""))
    for n in notes:
        _say(f"Tagged {n.get('step_from')}-{n.get('step_to')}: {n.get('tag')}" + (f" - {n['note']}" if n.get("note") else ""))
    if flags:
        _say(f"{len(flags)} flagged range{'s' if len(flags) != 1 else ''}, "
             f"{round(100 * detectors.avoidable_ratio(evs, flags))}% of steps.")
        if sys.stdin.isatty() and not a.no_input:
            return _tag_loop(row, evs, flags)
        f = flags[0]
        rng = f"{f['step_from']}-{f['step_to']}" if f["step_from"] != f["step_to"] else str(f["step_from"])
        _next(f'{BIN} tag {rng} missing "what it should have done"')
    return 0


def _write_annotation(row, evs, step_from, step_to, tag, note, confirms=None):
    """Append a tag to the session. Returns its id, or None when the steps are out of range."""
    import uuid
    from . import hook
    st = {s["step"]: s for s in detectors.steps(evs)}
    if step_from not in st or step_to not in st or step_from > step_to:
        return None
    aid = "a_" + uuid.uuid4().hex[:10]
    ev = {"schema": SCHEMA, "session_id": row["id"], "agent": row["agent"], "repo": row["repo"],
          "ts": time.time(), "event": "annotation", "id": aid, "source": "human", "tag": tag, "note": note or "",
          "step_from": step_from, "step_to": step_to,
          "seq_from": st[step_from]["seq"], "seq_to": st[step_to]["seq"]}
    if confirms:
        ev["confirms"] = f"auto:{confirms}"
    hook._append(row["repo"], row["id"], ev)
    try:
        store.log_action(store.connect(), "tag", {"slug": row["repo"], "session": row["id"], "agent": row["agent"],
                                                  "annotation": aid, "steps": f"{step_from}-{step_to}", "root": None})
    except Exception:
        pass
    return aid


def _tag_loop(row, evs, flags):
    _say("Tag a flag: its number, then  [w]rong  [a]voidable  [m]issing knowledge  [g]ood.  Enter to quit.")
    for i, f in enumerate(flags, 1):
        _say(f"  {i}. steps {f['step_from']}-{f['step_to']}: {f['note']}")
    while True:
        try:
            ans = input("> ").strip().lower().split()
        except (EOFError, KeyboardInterrupt):
            break
        if not ans:
            break
        if len(ans) < 2 or not ans[0].isdigit() or ans[1][0] not in TAGS or not 1 <= int(ans[0]) <= len(flags):
            _say("  e.g.  1 m")
            continue
        f = flags[int(ans[0]) - 1]
        note = input("  What should it have done? (optional) ").strip()
        _write_annotation(row, evs, f["step_from"], f["step_to"], TAGS[ans[1][0]], note, f["detector"])
        _say("  Saved.")
    store.sync()
    return 0


def cmd_tag(a):
    slug, _ = _ctx()
    con = store.connect()
    store.sync(con)
    row, _ = _pick_session(con, slug, a.session)
    if row is None:
        _say("No sessions captured yet.")
        return 1
    try:
        lo, _, hi = a.steps.partition("-")
        lo, hi = int(lo), int(hi or lo)
    except ValueError:
        _say(f"Steps look like 4 or 4-8.\n\nExample: {BIN} tag 4-8 missing \"use the Utc type\"")
        return 2
    tag = next((v for k, v in TAGS.items() if a.tag.lower().startswith(k)), None)
    if not tag:
        _say("Tag must be one of: wrong, avoidable, missing, good.")
        return 2
    evs = store.events(con, row["id"])
    flag = next((f for f in detectors.detect(evs) if f["step_from"] == lo and f["step_to"] == hi), None)
    if a.dry_run:
        _say(f"Would tag steps {lo}-{hi} of the session from {time.strftime('%b %d %H:%M', time.localtime(row['started'] or 0))} "
             f"as {tag.replace('_', ' ')}.")
        return 0
    if not _write_annotation(row, evs, lo, hi, tag, a.note, flag and flag["detector"]):
        _say(f"That session has {row['steps']} steps; {a.steps} is out of range.")
        return 2
    store.sync(con)
    _say(f"Tagged steps {lo}-{hi} as {tag.replace('_', ' ')}." if lo != hi else f"Tagged step {lo} as {tag.replace('_', ' ')}.")
    if tag != "good" and a.note:
        _next(f'{BIN} teach "{a.note}"    make it a rule now')
    return 0


def cmd_teach(a):
    slug, root = _ctx()
    sentence = " ".join(a.sentence).strip()
    if not sentence:
        _say(f'Say what the agent should do.\n\nExample: {BIN} teach "always use uv run, never bare pip"')
        return 2
    rule = rules.new_manual(sentence, a.path or [], _who())
    rule["id"] = rules.unique_id(slug, rule["id"])
    level = getattr(a, "level", "repo") or "repo"
    where = "this repo"
    target = None
    if level != "repo":
        if level == "stack":
            st = a.stack or (rules.stacks_for(slug) or [None])[-1]
            if not st:
                _say(f"No stack detected for this repo. Name one:  {BIN} teach --level stack --stack uv \"...\"")
                return 2
            target, where = rules.level_dir("stack", stack=st), f"every {st} repo"
        elif level == "agent":
            if not a.agent:
                _say(f"Name the agent:  {BIN} teach --level agent --agent codex \"...\"")
                return 2
            target, where = rules.level_dir("agent", agent=a.agent), f"every {a.agent} session"
            rule["scope"]["agents"] = [a.agent]
        elif level == "owner":
            try:
                target = rules.level_dir("owner", slug)
            except ValueError as e:
                _say(f"Not added: {e}.")
                return 2
            where = f"every repo of {rules.owner_of(slug)}"
        else:
            target, where = rules.level_dir("user"), "every repo you work in (your own preference)"
        if a.path:
            _say("A rule above the repo applies to whole repos; --path is ignored.")
            rule["scope"]["path_globs"] = []
    errs = rules.lint(rule, root if level == "repo" else None)
    if errs:
        _say("Not added:")
        for e in errs:
            _say(f"  - {e}")
        return 2
    if a.dry_run:
        _say(f"Would add for {where}: {sentence}")
        return 0
    if target is not None:
        target.mkdir(parents=True, exist_ok=True)
        f = target / f"{rule['id']}.yaml"
        import yaml as _yaml
        f.write_text(_yaml.safe_dump(rule, sort_keys=False, allow_unicode=True))
        rules.commit(f"teach {rule['id']} ({level})")
        rules.compile_all()
    else:
        f = rules.save(slug, rule)
        rules.commit(f"teach {rule['id']}")
    _refresh(slug, root)
    con = store.connect()
    store.log_action(con, "teach", {"slug": slug, "id": rule["id"], "file": str(f), "root": str(root) if root else None})
    if a.json:
        print(json.dumps({"id": rule["id"], "file": str(f), "level": level}))
        return 0
    _say(f"Added as a rule for {where}:")
    _say(f"  {sentence}")
    _say(f"  Applies to   {', '.join(a.path) if a.path and level == 'repo' else 'whole repo'}")
    _say(f"  Status       active (you wrote it)   id: {rule['id']}")
    if not all(adapters.get("claude-code").installed().values()):
        _say(f"\nYour agents are not connected yet, so nothing will see this rule. Run: {BIN} init")
    _say(f'\nUndo: {BIN} undo     Narrow it: {BIN} teach --path "src/**" "..."')
    return 0


def cmd_why(a):
    slug, root = _ctx()
    idx = index.load(slug)
    if a.target:
        p = Path(a.target)
        rel = os.path.relpath(p.resolve(), root) if root and (p.is_absolute() or p.exists()) else a.target
        scoped = index.matching(idx, rel)
        wide = index.repo_wide(idx)
        import re as _re
        enforced = [r for r in idx.get("rules", []) if r.get("level", "instruct") != "instruct"
                    and (r.get("repo_wide") or any(_re.match(rx, rel) for rx in r.get("regex", [])))]
        if a.json:
            print(json.dumps({"file": rel, "rules": [r["id"] for r in scoped + wide],
                              "enforced": {r["id"]: r["level"] for r in enforced}}))
            return 0
        total = len(scoped) + len(wide)
        _say(f"{total} rule{'s' if total != 1 else ''} would apply to {rel}:" if total else f"No rules apply to {rel}.")
        for r in scoped:
            _say(f"  {r['id']:<34} matches {', '.join(r['globs'])}")
        for r in wide:
            _say(f"  {r['id']:<34} whole repo" + ("" if r.get("scope_level", "repo") == "repo" else f" ({r['scope_level']} level)"))
        for r in enforced:
            _say(f"  {r['id']:<34} {r['level']}: checked against each edit, not given as context")
        if total or enforced:
            _next(f"{BIN} rules show <id>    wording and source")
        return 0
    con = store.connect()
    store.sync(con)
    row, _ = _pick_session(con, slug, 1)
    if row is None:
        _say(f"No sessions captured yet. To see what would apply to a file: {BIN} why <file>")
        return 1
    evs = store.events(con, row["id"])
    given = [e for e in evs if e.get("event") == "rule_injected"]
    held = [e for e in evs if e.get("event") == "rule_withheld"]
    if a.json:
        print(json.dumps({"session": row["id"], "injected": given, "withheld": held}))
        return 0
    n = sum(len(e.get("rule_ids", [])) for e in given)
    _say(f"Last session got {n} rule{'s' if n != 1 else ''}" + (":" if n else "."))
    for e in given:
        for rid in e.get("rule_ids", []):
            how = "repo-wide, given at session start" if e.get("channel") == "session_start" \
                else f"matched {e.get('matched', 'a file')} when it was touched"
            _say(f"  {rid:<34} {how}")
    for e in held:
        why = "this session was held out" if e.get("reason") == "held_out" else "over the rule budget"
        _say(f"\n{len(e.get('rule_ids', []))} matched but held back: {why}.")
    if row["agent"] == "claude-code" and any(e.get("inferred") for e in given):
        _say("\nPath-scoped rules are loaded by Claude Code itself; their timing here is inferred from file reads.")
    return 0


def cmd_rules(a):
    slug, root = _ctx()
    all_rules = rules.load_all(slug)
    if a.action in ("show", "drop", "detect") and not a.id:
        _say(f"Name a rule: {BIN} rules {a.action} <id>   (any unique prefix works). List them with: {BIN} rules")
        return 1
    if a.action == "show":
        r = rules.find(slug, a.id or "")
        if not r:
            _say(f"No single rule matches {a.id!r}. Run: {BIN} rules")
            return 1
        if a.json:
            print(json.dumps({k: v for k, v in r.items() if not k.startswith("_")}))
            return 0
        _say(f"{r['id']}   {r.get('status')}   {r.get('kind')}")
        _say(f"  {(r.get('rule') or {}).get('directive', '')}")
        if (r.get("rule") or {}).get("rationale"):
            _say(f"  Why: {r['rule']['rationale']}")
        gl = (r.get("scope") or {}).get("path_globs") or []
        _say(f"  Applies to   {', '.join(gl) if gl else 'whole repo'}")
        pv = r.get("provenance") or {}
        _say(f"  Source       {pv.get('source_kind', '?')}, {pv.get('approved_by', '?')}, {pv.get('created', '?')}")
        lvl = r.get("_level", "repo")
        if lvl != "repo":
            _say(f"  Level        {lvl}" + (f" ({r['_level_key']})" if r.get("_level_key") else "") + ", inherited by this repo")
        enf = r.get("enforcement") or {}
        det = (enf.get("detector") or {}).get("pattern")
        _say(f"  Enforcement  {enf.get('level', 'instruct')}" + (f", detector /{det}/" if det else ""))
        u = _rule_usage(slug).get(r["id"], {})
        _say(f"  Seen         given in {u.get('fired', 0)} sessions, ignored in {u.get('ignored', 0)}"
             + (f", enforced {u['enforced']} times" if u.get("enforced") else ""))
        rp_ = (r.get("evidence") or {}).get("replay")
        if rp_:
            _say(f"  Evidence     replayed {rp_.get('runs')}x: failed {rp_['without_rule']['failed']} without it, "
                 f"{rp_['with_rule']['failed']} with it ({rp_.get('verdict')})")
        _say(f"  File         {r['_file']}")
        if u.get("ignored") and enf.get("level", "instruct") == "instruct":
            _next(f"{BIN} rules promote {r['id']}    the agent ignored it {u['ignored']} times; check it after each edit instead")
        return 0
    if a.action == "detect":
        r = rules.find(slug, a.id or "")
        if not r or not a.pattern:
            _say(f'Usage: {BIN} rules detect <id> "<regex of what the rule forbids>"')
            return 2
        import re as _re
        try:
            _re.compile(a.pattern)
        except _re.error as e:
            _say(f"Not a valid pattern: {e}")
            return 2
        gl = (r.get("scope") or {}).get("path_globs") or []
        hits = 0
        if root:
            g = subprocess.run(["git", "-C", str(root), "grep", "-lE", a.pattern, "--", *(gl or ["."])], capture_output=True, text=True)
            hits = len([l for l in g.stdout.splitlines() if l.strip()])
        if a.dry_run:
            _say(f"Would set /{a.pattern}/ as the detector for {r['id']} ({hits} file(s) in the tree match it now).")
            return 0
        r.setdefault("enforcement", {"level": "instruct"})["detector"] = {
            "kind": "text_pattern", "pattern": a.pattern, "backtest": {"commit": repo.head(root) if root else "", "files_matching_in_tree": hits}}
        rules.save(slug, r)
        rules.commit(f"detector for {r['id']}")
        _refresh(slug, root)
        _say(f"Detector set for {r['id']}. Edits that match it after the rule was given are flagged as an ignored rule.")
        if hits:
            _say(f"Note: {hits} file{'s' if hits != 1 else ''} in the repo already match{'es' if hits == 1 else ''} this pattern, "
                 "so the codebase itself does not follow it everywhere. Check the pattern, or the rule.")
        return 0
    if a.action == "drop":
        r = rules.find(slug, a.id or "")
        if not r:
            _say(f"No single rule matches {a.id!r}. Run: {BIN} rules")
            return 1
        seen = _rule_usage(slug).get(r["id"], {})
        if a.dry_run:
            _say(f"Would drop {r['id']}. It was given in {seen.get('fired', 0)} session(s).")
            return 0
        r["status"] = "deprecated"
        rules.save(slug, r)
        rules.commit(f"drop {r['id']}")
        if r.get("_level", "repo") != "repo":
            rules.compile_all()
        _refresh(slug, root)
        store.log_action(store.connect(), "drop", {"slug": slug, "id": r["id"], "root": str(root) if root else None})
        n = seen.get("fired", 0)
        _say(f"Dropped {r['id']}. It is no longer given to agents.")
        _say(f"It had been given in {n} session{'s' if n != 1 else ''}" + (f", the last {seen['last']}." if n and seen.get("last") else "."))
        _say(f"\nUndo: {BIN} undo")
        return 0
    if a.action == "promote":
        return _rules_promote(a, slug, root)
    if a.action == "lift":
        return _rules_lift(a, slug, root)
    if a.all_levels:
        all_rules = rules.all_levels(slug)
    if a.json:
        print(json.dumps([{k: v for k, v in r.items() if not k.startswith("_")} | {"level": r.get("_level", "repo")}
                          for r in all_rules]))
        return 0
    live = [r for r in all_rules if r.get("status") != "deprecated"]
    if not live:
        _say("No rules for this repo yet.")
        _next(f'{BIN} teach "..."')
        return 0
    use = _rule_usage(slug)
    for r in live:
        gl = (r.get("scope") or {}).get("path_globs") or []
        lvl = r.get("_level", "repo")
        enf = (r.get("enforcement") or {}).get("level", "instruct")
        u = use.get(r["id"], {})
        extra = (f"  [{lvl}{':' + str(r['_level_key']) if r.get('_level_key') else ''}]" if lvl != "repo" else "") \
            + (f"  {enf}" if enf != "instruct" else "") + (f"  ignored {u['ignored']}x" if u.get("ignored") else "")
        _say(f"  {r['id']:<34} {r.get('status', '?'):<10} {', '.join(gl) if gl else 'whole repo'}{extra}")
    if not a.all_levels:
        up = len(rules.inherited(slug))
        if up:
            _say(f"\n{up} more rule{'s' if up != 1 else ''} inherited from the user, agent, stack or owner level: {BIN} rules --all-levels")
    dead = len(all_rules) - len(live)
    if dead:
        _say(f"\n{dead} dropped rule{'s' if dead != 1 else ''} not shown.")
    _next(f"{BIN} rules show <id>")
    return 0


def _rule_usage(slug) -> dict:
    """Per rule: sessions it was given in, sessions where an edit went against it, and when it was last given."""
    out = {}
    try:
        con = store.connect()
        store.sync(con)
        for row in con.execute("SELECT e.raw, s.started FROM events e JOIN sessions s ON s.id = e.session_id "
                               "WHERE s.repo=? AND e.event IN ('rule_injected', 'rule_violation', 'rule_enforced')", (slug,)):
            ev = json.loads(row["raw"])
            if ev.get("event") == "rule_injected":
                for i in ev.get("rule_ids", []):
                    u = out.setdefault(i, {"fired": 0, "ignored": 0, "enforced": 0, "last": None})
                    u["fired"] += 1
                    u["last"] = _ago(max(row["started"] or 0, 0))
            elif ev.get("rule_id"):
                u = out.setdefault(ev["rule_id"], {"fired": 0, "ignored": 0, "enforced": 0, "last": None})
                u["ignored" if ev["event"] == "rule_violation" else "enforced"] += 1
    except Exception:
        pass
    return out


ENFORCEMENT = ("instruct", "remind", "check", "block")


def _rules_promote(a, slug, root):
    """Move a rule up the enforcement ladder: instruct -> remind -> check -> block. Needs a detector."""
    r = rules.find(slug, a.id or "")
    if not r:
        _say(f"No single rule matches {a.id!r}. Run: {BIN} rules")
        return 1
    enf = r.get("enforcement") or {}
    cur = enf.get("level", "instruct")
    to = a.pattern or ENFORCEMENT[min(ENFORCEMENT.index(cur) + 1, len(ENFORCEMENT) - 1)]
    if to not in ENFORCEMENT:
        _say(f"Levels: {', '.join(ENFORCEMENT)}")
        return 2
    det = (enf.get("detector") or {}).get("pattern")
    if to != "instruct" and not det:
        _say(f"{r['id']} has no detector, so nothing can check it. Give it one first:")
        _say(f'  {BIN} rules detect {r["id"]} "<regex of what the rule forbids>"')
        return 2
    # The promotion test: a detector that matches the codebase as it stands would fire on correct work.
    hits = []
    if det and root and to in ("check", "block"):
        gl = (r.get("scope") or {}).get("path_globs") or []
        g = subprocess.run(["git", "-C", str(root), "grep", "-lE", det, "--", *(gl or ["."])], capture_output=True, text=True)
        hits = [l for l in g.stdout.splitlines() if l.strip()]
    use = _rule_usage(slug).get(r["id"], {})
    _say(f"{r['id']}: {cur} -> {to}")
    _say({"remind": "  The rule is shown right after an edit that matches its detector.",
          "check": "  An edit that matches its detector is returned to the agent as an error to fix.",
          "block": "  An edit or shell command that matches its detector is refused before it lands.",
          "instruct": "  The rule is only given as context."}[to])
    _say(f"  Given in {use.get('fired', 0)} sessions, ignored in {use.get('ignored', 0)}.")
    if hits and not a.force:
        _say(f"\nNot promoted: the detector already matches {len(hits)} file{'s' if len(hits) != 1 else ''} in the repo "
             f"({', '.join(hits[:3])}), so it would fire on work that follows the codebase. Narrow the pattern, "
             f"or add --force.")
        return 1
    if to in ("check", "block") and not use.get("ignored") and not a.force:
        _say("\nNot promoted: the agent has not ignored this rule yet, so an instruction is enough. "
             "A rising ignored count is the cue. Add --force to promote anyway.")
        return 1
    if a.dry_run:
        return 0
    r.setdefault("enforcement", {})["level"] = to
    rules.save(slug, r)
    rules.commit(f"promote {r['id']} to {to}")
    if r.get("_level", "repo") != "repo":
        rules.compile_all()
    _refresh(slug, root)
    store.log_action(store.connect(), "promote", {"slug": slug, "id": r["id"], "from": cur, "to": to,
                                                  "root": str(root) if root else None})
    _say(f"\nDone. Claude Code, Codex and Antigravity enforce it from their next edit. Undo: {BIN} undo")
    return 0


def _rules_lift(a, slug, root):
    """Move a repo rule to a shared level, so every repo below inherits it."""
    r = rules.find(slug, a.id or "", levels=False)
    to = a.pattern
    if not r or r.get("status") != "active":
        _say(f"No single active repo rule matches {a.id!r}. Run: {BIN} rules")
        return 1
    if to not in ("owner", "stack", "user"):
        _say(f"Lift to one of: owner, stack, user.  Example: {BIN} rules lift {r['id']} stack")
        return 2
    d = (r.get("rule") or {}).get("directive", "")
    elsewhere = rules.similar_elsewhere(slug, d)
    proven = [(o, x) for o, x in elsewhere if ((x.get("evidence") or {}).get("replay") or {}).get("verdict") == "passes"]
    mine = ((r.get("evidence") or {}).get("replay") or {}).get("verdict") == "passes"
    stack = None
    if to == "stack":
        stack = (rules.stacks_for(slug) or [None])[-1]
        if not stack:
            _say("No stack detected for this repo; set `stacks: [...]` in its repo.yaml.")
            return 2
        # Evidence travels with the level: a wrong stack rule hurts every repo of that stack.
        if (len(proven) + mine) < 2 and not a.force:
            _say(f"Not lifted: a {stack} rule needs passing replays in at least two repos. "
                 f"It has {len(proven) + mine}" + (f" (also active, unreplayed, in {', '.join(o for o, _ in elsewhere[:3])})" if elsewhere else "")
                 + ". Replay it, or add --force.")
            return 1
    try:
        dest = rules.level_dir(to, slug, stack=stack)
    except ValueError as e:
        _say(f"Not lifted: {e}.")
        return 2
    where = {"owner": f"every repo of {rules.owner_of(slug)}", "stack": f"every {stack} repo", "user": "every repo you use"}[to]
    _say(f"{r['id']} will apply to {where}." + (f" The copies in {', '.join(o for o, _ in elsewhere)} are retired." if elsewhere else ""))
    if a.dry_run:
        return 0
    import yaml as _yaml
    up = {k: v for k, v in r.items() if not k.startswith("_")}
    up.setdefault("provenance", {})["lifted_from"] = [slug] + [o for o, _ in elsewhere]
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f"{r['id']}.yaml").write_text(_yaml.safe_dump(up, sort_keys=False, allow_unicode=True))
    for s_, x in [(slug, r)] + elsewhere:
        x = {k: v for k, v in x.items() if not k.startswith("_")}
        x["status"] = "deprecated"
        x.setdefault("provenance", {})["lifted_to"] = to
        rules.save(s_, x)
    rules.commit(f"lift {r['id']} to {to}")
    rules.compile_all()
    _refresh(slug, root)
    _say("Done.")
    return 0


def _running_session(slug):
    """The session state of the newest session in this repo that has not ended, if any."""
    d = paths.local() / "sessions"
    best = None
    for f in (d.glob("*.json") if d.is_dir() else []):
        try:
            st = json.loads(f.read_text())
        except Exception:
            continue
        if st.get("slug") == slug and (best is None or f.stat().st_mtime > best[0]):
            best = (f.stat().st_mtime, f, st)
    return (best[1], best[2]) if best else (None, None)


def cmd_off(a, on=False):
    slug, root = _ctx()
    if a.session:
        f, st = _running_session(slug)
        if not st:
            _say("No session found for this repo. Start your agent here first, or use  " + f"{BIN} off  for the whole repo.")
            return 1
        if a.dry_run:
            _say(f"Would {'resume' if on else 'pause'} rules for session {st['session_id'][:12]} only.")
            return 0
        st["paused"] = not on
        f.write_text(json.dumps(st))
        if on:
            _say(f"Rules are given again for the rest of session {st['session_id'][:12]}.")
        else:
            _say(f"No more rules for the rest of session {st['session_id'][:12]}. It is still recorded.")
            _say("Path-scoped rule files stay in place for your other sessions, so Claude Code may still read them.")
            _say(f"\nResume: {BIN} on --session")
        return 0
    flag = paths.off_flag(None if a.everywhere else slug)
    cap = paths.capture_off_flag(None if a.everywhere else slug)
    scope = "everywhere" if a.everywhere else f"for {slug}"
    cc = adapters.get("claude-code")
    if a.dry_run:
        _say(f"Would turn memory {'on' if on else 'off'} {scope}" + (", and recording too." if a.capture else "."))
        return 0
    if on:
        if flag.exists():
            flag.unlink()
        if cap.exists():  # `once on` always restarts recording
            cap.unlink()
        _refresh(slug, root)
        _say(f"Memory is on {scope}. Sessions are recorded.")
        return 0
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text(str(time.time()))
    if a.capture:
        cap.write_text(str(time.time()))
    ag = adapters.get("antigravity")
    removed = (cc.remove_native(root) + ag.remove_native(root)) if root else 0
    if root:
        cc.remove_skills(root)
        ag.remove_skills(root)
    _say(f"Memory is off {scope}. Rules are no longer given to agents"
         + (f"; {removed} generated rule file{'s' if removed != 1 else ''} removed." if removed else "."))
    _say("Sessions are not recorded either." if a.capture
         else f"Sessions are still recorded. To stop that too: {BIN} off --capture" + (" --everywhere" if a.everywhere else ""))
    _say(f"\nResume: {BIN} on" + (" --everywhere" if a.everywhere else ""))
    return 0


def cmd_undo(a):
    con = store.connect()
    act = store.last_action(con)
    if not act:
        _say("Nothing to undo.")
        return 1
    p = json.loads(act["payload"])
    root = Path(p["root"]) if p.get("root") else None
    if a.dry_run:
        _say(f"Would undo the last {act['kind']}: {p.get('id') or p.get('steps') or ''}".rstrip(": "))
        return 0
    if act["kind"] == "teach":
        f = Path(p["file"])
        if f.exists():
            f.unlink()
        rules.commit(f"undo teach {p['id']}")
        _say(f"Removed rule {p['id']}.")
    elif act["kind"] == "drop":
        r = rules.find(p["slug"], p["id"])
        if r:
            r["status"] = "active"
            rules.save(p["slug"], r)
            rules.commit(f"undo drop {p['id']}")
        _say(f"Restored rule {p['id']}.")
    elif act["kind"] == "review":
        r = rules.find(p["slug"], p["id"])
        if r:
            r["status"] = "candidate"
            r["rule"]["directive"] = p.get("draft") or r["rule"]["directive"]
            rules.save(p["slug"], r)
            rules.commit(f"undo review {p['id']}")
        _say(f"{p['id']} is a suggestion again.")
    elif act["kind"] == "tag":
        from . import hook
        hook._append(p["slug"], p["session"], {"schema": SCHEMA, "session_id": p["session"], "agent": p.get("agent"),
                                                "repo": p["slug"], "ts": time.time(), "event": "annotation_retracted",
                                                "annotation_id": p["annotation"]})
        store.sync(con)
        _say(f"Removed the tag on steps {p.get('steps')}.")
    elif act["kind"] == "promote":
        r = rules.find(p["slug"], p["id"])
        if r:
            r.setdefault("enforcement", {})["level"] = p.get("from", "instruct")
            rules.save(p["slug"], r)
            rules.commit(f"undo promote {p['id']}")
        _say(f"{p['id']} is back at level {p.get('from', 'instruct')}.")
    else:
        _say(f"Cannot undo {act['kind']} yet.")
        return 1
    con.execute("UPDATE actions SET undone=1 WHERE id=?", (act["id"],))
    con.commit()
    _refresh(p["slug"], root)
    rules.compile_all()
    return 0


def health(slug: str, timing: bool = True) -> list[tuple[str, bool, str]]:
    """The checks behind `once doctor`, also shown on the dashboard's Live screen."""
    checks = []
    found_now = _agents_found()
    for name in adapters.IMPLEMENTED:
        if name != "claude-code" and name not in found_now:
            continue
        ad = adapters.get(name)
        missing = [e for e, ok in ad.installed().items() if not ok]
        checks.append((f"{name} hooks", not missing, f"missing: {', '.join(missing)}" if missing else str(ad.settings_path())))
    py_ok = Path(sys.executable).exists()
    checks.append(("python for hooks", py_ok, sys.executable))
    checks.append(("memory folder", paths.repo_memory(slug).is_dir(), str(paths.repo_memory(slug))))
    idx_f = paths.index_file(slug)
    newest = max((Path(r["_file"]).stat().st_mtime for r in rules.load_all(slug)), default=0)
    fresh = idx_f.exists() and idx_f.stat().st_mtime >= newest
    checks.append(("rule index", fresh, "up to date" if fresh else "stale"))
    broken = [Path(r["_file"]).name for r in rules.load_all(slug) if "_error" in r]
    checks.append(("rule files", not broken, f"cannot read {', '.join(broken[:3])}: fix or delete" if broken
                   else "all readable"))
    errs = paths.error_log().read_text().splitlines() if paths.error_log().exists() else []
    checks.append(("hook errors", not errs, f"{len(errs)} logged; last: {errs[-1][:90]}" if errs else "none"))
    con = store.connect()
    store.sync(con)
    rows = store.sessions(con, None, 1)
    checks.append(("capture", bool(rows), f"last event {_ago(rows[0]['last_event'])}" if rows else "no sessions yet"))
    if "codex" in found_now and all(adapters.get("codex").installed().values()):
        try:
            n_cx = con.execute("SELECT COUNT(*) FROM sessions WHERE agent = 'codex'").fetchone()[0]
        except Exception:
            n_cx = 0
        checks.append(("codex capture", n_cx > 0, f"{n_cx} sessions" if n_cx else
                       "none yet: in codex, type /hooks and trust the hooks that run once.hook codex"))
    con.close()
    if "antigravity" in found_now and all(adapters.get("antigravity").installed().values()):
        ag = adapters.get("antigravity")
        checks.append(("antigravity mcp", ag.mcp_registered(), str(ag.mcp_path())))
    if timing:
        t0 = time.time()
        subprocess.run([sys.executable, "-m", "once.hook", "claude-code"], input="{}", text=True,
                       capture_output=True, env={**os.environ, "ONCE_DISABLE": "1"})
        ms = (time.time() - t0) * 1000
        checks.append(("hook start-up", ms < 150, f"{ms:.0f} ms (budget 150 ms)"))
    return checks


SOFT_CHECKS = ("capture", "codex capture")


def cmd_doctor(a):
    slug, root = _ctx()
    cc = adapters.get("claude-code")
    found_now = _agents_found()
    checks = health(slug)
    if a.fix:
        cc.install()
        for name in found_now:
            if name in adapters.IMPLEMENTED and name != "claude-code" and _installable(name):
                adapters.get(name).install()
                adapters.get(name).register_mcp()
        if root:  # drop snapshot refs older than the retention window
            cutoff = time.time() - 30 * 86400
            refs = subprocess.run(["git", "-C", str(root), "for-each-ref", "--format=%(refname) %(creatordate:unix)",
                                   "refs/once/"], capture_output=True, text=True).stdout.splitlines()
            for line in refs:
                name_, _, ts = line.rpartition(" ")
                if ts.isdigit() and int(ts) < cutoff:
                    subprocess.run(["git", "-C", str(root), "update-ref", "-d", name_], capture_output=True)
        _refresh(slug, root)
        if a.clear_errors and paths.error_log().exists():
            paths.error_log().unlink()
    if a.json:
        print(json.dumps([{"check": n, "ok": ok, "detail": d} for n, ok, d in checks]))
        return 0
    for n, ok, d in checks:
        mark = "ok " if ok else ("-- " if n in SOFT_CHECKS else "FIX")
        _say(f"  {mark}  {n:<20} {d}")
    if a.data:
        tr = paths.local() / "trajectories"
        size = sum(f.stat().st_size for f in tr.rglob("*.jsonl")) if tr.is_dir() else 0
        con = store.connect()
        n_s = con.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        red = sum(json.loads(r[0]).get("redactions", 0) for r in con.execute("SELECT raw FROM events WHERE raw LIKE '%redactions%'"))
        oldest = con.execute("SELECT MIN(started) FROM sessions").fetchone()[0]
        _say(f"\nData\n  Rules and playbooks   {paths.memory()}   (a git repo you own)")
        _say(f"  Sessions              {tr}   ({n_s} sessions, {size / 1024:.0f} KB"
             + (f", oldest {_ago(oldest)}" if oldest else "") + ")")
        _say(f"  Masked                {red} secret{'s' if red != 1 else ''} replaced before anything was written "
             "(keys, tokens, passwords, private keys)")
        _say(f"  Not stored            file contents, edit text (only hashes), model reasoning")
        _say(f"  Index and state       {paths.local()}")
        _say(f"  Expiry                sessions stay until  {BIN} purge [--older-than DAYS];"
             f" tree snapshots older than 30 days go with  {BIN} doctor --fix")
        _say(f"  Leaves the machine    nothing, unless you run  {BIN} share  or set a Slack webhook for the digest")
    bad = [n for n, ok, _ in checks if not ok and n not in SOFT_CHECKS]
    if a.fix:
        _say("\nRe-registered hooks and rebuilt the index.")
    elif bad:
        _next(f"{BIN} doctor --fix")
    return 0 if not bad or a.fix else 1


def cmd_uninstall(a):
    slug, root = _ctx()
    cc = adapters.get("claude-code")
    if a.dry_run:
        _say("Would remove: the hooks for every agent, the MCP server, the slash commands, the status line, "
             "the menu bar plugin, the editor extension, and the generated rule files and skills in this repo.")
        _say(f"Would keep: your rules and session history in {paths.home()}.")
        return 0
    removed = cc.uninstall()
    for name in adapters.IMPLEMENTED:
        if name != "claude-code" and _installable(name):
            try:
                if adapters.get(name).uninstall():
                    removed.append(f"({name})")
            except Exception:
                pass
    cc.register_mcp(remove=True)
    for other in ("codex", "antigravity"):
        if _installable(other):
            adapters.get(other).register_mcp(remove=True)
    cc.remove_commands()
    cc.remove_statusline()
    from . import ambient
    ambient.uninstall_menubar()
    for ed in EDITOR_DIRS:
        _remove_editor(ed)
    ag = adapters.get("antigravity")
    n = (cc.remove_native(root) + cc.remove_skills(root) + ag.remove_native(root) + ag.remove_skills(root)) if root else 0
    _say(f"Removed hooks for: {', '.join(removed) or 'none were installed'}")
    if n:
        _say(f"Removed {n} generated rule file{'s' if n != 1 else ''} from this repo.")
    _say(f"Kept your rules and session history in {paths.home()}. Delete that folder to remove everything.")
    return 0


def cmd_playbooks(a):
    from . import playbooks
    slug, root = _ctx()
    if a.action == "new":
        if not a.id:
            _say(f'Give it a short id.\n\nExample: {BIN} playbooks new add-endpoint "Use when adding an HTTP endpoint"')
            return 2
        if a.dry_run:
            _say(f"Would create playbook {a.id} in {playbooks.folder(slug)}")
            return 0
        f = playbooks.new(slug, a.id, a.description or "")
        _say(f"Created {f}\nEdit the steps, then run:  {BIN} playbooks sync")
        return 0
    if a.action == "show":
        pb = playbooks.find(slug, a.id or "")
        if not pb:
            _say(f"No single playbook matches {a.id!r}.")
            return 1
        _say(f"{pb['id']}: {pb['description']}\n\n{pb['body'].strip()}\n\nFile: {pb['_file']}")
        return 0
    pbs = playbooks.load_all(slug)
    if a.action == "sync":
        if a.dry_run:
            _say(f"Would publish {len([p_ for p_ in pbs if not p_.get('draft')])} playbook(s) as skills.")
            return 0
        rules.commit("playbooks")
        _refresh(slug, root)
        _say(f"{len(pbs)} playbook{'s' if len(pbs) != 1 else ''} published to your agent as skills.")
        return 0
    if a.json:
        print(json.dumps([{k: v for k, v in p.items() if not k.startswith("_")} for p in pbs]))
        return 0
    if not pbs:
        _say("No playbooks for this repo yet.")
        _next(f'{BIN} playbooks new add-endpoint "Use when adding an HTTP endpoint"')
        return 0
    for p in pbs:
        _say(f"  {p['id']:<28} {detectors._short(p['description'], 70)}")
    return 0


def cmd_ui(a):
    import webbrowser
    from . import ui
    slug, _ = _ctx()
    if getattr(a, "detach", False):
        from . import ambient
        base = ambient.ensure_ui(a.port, os.getcwd())
        url = f"{base}/live?repo={slug}"
        if not a.no_open:
            webbrowser.open(url)
        _say(url)
        return 0
    try:
        srv = ui.serve(slug, lambda s: _refresh(s), a.port)
    except OSError:
        _say(f"Port {a.port} is in use. The viewer may already be running at http://127.0.0.1:{a.port}/\n\nOr: {BIN} ui --port {a.port + 1}")
        return 1
    url = f"http://127.0.0.1:{srv.server_address[1]}/live?repo={slug}"
    _say(f"Viewer running at {url}\nLocal only. Ctrl-C to stop.")
    if not a.no_open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        _say("\nStopped.")
    return 0


def _confirm(a, question="Continue? [y/N] "):
    if a.yes:
        return True
    if not sys.stdin.isatty() or input("\n" + question).strip().lower() not in ("y", "yes"):
        _say("Nothing ran. Add --yes to skip this question.")
        return False
    return True


def cmd_lab_tasks(a):
    from . import tasks
    slug, root = _ctx()
    if not root:
        _say("Run this from inside a git repo.")
        return 1
    action = a.rule
    if action == "mine":
        _say(f"Mining the last {a.limit} merged pull requests of {slug} ...")
        try:
            st = tasks.mine(slug, root, a.limit)
        except tasks.MineError as e:
            _say(f"\nCould not mine: {e}")
            return 1
        rules.commit(f"mine tasks {slug}")
        _say(f"\nKept {st['kept']} of {st['merged']} merged PRs as tasks.")
        _say(f"  Skipped: {st['no_issue']} with no linked issue, {st['no_tests']} with no test and source change, "
             f"{st['too_big']} too large, {st['already']} already mined")
        if st["kept"] or st["already"]:
            _next(f"{BIN} lab tasks validate    keep only tasks that fail before the fix and pass after")
        return 0
    all_t = tasks.load_all(slug)
    if action == "validate":
        todo = [t for t in all_t if (a.arg2 and (str(t["pr"]) == a.arg2.lstrip("#") or t["id"].endswith(a.arg2)))
                or (not a.arg2 and t.get("valid") is None)]
        todo = todo[-a.limit:] if not a.arg2 else todo
        if not todo:
            _say("Nothing to validate." if all_t else f"No tasks yet. Run: {BIN} lab tasks mine")
            return 0
        _say(f"Validating {len(todo)} task{'s' if len(todo) != 1 else ''}: each is checked before and after its fix.")
        good = 0
        for t in todo:
            res = tasks.validate(slug, root, t)
            good += bool(res["valid"])
            _say(f"  #{t['pr']:<6} {'valid  ' if res['valid'] else 'dropped' if res['valid'] is False else 'skipped'} {res['why']}")
            if a.verbose and not res["valid"]:
                _say("           " + (res.get("after_tail") or res.get("before_tail") or "").strip().replace("\n", "\n           ")[-500:])
        tasks.assign_split(slug)
        rules.commit(f"validate tasks {slug}")
        _say(f"\n{good} of {len(todo)} valid.")
        if good:
            _next(f"{BIN} lab sweep    run the agent once on every valid train task")
        return 0
    if a.json:
        print(json.dumps([{k: v for k, v in t.items() if not k.startswith("_")} for t in all_t]))
        return 0
    if not all_t:
        _say(f"No tasks mined for this repo yet.")
        _next(f"{BIN} lab tasks mine")
        return 0
    for t in all_t:
        state = {True: "valid", False: "dropped", None: "unchecked"}[t.get("valid")]
        _say(f"  #{t['pr']:<6} {str(t.get('merged_at'))[:10]}  {state:<9} {str(t.get('split') or '-'):<8} "
             f"{detectors._short(t.get('title'), 54)}")
    v = sum(1 for t in all_t if t.get("valid"))
    _say(f"\n{len(all_t)} tasks, {v} valid, {sum(1 for t in all_t if t.get('valid') is None)} unchecked.")
    return 0


def _print_run(res, verbose=False):
    verdict = {True: "PASSED the PR's tests", False: "FAILED the PR's tests", None: "not checked"}[res["verified"]]
    _say(f"\n{verdict}")
    _say(f"  {res['steps']} steps, {res['failed_steps']} failed, {res['files_changed']} files changed, {res['seconds']}s"
         + (f", ${res['cost_usd']:.2f}" if res.get("cost_usd") else ""))
    if res.get("missed_files"):
        _say(f"  The real fix also changed: {', '.join(res['missed_files'])}")
    if res.get("wrote_tests") is False:
        _say("  The real fix added tests; the agent wrote none.")
    if not res["captured"]:
        _say(f"  The session was not captured. Run: {BIN} doctor")
    if res["verified"] is False and verbose:
        _say("\n" + res["check_tail"].strip())


def cmd_lab_run(a):
    from . import experiment as ex, tasks
    slug, root = _ctx()
    task = tasks.find(slug, a.rule or "") if root else None
    if not task:
        _say(f"No single task matches {a.rule!r}. Run: {BIN} lab tasks")
        return 1
    arm = "rules" if a.with_rules else (a.arms or "bare")
    _say(f"Task #{task['pr']}: {detectors._short(task.get('title'), 64)}")
    _say(f"  From      issue #{task['issue']}, at commit {task['base_commit'][:9]}  ({task.get('split') or 'no split'})")
    _say(f"  Arm       {arm}")
    _say(f"  Check     {task.get('check') or 'none'}")
    _say(f"  Model     {a.model or 'your Claude Code default'}")
    _say(f"  Spend     up to ${a.budget:.2f}")
    if task.get("valid") is not True:
        _say(f"  Note      this task is not validated; run  {BIN} lab tasks validate {task['pr']}  first to trust the check.")
    if task.get("split") == "heldout":
        _say("  Note      this is a held-out task. Looking at its result before the experiment weakens the experiment.")
    _say("\nThe agent works in a throwaway copy with no history, with edit and shell access. Not a container.")
    if a.dry_run or not _confirm(a):
        return 0 if a.dry_run else 1
    _say("\nRunning ...")
    try:
        res = ex.run_task(slug, root, task, arm, a.model, a.budget, a.timeout, _agent_cmd(a), a.keep,
                          agent=a.agent or "claude-code")
    except ValueError as e:
        _say(f"Could not run: {e}")
        return 1
    store.sync()
    if a.json:
        print(json.dumps({k: v for k, v in res.items() if k != "check_tail"}))
        return 0
    if "error" in res:
        _say(f"Could not run: {res['error']}")
        return 1
    _print_run(res, a.verbose)
    _next(f"{BIN} last    see what the agent did, and tag what went wrong")
    return 0


def _report(m, rows):
    from . import experiment as ex
    sm = ex.summarize(m, rows)
    fp = m["fingerprint"]
    _say(f"{m['id']}   {m['slug']}")
    if sm.get("excluded"):
        _say(f"  {sm['excluded']} run{'s' if sm['excluded'] != 1 else ''} left out: the agent did no work in them, so they say nothing about the task.")
    _say(f"  {len(m['tasks'])} tasks, {m['repeats']} repeat{'s' if m['repeats'] != 1 else ''}, "
         f"{sm['total_runs']} of {sm['planned']} runs done")
    _say(f"  Agent {fp['agent']}, model {fp['model']}, once v{fp['meta_agent']}, rules at {fp['rules_commit'] or 'none'}\n")
    _say(f"  {'Arm':<10} {'Runs':>4}  {'Verified':>16}  {'Steps':>6}  {'Failed':>6}  {'Missed files':>12}  {'Cost':>7}  {'Per verified':>12}")
    for a_, v in sm["arms"].items():
        ci = f"{100 * v['rate']:.0f}% ({100 * v['ci'][0]:.0f}-{100 * v['ci'][1]:.0f})"
        cpv = f"${v['cost_per_verified']:.2f}" if v["cost_per_verified"] else "-"
        _say(f"  {a_:<10} {v['runs']:>4}  {ci:>16}  {v['steps']:>6.1f}  {v['failed_steps']:>6.1f}  "
             f"{100 * v['missed_rate']:>11.0f}%  {('$%.2f' % v['cost']) if v['cost'] else '-':>7}  {cpv:>12}")
    for p_ in sm["pairs"]:
        _say(f"\n  {p_['arm']} against {p_['against']}, task by task: better on {p_['better']}, worse on {p_['worse']}, "
             f"same on {p_['same']} (sign test p = {p_['p']:.2f}); {p_['steps_diff']:+.1f} steps on average")
    if sm["arms"]:
        _say("\n  Verified shows a 95% interval. With few tasks the intervals are wide; read differences accordingly.")
    return sm


def cmd_lab_sweep(a):
    """Run every valid task of one split once, bare: the quick way to find tasks worth learning from."""
    from . import experiment as ex, replay as rp, tasks
    a.split = a.split or "train"
    slug, root = _ctx()
    if not root:
        _say("Run this from inside a git repo.")
        return 1
    agent_cmd = _agent_cmd(a)
    got = ex.load("sweep") if a.resume else None
    if got:
        m, rows = got
    else:
        ts = [t for t in tasks.load_all(slug) if t.get("valid") and t.get("split") == a.split][: a.limit]
        if not ts:
            _say(f"No valid {a.split} tasks. Run: {BIN} lab tasks mine  then  {BIN} lab tasks validate")
            return 1
        cap = a.max_cost if a.max_cost is not None else a.budget * len(ts)
        _say(f"Sweep: {len(ts)} {a.split} tasks, once each, no rules.")
        _say(f"  Model     {a.model or 'your Claude Code default'}")
        _say(f"  Spend     up to ${a.budget:.2f} per task, stopping at ${cap:.2f} in total")
        _say("\nEach run works in a throwaway copy with edit and shell access. Not a container.")
        if a.dry_run or not _confirm(a):
            return 0 if a.dry_run else 1
        m, rows = ex.create(slug, [t["id"] for t in ts], ["bare"], 1, a.model, a.budget, agent_cmd, "sweep",
                            agent=a.agent or "claude-code"), []
    _say("")
    out = ex.run(m, root, a.max_cost if a.max_cost is not None else a.budget * len(m["tasks"]), agent_cmd,
                 force_mixed=a.force_mixed)
    store.sync()
    m, rows = ex.load(m["id"])
    _say("")
    _report(m, rows)
    if out.get("stopped"):
        _say(f"\nStopped: {out['stopped']}. Continue with: {BIN} lab sweep --resume")
    rows = [r for r in rows if not ex.did_no_work(r)]
    interesting = [r for r in rows if r.get("verified") is False or r.get("missed_files") or r.get("failed_steps", 0) >= 2]
    if interesting:
        _say(f"\n{len(interesting)} run{'s' if len(interesting) != 1 else ''} worth a look (failed, missed files, or repeated failures):")
        for r in interesting[:12]:
            why = [w for w, c in (("failed tests", r.get("verified") is False), (f"missed {', '.join(r.get('missed_files') or [])}", r.get("missed_files")),
                                  (f"{r.get('failed_steps')} failed steps", r.get("failed_steps", 0) >= 2)) if c]
            _say(f"  #{r['pr']:<6} {'; '.join(why)}")
        _next(f"{BIN} learn    turn these into suggested rules, or browse them with  {BIN} ui")
    elif rows:
        _say("\nNothing failed and nothing was missed. This repo's small tasks may leave little to learn; try a larger or less familiar repo.")
    return 0


def cmd_lab_exp(a):
    from . import experiment as ex, replay as rp, tasks
    a.split = a.split or "heldout"
    slug, root = _ctx()
    if not root:
        _say("Run this from inside a git repo.")
        return 1
    agent_cmd = _agent_cmd(a)
    action = a.rule
    if action == "report":
        got = ex.load(a.arg2)
        if not got:
            _say("No experiment found.")
            return 1
        if a.json:
            print(json.dumps(ex.summarize(*got)))
        else:
            _report(*got)
        return 0
    all_t = [t for t in tasks.load_all(slug) if t.get("valid")]
    if action == "baseline":
        train = [t for t in all_t if t.get("split") == "train"]
        held = [t for t in all_t if t.get("split") == "heldout"]
        if a.arg2 == "generated":
            cutoff = (held[0] if held else all_t[-1])["base_commit"] if all_t else "HEAD"
            _say(f"The agent will write its own instruction file from the repo at {cutoff[:9]}, the training cutoff.")
            _say(f"  Spend     up to ${a.budget:.2f}")
            if a.dry_run or not _confirm(a):
                return 0 if a.dry_run else 1
            f = ex.prepare_generated(slug, root, cutoff, a.model, a.budget, agent_cmd)
            _say(f"Saved {f}" if f else "The agent did not produce a CLAUDE.md.")
            return 0 if f else 1
        if a.arg2 == "native":
            _say(f"Built-in memory baseline: {len(train)} train tasks run in merge order with one memory folder, then frozen.")
            _say("Experimental: it relies on Claude Code honouring the autoMemoryDirectory setting in headless runs.")
            _say(f"  Spend     up to ${a.budget:.2f} per task, ${a.budget * len(train):.2f} in total")
            if a.dry_run or not _confirm(a):
                return 0 if a.dry_run else 1
            d = ex.prepare_native(slug, root, train, a.model, a.budget, agent_cmd)
            n = sum(1 for _ in d.rglob("*") if _.is_file())
            _say(f"Frozen at {d} ({n} files)." + ("" if n else " It is empty: built-in memory wrote nothing, so this arm would equal bare."))
            return 0
        _say(f"Choose one: {BIN} lab exp baseline generated   or   {BIN} lab exp baseline native")
        return 2
    if action == "resume":
        got = ex.load(a.arg2)
        if not got:
            _say("No experiment to resume.")
            return 1
        m, rows = got
    elif action == "run":
        arms = [x.strip() for x in (a.arms or "bare,rules").split(",") if x.strip()]
        ts = [t for t in all_t if t.get("split") == a.split][: a.limit]
        if not ts:
            _say(f"No valid {a.split} tasks.")
            return 1
        try:
            for arm in arms:
                cfg = ex.arm_setup(slug, arm)
                if cfg.get("_tmp"):
                    import shutil as _sh
                    _sh.rmtree(cfg["_tmp"], ignore_errors=True)
        except ValueError as e:
            _say(f"Cannot start: {e}")
            return 1
        total = len(ts) * len(arms) * a.repeats
        cap = a.max_cost if a.max_cost is not None else a.budget * total
        _say(f"Experiment on {slug}: {len(ts)} {a.split} tasks x {len(arms)} arms ({', '.join(arms)}) x {a.repeats} repeats = {total} runs")
        _say(f"  Model     {a.model or 'your Claude Code default'}")
        _say(f"  Spend     up to ${a.budget:.2f} per run, stopping at ${cap:.2f} in total")
        if any(x in arms for x in ("rules", "unreviewed", "rules+native")):
            n = len(rules.active(slug))
            _say(f"  Rules     {n} active. For a fair test they must come from train tasks only, never from held-out ones.")
        if "unreviewed" in arms:
            n = sum(1 for r in rules.load_all(slug) if r.get("status") == "candidate")
            _say(f"  Unreviewed  the active rules plus {n} suggestion{'s' if n != 1 else ''} still waiting for review")
        if a.split != "heldout":
            _say("  Note      this is not the held-out split, so it measures fit, not generalization.")
        _say("\nArms are shuffled within each task. Runs work in throwaway copies. Not a container. Resumable.")
        if a.dry_run or not _confirm(a):
            return 0 if a.dry_run else 1
        m, rows = ex.create(slug, [t["id"] for t in ts], arms, a.repeats, a.model, a.budget, agent_cmd,
                            agent=a.agent or "claude-code"), []
    else:
        _say(f"{BIN} lab exp run [--arms bare,rules,generated,native] [--repeats 3] [--split heldout] [--max-cost N]\n"
             f"{BIN} lab exp resume [id]\n{BIN} lab exp report [id]\n{BIN} lab exp baseline generated|native")
        return 0
    _say("")
    planned = len(m["tasks"]) * len(m["arms"]) * m["repeats"]
    out = ex.run(m, root, a.max_cost if a.max_cost is not None else m["budget"] * planned, agent_cmd, force_mixed=a.force_mixed)
    store.sync()
    _say("")
    _report(*ex.load(m["id"]))
    if out.get("stopped"):
        _say(f"\nStopped: {out['stopped']}. Continue with: {BIN} lab exp resume {m['id']}")
    return 0


def cmd_lab_stats(a):
    from . import learn
    st = learn.stats()
    if a.json:
        print(json.dumps(st))
        return 0
    if not st:
        _say(f"No suggestions drafted yet. Run: {BIN} learn")
        return 0
    _say(f"  {'Source':<14} {'Queued':>6} {'Skipped':>7} {'Dupes':>5} {'Lint':>4}  {'Accepted':>8} {'Edited':>6} {'Rejected':>8}  {'Kept':>5} {'As written':>10}")
    for src, b in sorted(st.items()):
        kept = f"{100 * b['acceptance']:.0f}%" if b["acceptance"] is not None else "-"
        asw = f"{100 * b['as_written']:.0f}%" if b["as_written"] is not None else "-"
        _say(f"  {src:<14} {b['queued']:>6} {b['skipped']:>7} {b['duplicate']:>5} {b['lint_failed']:>4}  "
             f"{b['accepted']:>8} {b['accepted_with_edit']:>6} {b['rejected']:>8}  {kept:>5} {asw:>10}")
    _say("\nKept is the share of reviewed suggestions you accepted. This table is what the pipeline will tune itself on.")
    return 0


def cmd_lab_bootstrap(a):
    """Cold-start mining: suggestions from the repo's own config, before any session."""
    from . import bootstrap
    slug, root = _ctx()
    if not root:
        _say("Run this from inside a git repo.")
        return 1
    res = bootstrap.run(slug, root, dry_run=a.dry_run)
    if a.json:
        print(json.dumps(res))
        return 0
    if res["cutoff"]:
        _say(f"Reading the repo as of {res['cutoff'][:9]}, the held-out cutoff, so no held-out answer leaks in.")
    if not res["candidates"]:
        _say("Nothing to suggest from this repo's CI, lint or lockfiles" + (" that it does not already document." if res["dropped"] else "."))
    else:
        _say(f"{'Would suggest' if a.dry_run else 'Suggested'} {len(res['candidates'])} rule{'s' if len(res['candidates']) != 1 else ''}"
             f" from the repo's own config (no model calls):")
        for c in res["candidates"]:
            _say(f"  - {c['directive']}\n      from {c['evidence']}" + (f", signal seen {c['recur']}x" if c["recur"] > 1 else ""))
    for d in res["dropped"]:
        _say(f"  skipped: {detectors._short(d['directive'], 60)} ({d['why']})")
    for f in res["findings"]:
        _say(f"\n{f}")
    if res["written"]:
        _say("\nThey are suggestions, not yet replayed. Nothing is active until you accept it.")
        _next(f"{BIN} review")
    return 0


def cmd_lab_meta(a):
    from . import meta
    return meta.cli(a, _say, _next, _confirm, _ctx, _refresh)


def cmd_lab_cost(a):
    from . import cost
    return cost.cli(a, _say, _ctx)


def cmd_lab_cochange(a):
    """Files that change together reveal procedures. No model, no sessions."""
    slug, root = _ctx()
    if not root:
        _say("Run this from inside a git repo.")
        return 1
    log = subprocess.run(["git", "-C", str(root), "log", f"-{a.limit * 10}", "--no-merges", "--name-only", "--format=tformat:@@"],
                         capture_output=True, text=True).stdout
    commits = [[f for f in c.split() if f] for c in log.split("@@") if c.strip()]
    commits = [c for c in commits if 2 <= len(c) <= 15]
    count, pair = {}, {}
    for c in commits:
        for f in c:
            count[f] = count.get(f, 0) + 1
        for x in c:
            for y in c:
                if x != y:
                    pair[(x, y)] = pair.get((x, y), 0) + 1
    out = []
    for f, n in sorted(count.items(), key=lambda kv: -kv[1]):
        if n < 5:
            continue
        mates = sorted(((y, k / n) for (x, y), k in pair.items() if x == f and k / n >= 0.6 and k >= 4), key=lambda t: -t[1])
        if mates:
            out.append((f, n, mates[:5]))
    if a.json:
        print(json.dumps([{"file": f, "commits": n, "with": m} for f, n, m in out]))
        return 0
    if not out:
        _say("No strong co-change patterns in recent history.")
        return 0
    if a.write:
        from . import playbooks
        n = 0
        for f, cnt, mates in out[:8]:
            pid = "change-" + "".join(c if c.isalnum() else "-" for c in Path(f).stem.lower()).strip("-")
            dest = playbooks.folder(slug) / f"{pid}.md"
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            steps = [f"1. Make the change in `{f}`."] + [f"{i}. Update `{y}` (changed alongside it in {100 * c:.0f}% of {cnt} commits)."
                                                         for i, (y, c) in enumerate(mates, 2)]
            dest.write_text(f"---\nid: {pid}\ndescription: Use when changing {f}.\ndraft: true\nscope: [\"{f}\"]\n---\n"
                            + "\n".join(steps) + "\n")
            n += 1
        _say(f"Wrote {n} draft playbook{'s' if n != 1 else ''} to {playbooks.folder(slug)}.")
        _say(f"Drafts are not given to agents. Edit one, remove its `draft: true` line, then run  {BIN} playbooks sync")
        return 0
    _say(f"From {len(commits)} recent commits. When the file on the left changes, these usually change with it:\n")
    for f, n, mates in out[:15]:
        _say(f"  {f}  ({n} commits)")
        for y, c in mates:
            _say(f"      {100 * c:>3.0f}%  {y}")
    _next(f'{BIN} playbooks new <id> "Use when ..."    write the pattern down as a playbook')
    return 0


def cmd_lab_regress(a):
    """Rerun every rule's replay: which rules are still needed, on this agent and model?"""
    from . import regress
    slug, root = _ctx()
    if not root:
        _say("Run this from inside a git repo.")
        return 1
    agent = a.agent or "claude-code"
    todo = [r for r in regress.testable(slug) if not a.rule or r["id"].startswith(a.rule)]
    untest = len(rules.active(slug)) - len(regress.testable(slug))
    if not todo:
        _say(f"No active rule has a replay to rerun. Rules get one from:  {BIN} lab replay <rule> --prompt \"...\"")
        return 0
    runs = a.runs * 2 * len(todo)
    _say(f"Regression run on {agent}: {len(todo)} rule{'s' if len(todo) != 1 else ''}, {runs} headless runs, up to ${a.budget * runs:.2f}")
    if untest:
        _say(f"  {untest} active rule{'s' if untest != 1 else ''} without replay evidence cannot be tested and are left alone.")
    if agent != "claude-code":
        _say("  A different agent than the rules were learned on: this measures how many rules transfer.")
    _say("  " + ("Harmful rules and rules obsolete twice in a row will be retired." if a.apply
                 else "Nothing is retired without --apply."))
    if a.dry_run or not _confirm(a):
        return 0 if a.dry_run else 1
    out = regress.run(slug, root, agent, _agent_cmd(a), a.model, a.budget, a.runs, a.rule, a.apply)
    _refresh(slug, root)
    if a.json:
        print(json.dumps(out))
        return 0
    counts = {}
    for r in out:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    _say("\n" + ", ".join(f"{n} {k}" for k, n in counts.items()))
    done = [r for r in out if r["state"] != "error"]
    if done and agent != "claude-code":
        _say(f"Transfer to {agent}: {sum(1 for r in done if r['state'] == 'needed')} of {len(done)} rules still fix their failure there.")
    return 0


def cmd_import(a):
    from . import importer
    slug, _ = _ctx()
    if a.dry_run:
        _say(f"Would import up to {a.limit} Claude Code transcripts from {importer.projects_dir()}"
             + ("" if a.all else f" for {slug}") + ".")
        return 0
    _say(f"Importing Claude Code transcripts from {importer.projects_dir()} ...")
    st = importer.run(a.limit, None if a.all else slug)
    store.sync()
    _say(f"{st['imported']} imported, {st['skipped_existing']} already captured, {st['skipped_other']} skipped "
         f"(other repos, empty or unreadable).")
    _say(f"Experimental: imported sessions come from an undocumented file format; check a few with  {BIN} last.")
    return 0


def cmd_config(a):
    known = {"auto_learn": "draft suggestions by itself after each session (spends money; needs daily_cost_cap)",
             "daily_cost_cap": "dollars per day that automatic drafting may spend (default 1.00)",
             "quiet": "no line at the end of a session", "snapshots": "tree snapshots at session start and end (default on)",
             "holdout_fraction": "share of ordinary sessions that get no rules, for measurement (default 0)",
             "teach_by_talking": "turn corrections you type to the agent into suggested rules (default on)",
             "ask_in_agent": "your agent asks you about suggested rules at the start of a session (default on)",
             "notifications": "desktop notification when rules are waiting or the digest is ready (default on)",
             "digest": "weekly | off: a summary of the week, to a file, a notification and Slack (default weekly)",
             "slack_webhook": "Slack incoming-webhook address for the weekly digest (default none)",
             "digest_email": "address the weekly digest is emailed to (needs smtp_host; default none)",
             "smtp_host": "mail server for the digest, e.g. smtp.gmail.com (port 465: SSL, else STARTTLS)",
             "smtp_port": "mail server port (default 587)",
             "smtp_user": "mail server login; the password is read from ONCE_SMTP_PASSWORD or smtp_password_cmd",
             "smtp_password_cmd": "a command that prints the password, e.g. security find-generic-password -s once-smtp -w",
             "digest_from": "the From address (default smtp_user)",
             "statusline_wrap": "the status line command to run before the `once` line (set by init)"}
    cfg = _cfg()
    if not a.key:
        for k, d in known.items():
            _say(f"  {k:<18} {json.dumps(cfg[k]) if k in cfg else '(default)':<10} {d}")
        return 0
    if a.key not in known:
        _say(f"Unknown setting {a.key!r}. Known: {', '.join(known)}")
        return 2
    if a.value is None:
        _say(json.dumps(cfg.get(a.key)))
        return 0
    if a.dry_run:
        _say(f"Would set {a.key} = {a.value}")
        return 0
    try:
        cfg[a.key] = json.loads(a.value)
    except Exception:
        cfg[a.key] = a.value
    _cfg_path().parent.mkdir(parents=True, exist_ok=True)
    _cfg_path().write_text(json.dumps(cfg, indent=2))
    _say(f"{a.key} = {json.dumps(cfg[a.key])}")
    return 0


def cmd_export(a):
    """A repo's reviewed rules as plain files, for people who never install this tool."""
    slug, root = _ctx()
    act = rules.active(slug)
    if not act:
        _say("No active rules to export.")
        return 1
    head = repo.head(root) if root else ""
    if a.format == "json":
        text = json.dumps({"repo": slug, "verified_at": head, "date": time.strftime("%Y-%m-%d"), "rules": [
            {"id": r["id"], "directive": r["rule"]["directive"], "rationale": r["rule"].get("rationale"),
             "path_globs": (r.get("scope") or {}).get("path_globs") or [], "kind": r.get("kind"),
             "replay": {k: v for k, v in ((r.get("evidence") or {}).get("replay") or {}).items()
                        if k in ("verdict", "runs", "without_rule", "with_rule")} or None} for r in act]}, indent=2)
    else:
        def ev_(r):
            rp_ = (r.get("evidence") or {}).get("replay")
            return f" _(replayed {rp_['runs']}x: failed {rp_['without_rule']['failed']} without, {rp_['with_rule']['failed']} with)_" \
                if rp_ and rp_.get("verdict") == "passes" else ""
        wide = [r for r in act if not ((r.get("scope") or {}).get("path_globs"))]
        scoped = [r for r in act if (r.get("scope") or {}).get("path_globs")]
        ranked = sorted(act, key=lambda r: ((r.get("evidence") or {}).get("replay") or {}).get("verdict") != "passes")
        keep = {r["id"] for r in ranked[: a.max]}
        lines = ["## Repository conventions", "",
                 f"Reviewed conventions for this repository, checked at commit {head or 'unknown'} on {time.strftime('%Y-%m-%d')}.", ""]
        lines += [f"- {r['rule']['directive']}{ev_(r)}" for r in wide if r["id"] in keep]
        for r in scoped:
            if r["id"] in keep:
                lines += ["", f"### Files matching `{', '.join(r['scope']['path_globs'])}`", "", f"- {r['rule']['directive']}{ev_(r)}"]
        if len(act) > len(keep):
            lines += ["", f"{len(act) - len(keep)} further rules were left out to keep this short."]
        text = "\n".join(lines) + "\n"
    if a.output and a.dry_run:
        _say(f"Would write {len(act)} rules to {a.output}.")
        return 0
    if a.output:
        Path(a.output).write_text(text)
        _say(f"Wrote {len(act)} rules to {a.output}. Nothing from your machine's sessions is included.")
    else:
        print(text)
    return 0


def _verify_candidate(slug, root, rule, a) -> str:
    """Replay a suggestion on the session it came from, when that session can be rerun."""
    from . import replay as rp
    hint = (rule.get("provenance") or {}).get("replay_hint") or {}
    if not root or not hint.get("prompt") or not hint.get("commit"):
        return "no replayable session behind it"
    have = subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{hint['commit']}^{{commit}}"], capture_output=True)
    if have.returncode != 0:
        return "its starting commit is not in this clone"
    res = rp.replay(slug, root, rule, hint["prompt"], hint["commit"], 3, None, hint.get("signature"), _agent_cmd(a),
                    getattr(a, "budget", 0.5) * 2, model=getattr(a, "model", None), progress=lambda *_: None)
    return rp.VERDICTS.get(res["verdict"], res.get("error", res["verdict"]))


def cmd_learn(a):
    from . import learn
    slug, root = _ctx()
    eps = learn.episodes(slug)
    if not eps:
        _say("Nothing new to learn from. Episodes come from tagged steps, repeated failures, files the agent missed, and review comments.")
        return 0
    model_eps = [e for e in eps if e["source"] != "missed_file"]
    counts = {}
    for e in eps:
        counts[e["source"]] = counts.get(e["source"], 0) + 1
    _say(f"{len(eps)} new episode{'s' if len(eps) != 1 else ''}: "
         + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in counts.items()))
    n = min(a.max, len(eps))
    _say(f"Drafting up to {a.max} suggested rules. {min(len(model_eps), a.max)} need a model call, up to ${a.budget:.2f} each.")
    _say("Nothing becomes active until you approve it.")
    if a.dry_run:
        for e in eps[:20]:
            _say(f"  {e['source'].replace('_', ' '):<14} {detectors._short(e.get('note') or e.get('file') or e.get('task') or e.get('session'), 60)}")
        return 0
    if a.auto:  # started by the session-end hook: stay under the daily cap, say nothing
        cap = float(_cfg().get("daily_cost_cap", 1.0))
        today = time.time() - 86400
        spent = sum(r.get("draft_cost") or 0 for r in learn.history() if r.get("ts", 0) >= today)
        if spent >= cap:
            return 0
        a.max = min(a.max, 3)
    if model_eps and not _confirm(a):
        return 1
    _say("")
    st = learn.learn(slug, root, a.max, a.drafter_cmd, a.budget)
    if a.replay and st["queued"]:
        _say("\nReplaying new suggestions on the sessions they came from (6 headless runs each):")
        for r in [r for r in rules.load_all(slug) if r.get("status") == "candidate" and not (r.get("evidence") or {}).get("replay")]:
            _say(f"  {r['id']}: {_verify_candidate(slug, root, r, a)}")
    _refresh(slug, root)
    if a.auto and st.get("queued"):
        from . import ambient
        n = st["queued"]
        ambient.notify("once", f"{n} suggested rule{'s' if n != 1 else ''} for {slug}. Your agent will ask you next session.",
                       f"http://127.0.0.1:{ambient.UI_PORT}/review?repo={slug}")
    if a.json:
        print(json.dumps(st))
        return 0
    if st.get("failed"):
        _say(f"\n{st['failed']} could not be drafted because the model call failed. They will be offered again next time.")
    _say(f"\n{st['queued']} suggested, {st['skipped']} skipped, {st['duplicates']} duplicates, {st['lint_failed']} failed lint"
         + (f", ${st['cost']:.2f} spent" if st["cost"] else ""))
    if st["queued"]:
        _next(f"{BIN} review    approve, edit or reject them")
    return 0


def _show_candidate(r, i=None, n=None):
    if i:
        _say(f"\nSuggestion {i} of {n}\n")
    _say(f"  {(r.get('rule') or {}).get('directive', '')}")
    gl = (r.get("scope") or {}).get("path_globs") or []
    pv = r.get("provenance") or {}
    _say(f"  Applies to   {', '.join(gl) if gl else 'whole repo'}")
    if (r.get("rule") or {}).get("rationale"):
        _say(f"  Why          {r['rule']['rationale']}")
    src = {"human_tag": "your tag on a session", "retry_cluster": "a repeated failure in a session",
           "missed_file": "files the agent missed in mined tasks", "pr_review": "maintainers' review comments",
           "chat": "something you told the agent in a session"}.get(pv.get("source_kind"), pv.get("source_kind"))
    _say(f"  From         {src}")
    rp_ = (r.get("evidence") or {}).get("replay")
    _say(f"  Evidence     replayed {rp_['runs']}x: failed {rp_['without_rule']['failed']} without it, {rp_['with_rule']['failed']} with it"
         if rp_ else f"  Evidence     not replayed yet   ({BIN} lab replay {r['id']} --prompt \"...\")")
    _say(f"  id           {r['id']}")


def cmd_review(a):
    from . import learn
    slug, root = _ctx()
    cands = [r for r in rules.load_all(slug) if r.get("status") == "candidate"]
    con = store.connect()

    def apply(r, decision, text=None, reason=None, secs=None):
        rec = learn.decide(slug, r, decision, _who(), text, reason, review_secs=secs)
        store.log_action(con, "review", {"slug": slug, "id": r["id"], "root": str(root) if root else None,
                                         "draft": rec["draft_text"]})
        _refresh(slug, root)
        return rec

    if a.accept or a.reject:
        r = rules.find(slug, a.accept or a.reject)
        if not r or r.get("status") != "candidate":
            _say(f"No single suggestion matches {(a.accept or a.reject)!r}. Run: {BIN} review --list")
            return 1
        if a.dry_run:
            _say(f"Would {'accept' if a.accept else 'reject'} {r['id']}.")
            return 0
        rec = apply(r, "accept" if a.accept else "reject", a.text, a.reason)
        _say(f"{rec['decision'].replace('_', ' ').capitalize()}: {r['id']}\n\nUndo: {BIN} undo")
        return 0
    if a.json:
        print(json.dumps([{k: v for k, v in r.items() if not k.startswith("_")} for r in cands]))
        return 0
    if not cands:
        _say("No suggested rules waiting.")
        if _pending_episodes(slug):
            _next(f"{BIN} learn    draft suggestions from {_pending_episodes(slug)} new episodes")
        return 0
    if a.list or not sys.stdin.isatty():
        for i, r in enumerate(cands, 1):
            _show_candidate(r, i, len(cands))
        _say(f"\nDecide with:  {BIN} review --accept <id>   or   {BIN} review --reject <id> --reason \"...\"")
        return 0
    for i, r in enumerate(cands, 1):
        _show_candidate(r, i, len(cands))
        t0 = time.time()  # review minutes per accepted rule is one of the measures the outer loop is judged on
        while True:
            ans = input("\n[a]ccept  [e]dit  [r]eject  [t]est by replay  [s]kip  [q]uit > ").strip().lower()[:1]
            if ans == "t":
                _say("  Replaying: 3 runs without it, 3 with it ...")
                _say("  " + _verify_candidate(slug, root, r, a))
                r = rules.find(slug, r["id"]) or r
                continue
            if ans == "a":
                apply(r, "accept", secs=time.time() - t0)
                _say("  Accepted. It is active now.")
            elif ans == "e":
                text = input("  New wording: ").strip()
                if not text:
                    continue
                apply(r, "accept", text, secs=time.time() - t0)
                _say("  Accepted with your wording.")
            elif ans == "r":
                why = input("  Why? (optional, one line; it trains the drafter) ").strip()
                apply(r, "reject", reason=why, secs=time.time() - t0)
                _say("  Rejected.")
            elif ans == "q":
                return 0
            elif ans != "s":
                continue
            break
    return 0


def cmd_lab(a):
    if a.what == "tasks":
        return cmd_lab_tasks(a)
    if a.what == "run":
        return cmd_lab_run(a)
    if a.what in ("sweep", "exp", "stats", "cochange", "regress", "bootstrap", "meta", "cost"):
        return {"sweep": cmd_lab_sweep, "exp": cmd_lab_exp, "stats": cmd_lab_stats, "cochange": cmd_lab_cochange,
                "regress": cmd_lab_regress, "bootstrap": cmd_lab_bootstrap, "meta": cmd_lab_meta,
                "cost": cmd_lab_cost}[a.what](a)
    if a.what != "replay":
        _say(f"Builder tools:\n  {BIN} lab tasks [mine|validate]    tasks from merged pull requests\n"
             f"  {BIN} lab run <task>               run the agent on one task and capture it\n"
             f"  {BIN} lab sweep                    run every valid train task once\n"
             f"  {BIN} lab replay <rule> --prompt   test a rule with and without it\n"
             f"  {BIN} lab exp run|resume|report|baseline   the held-out experiment\n"
             f"  {BIN} lab regress [rule]           rerun every rule's replay; --agent tests transfer\n"
             f"  {BIN} lab cochange [--write]       files that change together\n"
             f"  {BIN} lab stats                    how the learning pipeline is doing\n"
             f"  {BIN} lab bootstrap                first suggestions from CI, lint and lockfiles (no model)\n"
             f"  {BIN} lab meta [status|detectors|benchmark|drafter|ranking|priors|compare|report|promote|rollback]\n"
             f"                                    the outer loop: tune the pipeline from how its rules fared\n"
             f"  {BIN} lab cost [--init-prices]     tokens, spend, memory overhead, saving and break-even per rule")
        return 0 if not a.what else 1
    from . import replay as rp
    slug, root = _ctx()
    if not root:
        _say("Replay needs a git repo; run it from inside one.")
        return 1
    rule = rules.find(slug, a.rule or "")
    if not rule:
        _say(f"No single rule matches {a.rule!r}. Run: {BIN} rules")
        return 1
    if not a.prompt:
        _say(f'Replay needs the task to rerun.\n\nExample: {BIN} lab replay {rule["id"]} --prompt "the prompt that went wrong"')
        return 2
    check = a.check
    if check is None:
        try:
            import yaml
            check = (yaml.safe_load((paths.repo_memory(slug) / "repo.yaml").read_text()) or {}).get("check_command")
        except Exception:
            check = None
    if a.no_check:
        check = None
    total = a.runs * 2
    _say(f"Replay of rule {rule['id']}")
    _say(f"  Task      {detectors._short(a.prompt, 70)}")
    _say(f"  At        {a.commit}")
    _say(f"  Runs      {a.runs} without the rule, {a.runs} with it ({total} headless agent runs)")
    _say(f"  Failure   {'an error matching /' + a.signature + '/' if a.signature else ('the check command failing' if check else 'any failed step')}")
    _say(f"  Check     {check or 'none'}")
    _say(f"  Model     {a.model or 'your Claude Code default'}")
    _say(f"  Spend     up to ${a.budget:.2f} per run, ${a.budget * total:.2f} in total")
    _say("\nEach run works in a throwaway copy of the repo with no history, with edit and shell access.")
    _say("This is not a container: only replay code you trust.")
    if a.dry_run:
        return 0
    if not _confirm(a):
        return 1
    _say("")
    res = rp.replay(slug, root, rule, a.prompt, a.commit, a.runs, check, a.signature, _agent_cmd(a),
                    a.budget, a.timeout, a.keep, model=a.model, save=not a.agent or a.agent == "claude-code")
    if a.json:
        print(json.dumps(res))
        return 0 if res["verdict"] == "passes" else 1
    if res["verdict"] == "error":
        _say(f"\nCould not run: {res['error']}")
        return 1
    ev = res["evidence"]
    _say(f"\nWithout the rule: failure in {ev['without_rule']['failed']} of {ev['runs']} runs")
    _say(f"With the rule:    failure in {ev['with_rule']['failed']} of {ev['runs']} runs")
    _say(f"Average steps:    {ev['steps']['without']} without, {ev['steps']['with']} with")
    if ev.get("cost_usd"):
        _say(f"Spent:            ${ev['cost_usd']:.2f}")
    _say(f"\n{rp.VERDICTS[res['verdict']]}")
    _say(f"Saved to the rule as evidence. See: {BIN} rules show {rule['id']}")
    return 0 if res["verdict"] == "passes" else 1


# ------------------------------------------------------------------ ambient: where the developer already is

def cmd_capture(a):
    """Hidden: started by the hook when a prompt reads like a correction."""
    from . import ambient
    slug, root = _ctx()
    r = ambient.capture_correction(slug, root, a.text or "", a.session, a.agent)
    if a.json:
        print(json.dumps({"id": r.get("id") if r else None, "duplicate_of": r.get("duplicate_of") if r else None}))
    return 0


def cmd_statusline(a):
    from . import ambient
    return ambient.statusline_main()


def cmd_menubar(a):
    from . import ambient
    if (a.install or a.uninstall) and a.dry_run:
        _say(f"Would {'add' if a.install else 'remove'} the menu bar plugin.")
        return 0
    if a.install:
        f = ambient.install_menubar()
        if not f:
            _say("No SwiftBar or xbar found. The menu bar icon needs one of them (both are free):")
            _say("  brew install --cask swiftbar")
            _say(f"Then open SwiftBar once, choose a plugin folder, and run  {BIN} menubar --install  again.")
            return 1
        _say(f"Added the menu bar icon ({f}). It refreshes every minute.")
        _say("If it does not appear, choose Refresh All in the SwiftBar menu.")
        return 0
    if a.uninstall:
        gone = ambient.uninstall_menubar()
        _say(f"Removed {len(gone)} menu bar plugin{'s' if len(gone) != 1 else ''}.")
        return 0
    sys.stdout.write(ambient.menubar())
    return 0


def cmd_digest(a):
    from . import ambient
    text, w = ambient.digest_text(a.days)
    if a.json:
        print(json.dumps(w, default=str))
        return 0
    if a.send:
        went = ambient.send_digest(text)
        if not a.quiet:
            _say(text)
            _say(f"\nSent to: {', '.join(went)}")
        return 0
    _say(text)
    if not ambient.config().get("slack_webhook") and not ambient.config().get("digest_email"):
        _say(f"\nTo get this in Slack every week:  {BIN} config slack_webhook https://hooks.slack.com/services/..."
             f"\nOr by email:                     {BIN} config digest_email you@example.com  (and smtp_host, smtp_user)")
    return 0


def cmd_share(a):
    from . import team
    slug, root = _ctx()
    if not root:
        _say("Run this from inside a git repo.")
        return 1
    plan = team.share(slug, root, dry_run=True)
    if not plan["rules"]:
        _say(f"Nothing to share: every active rule on this machine is already in {rules.SHARED_DIR}/ (or there are none).")
        return 0
    if a.json and a.dry_run:
        print(json.dumps(plan))
        return 0
    _say(f"{plan['rules']} rule{'s' if plan['rules'] != 1 else ''} to share with the team:")
    for rid in plan["ids"]:
        r = rules.find(slug, rid) if hasattr(rules, "find") else None
        _say(f"  - {((r or {}).get('rule') or {}).get('directive', rid)}")
    _say(f"\nThis opens a pull request against {plan['base']} that adds them to {rules.SHARED_DIR}/.")
    _say("Your working copy is not touched; the branch is built in a separate worktree.")
    if a.dry_run:
        return 0
    if not _confirm(a, "Open the pull request? [y/N] "):
        return 1
    res = team.share(slug, root)
    if a.json:
        print(json.dumps(res))
        return 0
    if res.get("url"):
        _say(f"\nOpened {res['url']}")
    elif res.get("error"):
        _say(f"\nBranch {res.get('branch')}: {res['error']}")
        return 1
    _say("When it merges, everyone who uses `once` in this repo gets these rules.")
    return 0


def cmd_pr_check(a):
    from . import team
    slug, root = _ctx()
    if not root:
        _say("Run this from inside a git repo.")
        return 1
    if not index.load(slug).get("rules"):
        # A fresh machine (CI, say) has no index yet: build it, so the rules committed to the repo count.
        repo.remember_root(slug, root)
        rules.compile_index(slug)
    res = team.pr_check(slug, root, a.pr)
    if a.json:
        print(json.dumps(res))
        return 0
    body = team.pr_comment(res)
    if not res["files"]:
        _say("No changed files found (compared with the default branch).")
        return 0
    if a.post:
        if not res.get("pr"):
            _say("Give the pull request number to post a comment:  " + f"{BIN} pr-check 123 --post")
            return 2
        ok, msg = team.post_comment(root, res["pr"], body)
        _say(f"Posted on #{res['pr']}." if ok else f"Could not post: {msg}")
        return 0 if ok else 1
    _say(body)
    if res.get("pr"):
        _next(f"{BIN} pr-check {res['pr']} --post    add this as a comment on the pull request")
    return 0


EDITOR_DIRS = {"vscode": Path.home() / ".vscode" / "extensions",
               "cursor": Path.home() / ".cursor" / "extensions",
               "windsurf": Path.home() / ".windsurf" / "extensions"}
EXT_NAME = f"once.once-{__version__}"


def _remove_editor(ed) -> int:
    d = EDITOR_DIRS[ed] if isinstance(ed, str) else ed
    n = 0
    if d.is_dir():
        for old in d.glob("once.once-*"):
            shutil.rmtree(old, ignore_errors=True)
            n += 1
    return n


def cmd_editor(a):
    src = Path(__file__).parent / "editor" / "vscode"
    targets = [a.which] if a.which else [e for e, d in EDITOR_DIRS.items() if d.parent.is_dir()]
    if not targets:
        _say("No VS Code, Cursor or Windsurf found in your home folder. Name one:  " + f"{BIN} editor install vscode")
        return 1
    if a.dry_run:
        _say(f"Would {a.action} the extension for: {', '.join(targets)}")
        return 0
    for ed in targets:
        if a.action == "uninstall":
            n = _remove_editor(ed)
            _say(f"{ed}: removed {n} copy" if n == 1 else f"{ed}: removed {n} copies")
            continue
        _remove_editor(ed)
        dest = EDITOR_DIRS[ed] / EXT_NAME
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest)
        _say(f"{ed}: installed the extension. Reload the window, then look for the `once` icon in the activity bar.")
    return 0


def cmd_demo(a):
    from . import demo
    if a.real:
        _say("The demo with your real agent: Claude Code runs 8 times headless on a toy repo, and drafts one rule.")
        _say("  Spend     about $1, at most $0.50 per run")
        _say("  Where     a temporary folder; your repos and your rules are not touched")
        if not _confirm(a):
            return 1
    out = demo.run(real=a.real, keep=a.keep, say=(lambda *_: None) if a.json else _say)
    if a.json:
        print(json.dumps(out))
    if "error" in out:
        return 1
    if not a.json:
        _next(f"{BIN} init    in a repo of your own, then  {BIN} teach \"...\"  or just work")
    return 0


def cmd_collector(a):
    from . import collector
    return collector.cli(a, _say, _next)


def cmd_purge(a):
    """Delete captured sessions: the trajectory files, their session state and the index rows."""
    slug, _ = _ctx()
    con = store.connect()
    store.sync(con)
    cutoff = time.time() - a.older_than * 86400 if a.older_than is not None else None
    q = "SELECT id, repo, started FROM sessions" + ("" if a.everywhere else " WHERE repo=?")
    rows = [r for r in con.execute(q, () if a.everywhere else (slug,))
            if cutoff is None or (r["started"] or 0) < cutoff]
    where = "every repo" if a.everywhere else slug
    age = f" older than {a.older_than} days" if a.older_than is not None else ""
    if not rows:
        _say(f"No captured sessions{age} for {where}.")
        return 0
    _say(f"{len(rows)} captured session{'s' if len(rows) != 1 else ''}{age} for {where} will be deleted.")
    _say("Rules, playbooks and suggestions are kept. Evidence already written into rules is kept.")
    if a.dry_run:
        return 0
    if not _confirm(a, "Delete them? [y/N] "):
        return 1
    n = 0
    for r in rows:
        f = paths.trajectories(r["repo"]) / f"{paths._safe(r['id'])}.jsonl"
        if f.exists():
            f.unlink()
            n += 1
        st = paths.session_state(r["id"])
        if st.exists():
            st.unlink()
        con.execute("DELETE FROM events WHERE session_id=?", (r["id"],))
        con.execute("DELETE FROM sessions WHERE id=?", (r["id"],))
        con.execute("DELETE FROM files WHERE path=?", (str(f),))
    con.commit()
    _say(f"Deleted {n} session file{'s' if n != 1 else ''}.")
    return 0


COMPLETION = {
    "zsh": """#compdef once
_once() {
  local -a words_; words_=("${(@)words[2,CURRENT-1]}")
  local -a opts; opts=("${(@f)$(once _complete -- "${words_[@]}" 2>/dev/null)}")
  compadd -a opts
}
compdef _once once
""",
    "bash": """_once() {
  local cur="${COMP_WORDS[COMP_CWORD]}"
  COMPREPLY=( $(compgen -W "$(once _complete -- "${COMP_WORDS[@]:1:COMP_CWORD-1}" 2>/dev/null)" -- "$cur") )
}
complete -F _once once
""",
    "fish": """complete -c once -f -a '(once _complete -- (commandline -opc)[2..-1] 2>/dev/null)'
""",
}


def cmd_completion(a):
    sh = a.shell or Path(os.environ.get("SHELL", "zsh")).name
    if sh not in COMPLETION:
        _say(f"Shells: zsh, bash, fish.  Example: eval \"$({BIN} completion zsh)\"")
        return 2
    sys.stdout.write(COMPLETION[sh])
    return 0


def cmd_complete(a):
    """Hidden: what can come next, for shell completion. Rule ids and session numbers come from this repo."""
    words = [w for w in a.words if w != "--"]
    p = build_parser()
    subs = next(x for x in p._actions if isinstance(x, argparse._SubParsersAction))
    cmds = [c for c in subs.choices if not c.startswith("_")]
    if not words:
        print("\n".join(cmds))
        return 0
    cmd = words[0]
    slug, _ = _ctx()
    out = []
    if cmd in ("rules", "review"):
        acts = ["show", "drop", "detect", "promote", "lift"] if cmd == "rules" else []
        if len(words) == 1 and acts:
            out = acts
        else:
            out = [r["id"] for r in rules.all_levels(slug) if r.get("status") != "deprecated"]
            if len(words) >= 3 and words[1] == "promote":
                out = ["remind", "check", "block"]
            elif len(words) >= 3 and words[1] == "lift":
                out = ["owner", "stack", "user"]
    elif cmd in ("last", "tag"):
        con = store.connect()
        store.sync(con)
        out = [str(i) for i in range(1, len(store.sessions(con, slug, 20)) + 1)]
        if cmd == "tag" and len(words) == 2:
            out = ["wrong", "avoidable", "missing", "good"]
    elif cmd == "playbooks":
        from . import playbooks
        out = ["new", "show", "sync"] + [pb["id"] for pb in playbooks.load_all(slug)]
    elif cmd == "lab":
        out = ["tasks", "run", "sweep", "replay", "exp", "regress", "cochange", "stats", "bootstrap", "meta", "cost"]
    elif cmd == "config":
        out = ["auto_learn", "daily_cost_cap", "quiet", "snapshots", "holdout_fraction", "teach_by_talking",
               "ask_in_agent", "notifications", "digest", "slack_webhook", "digest_email", "smtp_host", "smtp_port",
               "smtp_user", "smtp_password_cmd", "digest_from"]
    elif cmd in subs.choices:
        sp = subs.choices[cmd]
        out = [o for act in sp._actions for o in act.option_strings if o.startswith("--")]
    print("\n".join(out))
    return 0


# ------------------------------------------------------------------ entry

def build_parser():
    p = argparse.ArgumentParser(prog=BIN, add_help=False)
    p.add_argument("-h", "--help", action="store_true")
    p.add_argument("--version", action="store_true")
    # Accepted before the command too (`once --json status`). Separate names, or the subcommand's default wins.
    p.add_argument("--json", dest="g_json", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("-q", "--quiet", dest="g_quiet", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--dry-run", dest="g_dry_run", action="store_true", help=argparse.SUPPRESS)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("-q", "--quiet", action="store_true", help="print nothing; the exit code says how it went")
    common.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("init", parents=[common], help="connect your agents and this repo")
    s.add_argument("-y", "--yes", action="store_true")
    sub.add_parser("status", parents=[common], help="what is pending in this repo")
    s = sub.add_parser("last", parents=[common], help="the last session as a timeline")
    s.add_argument("n", nargs="?", type=int, default=1, help="how many sessions back (default 1)")
    s.add_argument("--no-input", action="store_true", help="never prompt")
    s.add_argument("--web", action="store_true", help="open the session in the browser viewer")
    s.add_argument("--port", type=int, default=4317, help=argparse.SUPPRESS)
    s = sub.add_parser("tag", parents=[common], help="tag steps of a session")
    s.add_argument("steps", help="a step or a range, e.g. 4 or 4-8")
    s.add_argument("tag", help="wrong | avoidable | missing | good")
    s.add_argument("note", nargs="?", default="", help="what it should have done")
    s.add_argument("--session", type=int, default=1, help="how many sessions back (default 1)")
    s = sub.add_parser("teach", parents=[common], help="add a rule from a plain sentence")
    s.add_argument("sentence", nargs="*")
    s.add_argument("--path", action="append", help="limit to a glob, e.g. 'src/api/**'")
    s.add_argument("--level", default="repo", choices=["repo", "owner", "stack", "agent", "user"],
                   help="where the rule lives: this repo (default), every repo of its owner, of its stack, one agent, or you")
    s.add_argument("--stack", help="with --level stack: which stack (default: this repo's)")
    s.add_argument("--agent", choices=list(adapters.IMPLEMENTED), help="with --level agent: which agent")
    s = sub.add_parser("why", parents=[common], help="what the last session was told, or what applies to a file")
    s.add_argument("target", nargs="?")
    s = sub.add_parser("rules", parents=[common], help="list; show|drop|detect|promote|lift <id>")
    s.add_argument("action", nargs="?", choices=["show", "drop", "detect", "promote", "lift"])
    s.add_argument("id", nargs="?")
    s.add_argument("pattern", nargs="?", help="detect: the regex; promote: remind|check|block; lift: owner|stack|user")
    s.add_argument("--all-levels", action="store_true", help="also list rules inherited from the user, stack, owner and agent levels")
    s.add_argument("--force", action="store_true", help="promote or lift without the evidence check")
    for name in ("off", "on"):
        s = sub.add_parser(name, parents=[common], help=f"turn memory {name}")
        s.add_argument("--everywhere", "--global", dest="everywhere", action="store_true")
        s.add_argument("--session", action="store_true", help="only the session running in this repo now")
        s.add_argument("--capture", action="store_true", help="also stop (or restart) recording sessions")
    sub.add_parser("undo", parents=[common], help="reverse the last teach, drop, review decision or tag")
    s = sub.add_parser("purge", parents=[common], help="delete captured sessions")
    s.add_argument("--everywhere", "--global", dest="everywhere", action="store_true", help="every repo, not only this one")
    s.add_argument("--older-than", type=int, metavar="DAYS", help="only sessions older than this many days")
    s.add_argument("-y", "--yes", action="store_true")
    s = sub.add_parser("completion", parents=[common], help="shell completion: eval \"$(once completion zsh)\"")
    s.add_argument("shell", nargs="?", choices=["zsh", "bash", "fish"])
    s = sub.add_parser("collector", parents=[common], help="the team's central collector: serve, connect, push, team")
    s.add_argument("action", nargs="?", choices=["serve", "connect", "push", "status", "team", "disconnect"])
    s.add_argument("url", nargs="?", help="connect: the collector's address")
    s.add_argument("--token", help="connect: the team token; serve: use this token instead of a generated one")
    s.add_argument("--name", help="connect: how you appear to the team (default: git user.name)")
    s.add_argument("--host", default="127.0.0.1", help="serve: address to listen on")
    s.add_argument("--port", type=int, default=4400)
    s.add_argument("--data", help="serve: where the collector keeps its database (default ~/.once/collector)")
    s.add_argument("--all", action="store_true", help="push: send everything again, not only what changed")
    s.add_argument("--repo", help="team: one repo only")
    s = sub.add_parser("_complete", parents=[common])
    s.add_argument("words", nargs="*")
    s = sub.add_parser("doctor", parents=[common], help="check everything")
    s.add_argument("--fix", action="store_true")
    s.add_argument("--data", action="store_true", help="where your data lives")
    s.add_argument("--clear-errors", action="store_true")
    sub.add_parser("uninstall", parents=[common], help="remove hooks and generated files")
    s = sub.add_parser("playbooks", parents=[common], help="list; new <id> [description]; show <id>; sync")
    s.add_argument("action", nargs="?", choices=["new", "show", "sync"])
    s.add_argument("id", nargs="?")
    s.add_argument("description", nargs="?")
    s = sub.add_parser("ui", parents=[common], help="open the local web viewer")
    s.add_argument("--port", type=int, default=4317)
    s.add_argument("--no-open", action="store_true")
    s.add_argument("--detach", action="store_true", help="run in the background (start it if needed)")
    s = sub.add_parser("lab", parents=[common], help="builder tools: replay")
    s.add_argument("what", nargs="?")
    s.add_argument("rule", nargs="?")
    s.add_argument("arg2", nargs="?")
    s.add_argument("arg3", nargs="?")
    s.add_argument("--drafter-cmd", help="meta: drafting command template with {prompt} and {budget}")
    s.add_argument("--init-prices", action="store_true", help="cost: write a prices.yaml template to fill in")
    s.add_argument("--days", type=int, default=30, help="cost: how far back to look")
    s.add_argument("--prompt")
    s.add_argument("--model", help="model for headless runs, e.g. a cheaper one to control spend")
    s.add_argument("--limit", type=int, default=60, help="how many merged PRs to mine, or tasks to validate")
    s.add_argument("--with-rules", action="store_true", help="lab run: give the agent your active rules")
    s.add_argument("--arms", help="comma-separated arms: bare,rules,generated,native,unreviewed,rules+native")
    s.add_argument("--repeats", type=int, default=3)
    s.add_argument("--split", default=None, choices=["train", "heldout"])
    s.add_argument("--max-cost", type=float, help="stop the whole sweep or experiment at this spend")
    s.add_argument("--resume", action="store_true")
    s.add_argument("--force-mixed", action="store_true", help="continue although the environment changed")
    s.add_argument("--agent", choices=list(adapters.IMPLEMENTED), help="which agent to run headless (default claude-code)")
    s.add_argument("--apply", action="store_true", help="regress: retire harmful and twice-obsolete rules")
    s.add_argument("--write", action="store_true", help="cochange: write draft playbooks")
    s.add_argument("-v", "--verbose", action="store_true")
    s.add_argument("--commit", default="HEAD")
    s.add_argument("--runs", type=int, default=3)
    s.add_argument("--check", help="command that must pass; default comes from repo.yaml")
    s.add_argument("--no-check", action="store_true")
    s.add_argument("--signature", help="regex that identifies the failure in a failed step")
    s.add_argument("--budget", type=float, default=1.0, help="max dollars per run")
    s.add_argument("--timeout", type=int, default=900)
    s.add_argument("--agent-cmd", help="headless command template with {prompt} and {budget}")
    s.add_argument("--keep", action="store_true", help="keep the throwaway copies")
    s.add_argument("-y", "--yes", action="store_true")
    s = sub.add_parser("learn", parents=[common], help="draft suggested rules from new episodes")
    s.add_argument("--max", type=int, default=5)
    s.add_argument("--budget", type=float, default=0.25, help="max dollars per drafted rule")
    s.add_argument("--drafter-cmd", help="drafting command template with {prompt} and {budget}")
    s.add_argument("--replay", action="store_true", help="also replay each new suggestion (6 headless runs each)")
    s.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--agent", choices=list(adapters.IMPLEMENTED))
    s.add_argument("--agent-cmd")
    s.add_argument("--model")
    s.add_argument("-y", "--yes", action="store_true")
    s = sub.add_parser("review", parents=[common], help="approve, edit or reject suggested rules")
    s.add_argument("--list", action="store_true")
    s.add_argument("--accept")
    s.add_argument("--reject")
    s.add_argument("--text", help="with --accept: your wording")
    s.add_argument("--reason", help="with --reject: why")
    s.add_argument("--agent", choices=list(adapters.IMPLEMENTED))
    s.add_argument("--agent-cmd")
    s.add_argument("--model")
    s.add_argument("--budget", type=float, default=0.5)
    s = sub.add_parser("import", parents=[common], help="import sessions Claude Code already recorded")
    s.add_argument("--limit", type=int, default=200)
    s.add_argument("--all", action="store_true", help="every repo, not only this one")
    s = sub.add_parser("config", parents=[common], help="show or change settings")
    s.add_argument("key", nargs="?")
    s.add_argument("value", nargs="?")
    s = sub.add_parser("export", parents=[common], help="write active rules as a plain file")
    s.add_argument("--format", choices=["agents-md", "json"], default="agents-md")
    s.add_argument("-o", "--output")
    s.add_argument("--max", type=int, default=40)
    s = sub.add_parser("_capture", parents=[common])
    s.add_argument("--session")
    s.add_argument("--agent")
    s.add_argument("--text")
    sub.add_parser("statusline", parents=[common], help="the status line text (Claude Code runs this)")
    s = sub.add_parser("menubar", parents=[common], help="menu bar icon via SwiftBar or xbar")
    s.add_argument("--install", action="store_true")
    s.add_argument("--uninstall", action="store_true")
    s = sub.add_parser("digest", parents=[common], help="what your agents did this week")
    s.add_argument("--days", type=int, default=7)
    s.add_argument("--send", action="store_true", help="write it to a file, notify, and post to Slack if set")
    s = sub.add_parser("share", parents=[common], help="open a pull request that shares your rules with the team")
    s.add_argument("-y", "--yes", action="store_true")
    s = sub.add_parser("pr-check", parents=[common], help="files a pull request probably missed, from history")
    s.add_argument("pr", nargs="?")
    s.add_argument("--post", action="store_true", help="post the result as a comment")
    s = sub.add_parser("editor", parents=[common], help="install the VS Code / Cursor sidebar")
    s.add_argument("action", choices=["install", "uninstall"])
    s.add_argument("which", nargs="?", choices=["vscode", "cursor", "windsurf"])
    s = sub.add_parser("demo", parents=[common], help="the whole loop on a bundled toy repo, in a minute")
    s.add_argument("--real", action="store_true", help="use Claude Code and a real drafter (spends about $1)")
    s.add_argument("--keep", action="store_true", help="keep the temporary folder")
    s.add_argument("-y", "--yes", action="store_true")
    return p


JSON_EXEMPT = {"completion", "_complete", "menubar", "statusline", "ui", "_capture"}


def main(argv=None) -> int:
    p = build_parser()
    a, extra = p.parse_known_args(argv)
    if a.version:
        print(f"{BIN} {__version__}")
        return 0
    if a.help or extra and a.cmd is None:
        print(HELP)
        return 0
    if extra:
        # An unknown flag used to be dropped silently, so `once off --session` paused the whole repo.
        print(f"{BIN} {a.cmd}: unknown argument{'s' if len(extra) > 1 else ''}: {' '.join(extra)}\n"
              f"Run: {BIN} {a.cmd} --help", file=sys.stderr)
        return 2
    for k in ("json", "quiet", "dry_run"):
        setattr(a, k, bool(getattr(a, k, False) or getattr(a, f"g_{k}", False)))
    table = {None: cmd_status, "status": cmd_status, "init": cmd_init, "last": cmd_last, "tag": cmd_tag,
             "teach": cmd_teach, "why": cmd_why, "rules": cmd_rules, "off": cmd_off,
             "on": lambda x: cmd_off(x, on=True), "undo": cmd_undo, "doctor": cmd_doctor,
             "uninstall": cmd_uninstall, "review": cmd_review, "learn": cmd_learn, "ui": cmd_ui, "lab": cmd_lab,
             "playbooks": cmd_playbooks, "demo": cmd_demo, "import": cmd_import, "config": cmd_config,
             "export": cmd_export, "_capture": cmd_capture, "statusline": cmd_statusline, "menubar": cmd_menubar,
             "digest": cmd_digest, "share": cmd_share, "pr-check": cmd_pr_check, "editor": cmd_editor,
             "purge": cmd_purge, "completion": cmd_completion, "_complete": cmd_complete, "collector": cmd_collector}
    real_out = sys.stdout
    if a.quiet and not a.json:
        sys.stdout = open(os.devnull, "w")
    # --json is a promise for every command: what a command prints as prose is wrapped as {"ok", "exit", "message"}.
    # Not for output other tools read as text, nor for servers that never return.
    wrap = a.json and a.cmd not in JSON_EXEMPT and not (a.cmd == "collector" and getattr(a, "action", None) == "serve")
    if wrap:
        import io
        sys.stdout = io.StringIO()
    try:
        rc = table[a.cmd](a) or 0
        if wrap:
            text, sys.stdout = sys.stdout.getvalue(), real_out
            try:
                json.loads(text)
                sys.stdout.write(text)
            except ValueError:
                print(json.dumps({"ok": rc == 0, "exit": rc, "message": text.strip()}))
        sys.stdout.flush()
        return rc
    except KeyboardInterrupt:
        return 130
    except PermissionError as ex:
        sys.stdout = real_out
        print(f"{BIN}: could not write {getattr(ex, 'filename', None) or 'a file'}: permission denied.\n"
              f"Fix: make that folder writable again (for example  chmod -R u+w {paths.home()}), then retry.",
              file=sys.stderr)
        return 1
    except Exception as ex:  # one clear line instead of a traceback; the details go to a log
        sys.stdout = real_out
        if os.environ.get("ONCE_DEBUG"):
            raise
        import traceback
        try:
            paths.local().mkdir(parents=True, exist_ok=True)
            with open(paths.local() / "cli-errors.log", "a") as f:
                f.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} once {' '.join(argv or sys.argv[1:])}\n{traceback.format_exc()}")
        except Exception:
            pass
        print(f"{BIN} {a.cmd or ''}: {type(ex).__name__}: {ex}\nThe details are in {paths.local() / 'cli-errors.log'}. "
              f"Run  {BIN} doctor  to check the install; ONCE_DEBUG=1 shows the full error.", file=sys.stderr)
        return 1
    except BrokenPipeError:  # output piped into head, less, etc.
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0
    finally:
        if sys.stdout is not real_out:  # --quiet's devnull, or --json's buffer after an error
            try:
                sys.stdout.close()
            except Exception:
                pass
            sys.stdout = real_out


if __name__ == "__main__":
    sys.exit(main())
