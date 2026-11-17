"""Auto-flags: the first pass over a session, so nobody has to read 200 steps."""


def steps(events: list[dict]) -> list[dict]:
    """The rows a person sees: prompts and tool results, numbered from 1."""
    out = []
    for e in events:
        if e.get("event") in ("user_prompt", "tool_result"):
            out.append({**e, "step": len(out) + 1})
    return out


def _cfg() -> dict:
    try:
        from . import pipeline
        return pipeline.detector_cfg()
    except Exception:
        return {}


def detect(events: list[dict], cfg: dict | None = None) -> list[dict]:
    """Thresholds come from the active pipeline version, which the outer loop tunes from what you accept."""
    cfg = _cfg() if cfg is None else cfg
    th = lambda det, key, default: (cfg.get(det) or {}).get(key, default)
    st = steps(events)
    flags = []

    # Retry cluster: the same error digest repeats before a success.
    i = 0
    used = set()
    while i < len(st):
        s = st[i]
        if s.get("event") == "tool_result" and s.get("ok") is False and s.get("error_digest"):
            dig, last, count = s["error_digest"], i, 1
            j = i + 1
            while j < len(st):
                t = st[j]
                if t.get("event") == "user_prompt":
                    break
                if t.get("tool") == s.get("tool") and t.get("ok") is False and t.get("error_digest") == dig:
                    last, count = j, count + 1
                elif t.get("tool") == s.get("tool") and t.get("ok") is True and t.get("target") == s.get("target"):
                    break
                j += 1
            if count >= th("retry_cluster", "min_repeats", 2):
                flags.append({"detector": "retry_cluster", "step_from": st[i]["step"], "step_to": st[last]["step"],
                              "seq_from": st[i]["seq"], "seq_to": st[last]["seq"],
                              "note": f"{_short(s.get('target'))} failed {count} times, same error"
                                      + (f" ({s['error_head']})" if s.get("error_head") else "")})
                used.update(range(i, last + 1))
                i = last + 1
                continue
        i += 1

    # Failed command: a single failure outside any cluster.
    for k, s in enumerate(st):
        if k in used or not th("failed_command", "enabled", True):
            continue
        if s.get("event") == "tool_result" and s.get("tool") == "shell" and s.get("ok") is False:
            flags.append({"detector": "failed_command", "step_from": s["step"], "step_to": s["step"],
                          "seq_from": s["seq"], "seq_to": s["seq"],
                          "note": f"{_short(s.get('target'))} failed"})

    # Repeated read: the same file read 3+ times with no edit to it in between.
    reads: dict[str, list[int]] = {}
    for k, s in enumerate(st):
        if s.get("event") != "tool_result":
            continue
        tgt = s.get("target") or ""
        if s.get("tool") == "edit":
            reads.pop(tgt, None)
        elif s.get("tool") == "read" and tgt:
            reads.setdefault(tgt, []).append(k)
            if len(reads[tgt]) == th("repeated_read", "min_reads", 3):
                a, b = reads[tgt][0], k
                flags.append({"detector": "repeated_read", "step_from": st[a]["step"], "step_to": st[b]["step"],
                              "seq_from": st[a]["seq"], "seq_to": st[b]["seq"],
                              "note": f"{_short(tgt)} read {len(reads[tgt])} times with no edit between"})

    # Reverted edit: a later edit to the same file puts back exactly what an earlier edit removed.
    edits: dict[str, list[tuple[int, dict]]] = {}
    for k, s in enumerate(st):
        sig = s.get("edit_sig") or {}
        if s.get("tool") != "edit" or not s.get("target"):
            continue
        for j, prev in edits.get(s["target"], []):
            if sig.get("new") and sig["new"] == prev.get("old") and prev.get("new") == sig.get("old"):
                flags.append({"detector": "reverted_edit", "step_from": st[j]["step"], "step_to": s["step"],
                              "seq_from": st[j]["seq"], "seq_to": s["seq"],
                              "note": f"an edit to {_short(s['target'])} was undone"})
                break
        edits.setdefault(s["target"], []).append((k, sig))

    # Wandering search: many searches and reads before the first edit of a prompt turn.
    run_start, count = None, 0
    for k, s in enumerate(st):
        if s.get("event") == "user_prompt":
            run_start, count = k + 1, 0
        elif run_start is not None and s.get("tool") in ("search", "read"):
            count += 1
        elif run_start is not None and s.get("tool") == "edit":
            if count >= th("wandering_search", "min_steps", 8):
                flags.append({"detector": "wandering_search", "step_from": st[run_start]["step"], "step_to": st[k - 1]["step"],
                              "seq_from": st[run_start]["seq"], "seq_to": st[k - 1]["seq"],
                              "note": f"{count} searches and reads before the first edit"})
            run_start = None

    # Ignored rule: an edit matched the detector of a rule the agent had already been given.
    for e in events:
        if e.get("event") == "rule_violation":
            prev = [s for s in st if s["seq"] < e.get("seq", 0)]
            if prev:
                s = prev[-1]
                flags.append({"detector": "ignored_rule", "step_from": s["step"], "step_to": s["step"],
                              "seq_from": s["seq"], "seq_to": s["seq"], "rule_id": e.get("rule_id"),
                              "note": f"went against rule {e.get('rule_id')} in {_short(e.get('path'))}"})

    flags.sort(key=lambda f: f["step_from"])
    return flags


def annotations(events: list[dict]) -> list[dict]:
    """Human tags, with a step range. Feedback given inside the agent covers the steps since the last prompt."""
    st = steps(events)
    out = []
    gone = {e.get("annotation_id") for e in events if e.get("event") == "annotation_retracted"}
    for e in events:
        if e.get("event") != "annotation" or (e.get("id") and e["id"] in gone):
            continue
        a = dict(e)
        if "step_from" not in a:
            before = [s for s in st if s["seq"] < e.get("seq", 0)]
            if not before:
                continue
            start = next((s for s in reversed(before) if s["event"] == "user_prompt"), before[0])
            first = next((s for s in before if s["step"] > start["step"]), before[-1])
            a.update({"step_from": first["step"], "step_to": before[-1]["step"],
                      "seq_from": first["seq"], "seq_to": before[-1]["seq"]})
        out.append(a)
    return out


def avoidable_ratio(events: list[dict], flags: list[dict]) -> float:
    st = steps(events)
    if not st:
        return 0.0
    inside = set()
    for f in flags:
        inside.update(range(f["step_from"], f["step_to"] + 1))
    for e in annotations(events):
        if e.get("tag") in ("wrong", "avoidable", "missing_knowledge"):
            inside.update(range(e["step_from"], e["step_to"] + 1))
    return len(inside) / len(st)


def _short(t, n=48):
    t = (t or "").strip().replace("\n", " ")
    return t if len(t) <= n else t[: n - 1] + "…"
