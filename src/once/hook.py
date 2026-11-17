"""The hook entry point: `python -m once.hook <agent>` with the native payload on stdin.

Rules this file keeps:
  - fail open: any error is logged and the process exits 0 with no output
  - light: no yaml, no sqlite; a precompiled JSON index and one appended line
  - it never writes to tracked files in the user's repo
"""
import json
import os
import sys
import time


def _append(slug: str, session_id: str, ev: dict):
    """The one place session events reach disk, so every writer (hook, `once tag`, the MCP server) is masked."""
    from . import paths, redact
    ev, _ = redact.redact(ev)
    d = paths.trajectories(slug)
    d.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(ev, separators=(",", ":")) + "\n").encode()
    fd = os.open(d / f"{paths._safe(session_id)}.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)  # one write on an O_APPEND fd: concurrent hooks do not interleave
    finally:
        os.close(fd)


def _load_state(session_id: str) -> dict | None:
    from . import paths
    try:
        return json.loads(paths.session_state(session_id).read_text())
    except Exception:
        return None


def _save_state(state: dict):
    from . import paths
    p = paths.session_state(state["session_id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, p)


def _config() -> dict:
    from . import paths
    try:
        return json.loads((paths.home() / "config.json").read_text())
    except Exception:
        return {}


def _held_out(session_id: str, fraction: float) -> bool:
    if fraction <= 0:
        return False
    import hashlib
    return int(hashlib.sha1(session_id.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF < fraction


def _new_state(raw: dict, agent: str) -> dict:
    from . import paths, repo
    cwd = raw.get("cwd") or os.getcwd()
    slug, root = repo.slug(cwd)
    if os.environ.get("ONCE_SLUG"):  # replay and experiments route runs away from real sessions
        slug, root = os.environ["ONCE_SLUG"], (root or __import__("pathlib").Path(cwd))
    cfg = _config()
    if root and not os.environ.get("ONCE_SLUG"):
        repo.remember_root(slug, root)
    return {"session_id": raw.get("session_id") or f"unknown-{int(time.time())}", "agent": agent,
            "slug": slug, "root": str(root) if root else None, "cwd": cwd,
            "commit": repo.head(root) if root else "", "injected": [],
            "held_out": _held_out(raw.get("session_id") or "", float(cfg.get("holdout_fraction", 0))),
            "paused": paths.off_flag().exists() or paths.off_flag(slug).exists(),
            "started": time.time()}


def _index(slug: str, agent: str | None = None) -> dict:
    """The rule index for this session, only the rules meant for this agent.
    Replay overrides it to give exactly one rule set, or none."""
    from . import index
    override = os.environ.get("ONCE_INDEX")
    if override == "none":
        return {"rules": []}
    if override:
        try:
            return index.for_agent(json.loads(open(override).read()), agent)
        except Exception:
            return {"rules": []}
    return index.for_agent(index.load(slug), agent)


def _applies(r: dict, paths_: list[str]) -> bool:
    import re as _re
    if r.get("repo_wide"):
        return True
    return any(_re.match(rx, p) for p in paths_ for rx in r.get("regex", []))


def _violations(idx: dict, text: str, paths_: list[str], levels: tuple) -> list[dict]:
    """Rules at these enforcement levels whose detector matches what the tool call writes or runs."""
    import re as _re
    hits = []
    if not text:
        return hits
    for r in idx.get("rules", []):
        if r.get("level", "instruct") in levels and r.get("detector") and _applies(r, paths_):
            try:
                if _re.search(r["detector"], text):
                    hits.append(r)
            except _re.error:
                pass
    return hits


def _enforce_post(raw: dict, agent: str) -> str:
    """The synchronous post-edit hook: remind- and check-level rules answer right after the edit lands.
    Capture is done by the async hook for the same event, so this one records only what it enforced."""
    from . import adapters, redact
    adapter = adapters.get(agent)
    if not adapter.CAPABILITIES.get("enforce"):
        return ""
    state = _load_state(raw.get("session_id") or "")
    if not state or state.get("paused") or state.get("held_out"):
        return ""
    check, remind, evs = _enforce_hits(adapter, state, raw, agent)
    for ev in evs:
        ev, _ = redact.redact(ev)
        ev.update({"session_id": state["session_id"], "agent": agent, "repo": state["slug"], "ts": time.time()})
        _append(state["slug"], state["session_id"], ev)
    return adapter.enforce_reply(check, remind) if (check or remind) else ""


def _say_rules(rs: list[dict]) -> str:
    return "\n".join(f"[{r['id']}] {r['text']}" + (f" Why: {r['why']}" if r.get("why") else "") for r in rs)


def _enforce_hits(adapter, state: dict, raw: dict, agent: str) -> tuple[str, str, list[dict]]:
    """remind- and check-level rules this tool call broke: (check text, remind text, events to record)."""
    idx = _index(state["slug"], agent)
    rel = adapter.pre_tool_paths(raw, state.get("root"))
    hits = _violations(idx, adapter.edit_text(raw), rel, ("remind", "check"))
    check = [r for r in hits if r["level"] == "check"]
    remind = [r for r in hits if r["level"] == "remind"]
    evs = [{"event": "rule_enforced", "rule_id": r["id"], "level": r["level"], "path": rel[0] if rel else ""} for r in hits]
    return (("This edit goes against a convention of this repository. Fix it before you continue:\n" + _say_rules(check))
            if check else "",
            ("Reminder, this edit looks like it goes against:\n" + _say_rules(remind)) if remind else "", evs)


def _usage(transcript_path) -> dict | None:
    """Token totals from the agent's own transcript. Best effort: the format is not a documented contract."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    tot, calls, models = dict.fromkeys(keys, 0), 0, set()
    try:
        with open(transcript_path, errors="ignore") as f:
            for line in f:
                if '"usage"' not in line:
                    continue
                m = (json.loads(line).get("message") or {})
                u = m.get("usage") if isinstance(m, dict) else None
                if isinstance(u, dict):
                    calls += 1
                    if m.get("model"):
                        models.add(m["model"])
                    for k in keys:
                        tot[k] += int(u.get(k) or 0)
    except Exception:
        return None
    return {**tot, "model_calls": calls, "models": sorted(models)} if calls else None


def _end_line(state: dict) -> str:
    """One line when a session ends with something worth a look; nothing otherwise."""
    if _config().get("quiet") or os.environ.get("ONCE_SLUG"):
        return ""
    try:
        from . import detectors, index, paths
        f = paths.trajectories(state["slug"]) / f"{paths._safe(state['session_id'])}.jsonl"
        evs = [{**json.loads(l), "seq": i} for i, l in enumerate(f.read_text().splitlines()) if l.strip()]
        flags = detectors.detect(evs)
        noted = sum(1 for e in evs if e.get("event") == "correction_noted")
        pending = len(index.load(state["slug"]).get("pending") or [])
        parts = []
        if flags:
            kinds = sorted({fl["detector"].replace("_", " ") for fl in flags})
            parts.append(f"{len(flags)} avoidable pattern{'s' if len(flags) != 1 else ''} ({', '.join(kinds)})")
        if noted:
            parts.append(f"noted {noted} correction{'s' if noted != 1 else ''} from this chat")
        if not parts:
            return ""
        tail = (f"{pending} suggested rule{'s' if pending != 1 else ''} waiting; your agent will ask you next session."
                if pending else "Details:  once last")
        return json.dumps({"systemMessage": f"once: {'; '.join(parts)}. {tail}"})
    except Exception:
        return ""


def _spawn(args: list, cwd=None):
    """Run a `once` command detached, so the hook returns immediately."""
    try:
        import subprocess as _sp
        _sp.Popen([sys.executable, "-m", "once.cli", *args], cwd=cwd or None, stdin=_sp.DEVNULL,
                  stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, start_new_session=True)
    except Exception:
        pass


def _review(state: dict, idx: dict, events: list) -> str:
    """Ask the agent to put pending suggestions to the developer, once per session."""
    if state.get("review_asked") or state["paused"] or os.environ.get("ONCE_SLUG"):
        return ""
    cfg = _config()
    if cfg.get("quiet") or cfg.get("ask_in_agent", True) in (False, "off"):
        return ""
    from . import ambient
    text = ambient.review_prompt(idx, state["session_id"])
    if text:
        state["review_asked"] = True
        events.append({"event": "review_asked", "rule_ids": [p["id"] for p in idx.get("pending") or []][:2]})
    return text


def handle(raw: dict, agent: str, event: str | None = None) -> str:
    """Process one hook payload. Returns the text to print (may be empty)."""
    from . import SCHEMA, adapters, index, redact
    adapter = adapters.get(agent)
    if event == "enforce":
        return _enforce_post(raw, agent)
    raw = adapter.normalize(raw, event)
    # An agent update can send odd types; a session id that is not a short string must never reach a file name
    # or the index, where one bad line used to break every later command.
    for k in ("session_id", "hook_event_name", "cwd"):
        v = raw.get(k)
        if v is not None and not isinstance(v, str):
            raw[k] = "" if isinstance(v, (dict, list)) else str(v)
    raw["session_id"] = (raw.get("session_id") or "")[:200]
    name = raw.get("hook_event_name", "")
    sid = raw.get("session_id") or ""
    state = None if name == "SessionStart" else _load_state(sid)
    fresh = state is None
    if fresh:
        state = _new_state(raw, agent)
    from . import paths as _p
    if _p.capture_off_flag().exists() or _p.capture_off_flag(state["slug"]).exists():
        return adapter.fallback(name)  # `once off --capture`: nothing is recorded or given
    out = ""
    base = {"schema": SCHEMA, "session_id": state["session_id"], "agent": agent, "repo": state["slug"]}
    events = adapter.parse(raw, state.get("root"))
    idx = None

    if adapter.CAPABILITIES.get("reinject_each_turn"):
        events = [ev for ev in events if not (ev["event"] == "user_prompt" and ev.get("prompt") == state.get("last_prompt"))]
        for ev in events:
            if ev["event"] == "user_prompt":
                state["last_prompt"] = ev["prompt"]

    if fresh and name != "SessionStart":
        # The session began before the hooks were installed: open it lazily.
        events.insert(0, {"event": "session_start", "source": "late", "cwd": state["cwd"]})

    for ev in events:
        if ev["event"] == "session_start":
            ev.update({"commit": state["commit"], "held_out": state["held_out"], "paused": state["paused"]})
            try:
                ev["agent_version"] = json.loads((__import__("once.paths", fromlist=["x"]).local()
                                                  / "agent_versions.json").read_text()).get(agent)
            except Exception:
                pass
            if os.environ.get("ONCE_LABEL"):  # a lab run: which task and arm this session belongs to
                try:
                    ev["label"] = json.loads(os.environ["ONCE_LABEL"])
                except Exception:
                    pass
            if name == "SessionStart":
                idx = _index(state["slug"], agent)
                wide = index.repo_wide(idx)
                text = ""
                if wide and not state["paused"] and adapter.INJECT_EVENT == "SessionStart":
                    if state["held_out"]:
                        events.append({"event": "rule_withheld", "rule_ids": [r["id"] for r in wide],
                                       "reason": "held_out"})
                    else:
                        import random as _random
                        pol = idx.get("policy") or {}
                        ranked, explored = index.rank(wide, float(pol.get("explore", 0)), _random.Random())
                        chosen, held = index.within_budget(ranked)
                        text = index.render(chosen)
                        state["injected"] += [r["id"] for r in chosen]
                        ev_inj = {"event": "rule_injected", "rule_ids": [r["id"] for r in chosen],
                                  "channel": "session_start", "chars": len(text)}
                        if pol.get("version"):
                            ev_inj["policy"] = pol["version"]
                        if explored:
                            ev_inj["explored"] = explored
                        events.append(ev_inj)
                        if held:
                            events.append({"event": "rule_withheld", "rule_ids": [r["id"] for r in held],
                                           "reason": "budget"})
                if adapter.INJECT_EVENT == "SessionStart":
                    text = "\n\n".join(t for t in (text, _review(state, idx, events)) if t)
                if text:
                    out = adapter.reply("SessionStart", text)
                if not os.environ.get("ONCE_SLUG"):
                    try:
                        from . import ambient
                        if ambient.digest_due():
                            _spawn(["digest", "--send", "--quiet"], state.get("root"))
                    except Exception:
                        pass
        elif ev["event"] == "tool_result" and ev.get("paths") and not state["paused"] and not state["held_out"] \
                and (ev.get("tool") in ("read", "edit") and adapter.CAPABILITIES.get("native_path_rules")
                     or adapter.CAPABILITIES.get("reinject_each_turn")):
            # Claude Code loads native rule files itself, so the injection is inferred from the file touch.
            # Agents without them get the rule on their next turn.
            idx = idx or _index(state["slug"], agent)
            new = [r for p in ev["paths"] for r in index.matching(idx, p) if r["id"] not in state["injected"]]
            if new:
                ids = list(dict.fromkeys(r["id"] for r in new))
                state["injected"] += ids
                native = bool(adapter.CAPABILITIES.get("native_path_rules"))
                events.append({"event": "rule_injected", "rule_ids": ids, "channel": "native" if native else "next_turn",
                               "inferred": native, "matched": ev["paths"][0]})

    if not state["paused"] and not os.environ.get("ONCE_SLUG") \
            and _config().get("teach_by_talking", True) not in (False, "off"):
        for ev in list(events):
            if ev["event"] == "user_prompt" and ev.get("prompt"):
                from . import ambient
                if ambient.is_correction(ev["prompt"]):
                    from . import redact
                    said = redact.redact_text(ev["prompt"][:500])[0]  # masked before it becomes a rule or a process argument
                    events.append({"event": "correction_noted", "text": said})
                    _spawn(["_capture", "--session", state["session_id"], "--agent", agent, "--text", said],
                           state.get("root") or state.get("cwd"))

    if adapter.CAPABILITIES.get("reinject_each_turn") and name == adapter.INJECT_EVENT \
            and not state["paused"] and not state["held_out"]:
        idx = idx or _index(state["slug"], agent)
        by_id = {r["id"]: r for r in idx.get("rules", [])}
        wide = [r for r in index.repo_wide(idx)]
        new_wide = [r["id"] for r in wide if r["id"] not in state["injected"]]
        native = adapter.CAPABILITIES.get("native_path_rules")  # rule files the agent loads itself: not resent
        current = wide + ([] if native else [by_id[i] for i in state["injected"] if i in by_id and not by_id[i]["repo_wide"]])
        chosen, held = index.within_budget(current)
        held_back = "\n\n".join(state.pop("pending_enforce", None) or [])  # findings a PostToolUse could not deliver
        if held_back:
            events.append({"event": "enforcement_delivered", "channel": "next_turn"})
        text = "\n\n".join(t for t in (held_back, index.render(chosen) if chosen else "", _review(state, idx, events)) if t)
        if text:
            out = adapter.reply(name, text)
        if new_wide:
            state["injected"] += new_wide
            events.append({"event": "rule_injected", "rule_ids": new_wide, "channel": "each_turn"})

    if name == "PreToolUse" and not state["paused"] and not state["held_out"] \
            and adapter.CAPABILITIES.get("enforce"):
        # Block-level rules: refuse the call before it lands. Context alone could be ignored.
        idx = idx or _index(state["slug"], agent)
        touched_ = adapter.pre_tool_paths(raw, state.get("root"))
        blocked = _violations(idx, adapter.edit_text(raw), touched_, ("block",))
        if blocked:
            for r in blocked:
                events.append({"event": "rule_enforced", "rule_id": r["id"], "level": "block",
                               "path": touched_[0] if touched_ else ""})
            reason = ("Refused by a convention of this repository:\n"
                      + "\n".join(f"[{r['id']}] {r['text']}" for r in blocked) + "\nDo it the way the convention says.")
            out = adapter.deny(reason)

    if name == "PreToolUse" and not state["paused"] and not state["held_out"] and not out \
            and adapter.CAPABILITIES.get("inject_on_path"):
        # A shell command is about to touch files: hand over their path-scoped rules first.
        touched = adapter.pre_tool_paths(raw, state.get("root"))
        if touched:
            idx = idx or _index(state["slug"], agent)
            new, seen = [], set(state["injected"])
            for p in touched:
                for r in index.matching(idx, p):
                    if r["id"] not in seen:
                        seen.add(r["id"])
                        new.append(r)
            chosen, held = index.within_budget(new, already=len(state["injected"]))
            if chosen:
                out = adapter.reply("PreToolUse", index.render(chosen))
                state["injected"] += [r["id"] for r in chosen]
                events.append({"event": "rule_injected", "rule_ids": [r["id"] for r in chosen],
                               "channel": "pre_tool", "matched": touched[0]})

    if name == "PostToolUse" and adapter.CAPABILITIES.get("enforce_inline") and not state["paused"] \
            and not state["held_out"]:
        check, remind, evs_ = _enforce_hits(adapter, state, raw, agent)
        events += evs_
        if check or remind:
            if adapter.CAPABILITIES.get("enforce_deferred"):  # this agent's PostToolUse cannot speak to the model
                state.setdefault("pending_enforce", []).extend(t for t in (check, remind) if t)
            else:
                out = adapter.enforce_reply(check, remind)

    if name in ("PostToolUse",) and state["injected"] and not state["paused"]:
        # Ignored-rule check: does this edit contain what a rule the agent was given says to avoid?
        ti = raw.get("tool_input") if isinstance(raw.get("tool_input"), dict) else {}
        text = ti.get("new_string") or ti.get("content") or ti.get("CodeContent") or ti.get("ReplacementContent") or ""
        if isinstance(text, str) and text:
            import re as _re
            idx = idx or _index(state["slug"], agent)
            for r in idx.get("rules", []):
                if r.get("detector") and r["id"] in state["injected"]:
                    try:
                        hit = _re.search(r["detector"], text)
                    except _re.error:
                        hit = None
                    if hit:
                        events.append({"event": "rule_violation", "rule_id": r["id"],
                                       "path": next((p for ev in events for p in ev.get("paths", [])), "")})

    if state.get("root") and not os.environ.get("ONCE_SLUG") and _config().get("snapshots", True):
        from . import snapshot
        if name == "SessionStart":
            snapshot.spawn(state["root"], state["session_id"], "start")
        elif name in ("SessionEnd", "Stop"):
            snapshot.spawn(state["root"], state["session_id"], "end")

    if name == "SessionEnd" and not os.environ.get("ONCE_SLUG"):
        try:
            from . import collector
            if collector.due():  # the team's collector, when connected: summaries only, in the background
                _spawn(["collector", "push", "--quiet"], state.get("root"))
        except Exception:
            pass

    if name == "SessionEnd" and _config().get("auto_learn") and not os.environ.get("ONCE_SLUG"):
        _spawn(["learn", "--yes", "--auto"], state.get("root"))  # opt-in, under the daily cap

    if name == "SessionEnd":
        for ev in events:
            if ev["event"] == "session_end":
                usage = _usage(raw.get("transcript_path"))
                if usage:
                    ev["usage"] = usage
        out = _end_line(state) or out

    now = time.time()
    for ev in events:
        clean, n = redact.redact(ev)
        clean.update(base)
        clean["ts"] = now
        if n:
            clean["redactions"] = n
        _append(state["slug"], state["session_id"], clean)
    if fresh or events:
        _save_state(state)
    return out or adapter.fallback(name)


def _fallback(agent: str, event: str | None) -> str:
    """The harmless reply an agent expects when this hook has nothing to say or has failed."""
    try:
        from . import adapters
        return adapters.get(agent).fallback(event or "")
    except Exception:
        return ""


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if os.environ.get("ONCE_DISABLE") == "1":
            sys.stdout.write(_fallback(argv[0] if argv else "claude-code", argv[1] if len(argv) > 1 else None))
            return 0
        agent = argv[0] if argv else "claude-code"
        event = argv[1] if len(argv) > 1 else None
        data = sys.stdin.read()
        raw = json.loads(data) if data.strip() else {}
        if not isinstance(raw, dict):
            raw = {}
        if os.environ.get("ONCE_DEBUG_PAYLOADS"):
            # Raw payloads, secrets masked, to check an agent's real field and tool names after an update.
            try:
                from . import redact
                clean, _ = redact.redact(raw)
                with open(os.environ["ONCE_DEBUG_PAYLOADS"], "a") as f:
                    f.write(json.dumps({"agent": agent, "event": event, "raw": clean}) + "\n")
            except Exception:
                pass
        out = handle(raw, agent, event) if raw else _fallback(agent, event)
        if out:
            sys.stdout.write(out)
    except Exception as e:  # fail open, always
        try:
            sys.stdout.write(_fallback(argv[0] if argv else "claude-code", argv[1] if len(argv) > 1 else None))
        except Exception:
            pass
        try:
            from . import paths
            paths.local().mkdir(parents=True, exist_ok=True)
            with open(paths.error_log(), "a") as f:
                f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {type(e).__name__}: {e}\n")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
