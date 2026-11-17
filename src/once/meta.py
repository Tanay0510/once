"""The outer loop: the pipeline learns how to learn from what became of the rules it produced.

Five things improve, each from its own signal, and each change is a new pipeline version that a person promotes:
  1. detectors       which flags led to accepted rules -> thresholds and weights
  2. the drafter     accepted, edited and rejected drafts, and replay results -> instructions and examples
  3. ranking         session outcomes when rules compete for the budget -> a policy pooled over rule features
  4. rule-form priors which kinds of rule survive regression and get followed -> preferences for drafter and lint
  5. transfer        all of the above, carried to a repo the pipeline has not seen (`compare` and `report`)

Guardrails: a version holds only what pipeline.ALLOWED lists, so lint, redaction, the review gate and the
enforcement promotion test are out of reach. Nothing tuned here is used before `once lab meta promote`, and
`once lab meta rollback` undoes a promotion. Replay results weigh more than acceptance, so the drafter does
not learn to flatter. Drafter tuning checks on a repo it was not tuned on when there is one.
"""
import copy
import difflib
import json
import random
import time
from pathlib import Path

from . import detectors, index, learn, paths, pipeline, rules, store

# ---------------------------------------------------------------- 1. detectors

THRESHOLD = {"retry_cluster": "min_repeats", "repeated_read": "min_reads", "wandering_search": "min_steps"}


def _decisions() -> dict:
    """episode key -> the decision made on the candidate drafted from it."""
    out = {}
    for r in learn.history():
        if r.get("decision") and r.get("episode"):
            out[r["episode"]] = r["decision"]
    return out


def detector_stats(cfg: dict | None = None) -> dict:
    """Per detector, over every known repo: flags, confirmed by a tag, dismissed as good, and rules accepted."""
    cfg = (cfg or pipeline.active())["detectors"]
    dec = _decisions()
    by = {}
    con = store.connect()
    store.sync(con)
    for slug in rules.known_slugs() or [None]:
        for row in store.sessions(con, slug, 5000):
            if (row["repo"] or "").startswith("replay/"):
                continue
            evs = store.events(con, row["id"])
            notes = detectors.annotations(evs)
            for f in detectors.detect(evs, cfg):
                b = by.setdefault(f["detector"], {"flags": 0, "confirmed": 0, "dismissed": 0, "accepted": 0, "repos": set()})
                b["flags"] += 1
                b["repos"].add(row["repo"])
                over = [n for n in notes if n["step_from"] <= f["step_to"] and n["step_to"] >= f["step_from"]]
                if any(n.get("tag") == "good" for n in over):
                    b["dismissed"] += 1
                elif over:
                    b["confirmed"] += 1
                keys = [learn._key("retry", row["id"], f["seq_from"])] if f["detector"] == "retry_cluster" else []
                keys += [learn._key("tag", row["id"], n["seq_from"], n.get("note")) for n in over if n.get("tag") != "good"]
                if any(str(dec.get(k, "")).startswith("accepted") for k in keys):
                    b["accepted"] += 1
    con.close()
    for b in by.values():
        b["repos"] = len(b["repos"])
        judged = b["confirmed"] + b["dismissed"]
        b["precision"] = round(b["confirmed"] / judged, 3) if judged else None
        b["accepted_per_100"] = round(100 * b["accepted"] / b["flags"], 1) if b["flags"] else 0.0
    return by


def tune_detectors(stats: dict, cfg: dict) -> tuple[dict, list[str]]:
    """Raise thresholds on noisy detectors, surface precise ones first. Small, cheap, and the first to build."""
    new = copy.deepcopy(cfg)
    changes = []
    for det, b in stats.items():
        d = new["detectors"].setdefault(det, {})
        noisy = b["flags"] >= 20 and b["accepted_per_100"] < 2 and b["dismissed"] > b["confirmed"]
        sharp = b["flags"] >= 10 and (b["precision"] or 0) >= 0.6 and b["accepted_per_100"] >= 10
        if noisy and det in THRESHOLD:
            key = THRESHOLD[det]
            old = d.get(key, pipeline.DEFAULT["detectors"][det][key])
            d[key] = old + 1
            changes.append(f"{det}: {key} {old} -> {old + 1} (accepted {b['accepted_per_100']} per 100 flags, "
                           f"{b['dismissed']} dismissed against {b['confirmed']} confirmed)")
        elif noisy and det == "failed_command" and b["confirmed"] == 0 and b["flags"] >= 30:
            d["enabled"] = False
            changes.append(f"failed_command: off ({b['flags']} flags, none confirmed, {b['dismissed']} dismissed)")
        elif noisy:
            d["weight"] = round(d.get("weight", 1.0) * 0.8, 3)
            changes.append(f"{det}: weight -> {d['weight']} (noisy)")
        elif sharp:
            d["weight"] = round(d.get("weight", 1.0) * 1.25, 3)
            changes.append(f"{det}: weight -> {d['weight']} (precise: {b['precision']:.0%} confirmed, "
                           f"{b['accepted_per_100']} rules per 100 flags)")
    # Sources whose suggestions you keep get drafted first; your own tags always come first.
    st = learn.stats()
    ranked = sorted((s for s in st if s != "human_tag" and st[s]["acceptance"] is not None),
                    key=lambda s: -(st[s]["acceptance"] or 0))
    if ranked:
        order = {"human_tag": 0, **{s: i + 1 for i, s in enumerate(ranked)}}
        for s, v in pipeline.DEFAULT["source_priority"].items():
            order.setdefault(s, len(order) + v)
        if order != new.get("source_priority"):
            new["source_priority"] = order
            changes.append("source order: " + " > ".join(sorted(order, key=order.get)))
    return new, changes


def backtest_detectors(old: dict, new: dict) -> dict:
    """Run both settings over the logged sessions: the new one must keep the flags that led to accepted rules."""
    a, b = detector_stats(old), detector_stats(new)
    keep = lambda s: sum(v["accepted"] for v in s.values())
    noise = lambda s: sum(v["flags"] - v["accepted"] for v in s.values())
    return {"accepted_kept": (keep(b), keep(a)), "other_flags": (noise(b), noise(a)),
            "passes": keep(b) >= keep(a) and noise(b) <= noise(a)}


# ---------------------------------------------------------------- 2. the drafter

def benchmark(rebuild: bool = False) -> list[dict]:
    """A frozen set of past episodes with what became of them. Rebuilt only on request, so versions compare fairly."""
    f = pipeline.folder() / "benchmark.jsonl"
    if f.exists() and not rebuild:
        return [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    hist = learn.history()
    fate = {}
    for r in hist:
        if r.get("decision") and r.get("candidate_id"):
            fate[r["candidate_id"]] = r
    out = []
    for r in hist:
        if r.get("outcome") != "queued" or not r.get("episode_text") or r.get("source_kind") in ("bootstrap", "chat"):
            continue
        d = fate.get(r.get("candidate_id"))
        if not d:
            continue
        rule = rules.find(r["repo"], r["candidate_id"], levels=False) if r.get("repo") else None
        replay = (((rule or {}).get("evidence") or {}).get("replay") or {}).get("verdict") or d.get("replay")
        out.append({"episode": r["episode"], "repo": r.get("repo"), "source": r.get("source_kind"), "ts": r.get("ts", 0),
                    "text": r["episode_text"], "draft_text": r.get("draft_text"), "decision": d["decision"],
                    "final_text": d.get("final_text"), "reject_reason": d.get("reject_reason"), "replay": replay})
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("".join(json.dumps(x) + "\n" for x in out))
    return out


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def score(fields: dict | None, entry: dict) -> tuple[float, float]:
    """(score, weight). Close to what you accepted is good; close to what you rejected is bad; skipping a rejected
    episode is right. Replay outweighs taste: an entry whose rule passed replay counts double."""
    draft = " ".join(str((fields or {}).get("directive", "")).split())
    w = 2.0 if entry.get("replay") == "passes" else 1.0
    if entry["decision"].startswith("accepted"):
        return (_ratio(draft, entry.get("final_text") or entry.get("draft_text")) if draft else 0.0), w
    return (1.0 if not draft else 1.0 - _ratio(draft, entry.get("draft_text"))), w


VARIANTS = [
    ("fewer examples", {"drafter": {"examples": {"accepted": 2, "rejected": 2}}}),
    ("more examples", {"drafter": {"examples": {"accepted": 8, "rejected": 8}}}),
    ("no examples", {"drafter": {"examples": {"accepted": 0, "rejected": 0}}}),
    ("name the command", {"extra": "Name the exact command, file or function the rule is about, in backticks."}),
    ("short", {"extra": "Keep the directive under 160 characters; put the reason in the rationale."}),
    ("skip one-offs", {"extra": "If the same mistake would not recur in another task in this repository, skip."}),
]


def variant(cfg: dict, change: dict) -> dict:
    v = copy.deepcopy(cfg)
    if "extra" in change:
        v["drafter"]["extra"] = list(v["drafter"].get("extra") or []) + [change["extra"]]
    for k, val in change.items():
        if k != "extra":
            v[k] = pipeline._merge(v.get(k) or {}, val)
    return v


def evaluate(cfg: dict, entries: list[dict], drafter_cmd: str | None, budget: float) -> dict:
    tot = wsum = cost = 0.0
    for e in entries:
        fields, why, c = learn.draft({"source": e["source"], "text": e["text"]}, drafter_cmd, budget, cfg, before=e["ts"])
        cost += c
        s, w = score(fields, e)
        tot += s * w
        wsum += w
    return {"score": round(tot / wsum, 4) if wsum else 0.0, "n": len(entries), "cost": round(cost, 2)}


def split(entries: list[dict]) -> tuple[list[dict], list[dict], str]:
    """Hold out a repo the drafter is not tuned on when there are several; otherwise the newest 30%."""
    repos = sorted({e["repo"] for e in entries}, key=lambda r: max(e["ts"] for e in entries if e["repo"] == r))
    if len(repos) >= 2:
        held = repos[-1]
        return [e for e in entries if e["repo"] != held], [e for e in entries if e["repo"] == held], f"repo {held}"
    es = sorted(entries, key=lambda e: e["ts"])
    k = max(1, int(len(es) * 0.7))
    return es[:k], es[k:], "newest 30%"


def tune_drafter(drafter_cmd: str | None, budget: float, names: list[str] | None = None, progress=print) -> dict:
    entries = benchmark()
    if len(entries) < 4:
        return {"error": f"the benchmark has {len(entries)} decided suggestions; at least 4 are needed"}
    tune, held, how = split(entries)
    if not held:
        return {"error": "nothing left to check on after the split"}
    base = pipeline.active()
    cands = [(n, variant(base, c)) for n, c in VARIANTS if not names or n in names]
    progress(f"  tuning on {len(tune)} episodes, checking on {len(held)} ({how})")
    cur = evaluate(base, tune, drafter_cmd, budget)
    progress(f"  current {base['version']:<16} {cur['score']:.3f}")
    best = ("current", base, cur)
    for n, v in cands:
        r = evaluate(v, tune, drafter_cmd, budget)
        progress(f"  {n:<24} {r['score']:.3f}")
        if r["score"] > best[2]["score"]:
            best = (n, v, r)
    if best[0] == "current":
        return {"improved": False, "tune": cur, "split": how}
    h_cur, h_new = evaluate(base, held, drafter_cmd, budget), evaluate(best[1], held, drafter_cmd, budget)
    out = {"variant": best[0], "tune": {"current": cur, "new": best[2]}, "held": {"current": h_cur, "new": h_new},
           "split": how, "improved": h_new["score"] > h_cur["score"] + 0.01}
    if out["improved"]:
        out["version"] = pipeline.save_new(best[1], f"drafter: {best[0]}; held-out {h_cur['score']:.3f} -> {h_new['score']:.3f}",
                                           base["version"])
    return out


# ---------------------------------------------------------------- 3. ranking policy

def update_ranking(explore: float | None = None) -> dict:
    """A contextual bandit over rule features, pooled across all rules. Reward: 1 minus the avoidable-step ratio of
    each session a rule was given in. Weights are the reward each feature value earns above the average."""
    base = pipeline.active()
    con = store.connect()
    store.sync(con)
    acc, rewards, comp, n_sessions = {}, [], [], 0
    stats_out = {}
    for slug in rules.known_slugs():
        idx = {r["id"]: r for r in index.load(slug).get("rules", [])}
        st = stats_out.setdefault(slug, {})
        for row in store.sessions(con, slug, 5000):
            evs = store.events(con, row["id"])
            given = [i for e in evs if e.get("event") == "rule_injected" for i in e.get("rule_ids", [])]
            for e in evs:
                if e.get("event") == "rule_violation" and e.get("rule_id"):
                    st.setdefault(e["rule_id"], {"fired": 0, "ignored": 0})["ignored"] += 1
            for i in set(given):
                st.setdefault(i, {"fired": 0, "ignored": 0})["fired"] += 1
            if not given or any(e.get("event") == "session_start" and e.get("held_out") for e in evs):
                continue
            n_sessions += 1
            rw = 1.0 - detectors.avoidable_ratio(evs, detectors.detect(evs))
            rewards.append(rw)
            if any(e.get("event") == "rule_withheld" and e.get("reason") == "budget" for e in evs):
                comp.append((slug, set(given), rw))
            for i in set(given):
                if i in idx:
                    for f in pipeline.features(idx[i], st):
                        a = acc.setdefault(f, [0.0, 0])
                        a[0] += rw
                        a[1] += 1
    con.close()
    (paths.local() / "rule_stats.json").write_text(json.dumps(stats_out))
    if len(rewards) < 5:
        return {"error": f"{len(rewards)} session{'s' if len(rewards) != 1 else ''} with rules given so far; at least 5 are needed"}
    mean = sum(rewards) / len(rewards)
    weights = {f: round((s + mean) / (n + 1) - mean, 4) for f, (s, n) in acc.items() if n >= 3}  # shrunk to the mean
    new = copy.deepcopy(base)
    new["ranking"] = {"explore": base["ranking"].get("explore", 0.1) if explore is None else explore, "weights": weights}
    top = sorted(weights.items(), key=lambda kv: -abs(kv[1]))[:6]
    vid = pipeline.save_new(new, f"ranking from {n_sessions} sessions", base["version"])
    return {"version": vid, "sessions": n_sessions, "mean_reward": round(mean, 3), "competing": len(comp),
            "strongest": top}


# ---------------------------------------------------------------- 4. rule-form priors

def _fate(slug: str, r: dict, use: dict) -> str | None:
    st = r.get("status")
    if st == "candidate":
        return None
    ev = r.get("evidence") or {}
    reg = [h.get("state") for h in ev.get("regress") or []]
    u = use.get(r.get("id"), {})
    ignored = u.get("ignored", 0) / max(u.get("fired", 0), 1) if u.get("fired") else 0
    if st == "deprecated" or (reg and reg[-1] in ("obsolete", "harmful", "broken")) or ignored >= 0.3:
        return "died"
    return "survived"


def priors(min_n: int = 5) -> dict:
    """Which kinds of rule survive regression and get followed. Findings become preferences: the drafter is told,
    lint warns. They never block."""
    st = json.loads((paths.local() / "rule_stats.json").read_text()) if (paths.local() / "rule_stats.json").exists() else {}
    rows = []
    for slug in rules.known_slugs():
        for r in rules.load_all(slug, include_shared=False):
            fate = _fate(slug, r, st.get(slug, {}))
            if fate:
                f = learn.rule_features(r)
                f["directive_chars"] = learn._bucket("directive_chars", f["directive_chars"])
                rows.append((f, fate == "survived"))
    if not rows:
        return {"error": "no decided rules yet"}
    overall = sum(ok for _, ok in rows) / len(rows)
    found = []
    for feat in ("kind", "directive_chars", "scoped", "has_rationale", "has_example", "source"):
        vals = {}
        for f, ok in rows:
            v = vals.setdefault(str(f.get(feat)), [0, 0])
            v[0] += ok
            v[1] += 1
        for val, (k, n) in vals.items():
            if n >= min_n and abs(k / n - overall) >= 0.15:
                found.append({"feature": feat, "value": val, "rate": round(k / n, 2), "n": n,
                              "direction": "better" if k / n > overall else "worse"})
    return {"overall": round(overall, 2), "n": len(rows), "findings": found}


PHRASE = {("directive_chars", "long"): "Keep directives short: long ones were ignored or retired more often.",
          ("directive_chars", "short"): "Short directives survived best.",
          ("has_example", "True"): "Name the concrete command, file or forbidden form; rules that did survived better.",
          ("has_example", "False"): "Abstract rules without a concrete command or form were retired more often.",
          ("has_rationale", "True"): "Give the reason in the rationale; rules with one survived better.",
          ("scoped", "True"): "Prefer the narrowest path glob; scoped rules survived better.",
          ("scoped", "False"): "Repo-wide rules were retired more often; scope rules to the files involved."}


def apply_priors(res: dict) -> str | None:
    if not res.get("findings"):
        return None
    base = pipeline.active()
    new = copy.deepcopy(base)
    pri, prefer = [], []
    for f in res["findings"]:
        msg = PHRASE.get((f["feature"], f["value"])) or (
            f"Rules with {f['feature'].replace('_', ' ')} = {f['value']} {'survived' if f['direction'] == 'better' else 'died'} "
            f"more often ({f['rate']:.0%} against {res['overall']:.0%}, n={f['n']}).")
        pri.append({**f, "message": msg})
        prefer.append(msg)
    new["drafter"]["priors"] = pri
    new["drafter"]["prefer"] = prefer
    return pipeline.save_new(new, f"rule-form priors from {res['n']} rules", base["version"])


# ---------------------------------------------------------------- 5. transfer: blind comparison and report

def compare(slug: str, root, versions: list[str], drafter_cmd: str | None, budget: float, limit: int = 10,
            progress=print) -> dict:
    """Draft this repo's new episodes with each pipeline and queue the results together, source hidden, so your
    review cannot favour one. Identical drafts are queued once and share their fate."""
    cfgs = [pipeline.load(v) for v in versions]
    eps = learn.episodes(slug)[:limit]
    state = learn._state()
    out = {"episodes": len(eps), "queued": {v: 0 for v in versions}, "shared": 0, "cost": 0.0}
    rng = random.Random()
    for ep in eps:
        drafts = []
        for cfg in cfgs:
            fields, why, c = learn.draft(ep, drafter_cmd, budget, cfg)
            out["cost"] += c
            drafts.append((cfg, fields, why, c))
        order = list(range(len(drafts)))
        rng.shuffle(order)
        queued_texts = []
        for i in order:
            cfg, fields, why, c = drafts[i]
            d = " ".join(str((fields or {}).get("directive", "")).split())
            same = next((q for q in queued_texts if d and learn._similar(d, q[0]) >= 0.6), None)
            if same:
                learn.log({"episode": ep["key"], "source_kind": ep["source"], "repo": slug, "outcome": "shared",
                           "shared_with": same[1], "draft_text": d, "pipeline_version": cfg["version"],
                           "episode_text": (ep.get("text") or "")[:5000], "draft_cost": c})
                out["shared"] += 1
                continue
            res = learn.queue(slug, root, ep, fields, why, c, cfg, progress=lambda *_: None, check_duplicates=True)
            if res["outcome"] == "queued":
                out["queued"][cfg["version"]] += 1
                queued_texts.append((d, res["id"]))
        state["seen"].append(ep["key"])
    (paths.local() / "learn_state.json").write_text(json.dumps(state))
    rules.commit(f"meta compare {', '.join(versions)}")
    return out


def report() -> dict:
    """Per pipeline version: what a win looks like, from the logged suggestions and your decisions on them."""
    hist = learn.history()
    fate = {r["candidate_id"]: r for r in hist if r.get("decision") and r.get("candidate_id")}
    shared = {}
    for r in hist:
        if r.get("outcome") == "shared":
            shared.setdefault(r["shared_with"], []).append(r.get("pipeline_version"))
    by = {}

    def b(v):
        return by.setdefault(v or "p0", {"queued": 0, "decided": 0, "as_written": 0, "accepted": 0, "replayed": 0,
                                         "replay_pass": 0, "review_secs": 0.0, "timed": 0})
    con = store.connect()
    store.sync(con)
    for r in hist:
        if r.get("outcome") != "queued" or not r.get("candidate_id"):
            continue
        owners = [r.get("pipeline_version")] + shared.get(r["candidate_id"], [])
        d = fate.get(r["candidate_id"])
        rule = rules.find(r.get("repo") or "", r["candidate_id"], levels=False) if r.get("repo") else None
        rp_ = (((rule or {}).get("evidence") or {}).get("replay") or {}).get("verdict")
        for v in owners:
            x = b(v)
            x["queued"] += 1
            if rp_:
                x["replayed"] += 1
                x["replay_pass"] += rp_ == "passes"
            if d:
                x["decided"] += 1
                x["as_written"] += d["decision"] == "accepted"
                x["accepted"] += d["decision"].startswith("accepted")
                if d.get("review_secs") is not None:
                    x["review_secs"] += d["review_secs"]
                    x["timed"] += 1
    # Sessions needed to reach five accepted rules, per version and repo.
    acc_ts = {}
    for c, d in fate.items():
        if d["decision"].startswith("accepted"):
            q = next((r for r in hist if r.get("candidate_id") == c and r.get("outcome") == "queued"), None)
            if q:
                acc_ts.setdefault((q.get("pipeline_version") or "p0", q.get("repo")), []).append(d.get("ts", 0))
    for (v, repo_), ts in acc_ts.items():
        if len(ts) >= 5 and repo_:
            fifth = sorted(ts)[4]
            n = con.execute("SELECT COUNT(*) FROM sessions WHERE repo=? AND started <= ?", (repo_, fifth)).fetchone()[0]
            b(v).setdefault("sessions_to_five", {})[repo_] = n
    con.close()
    for x in by.values():
        x["as_written_rate"] = round(x["as_written"] / x["decided"], 3) if x["decided"] else None
        x["replay_pass_rate"] = round(x["replay_pass"] / x["replayed"], 3) if x["replayed"] else None
        x["review_min_per_accepted"] = round(x["review_secs"] / 60 / x["accepted"], 2) if x["accepted"] and x["timed"] else None
    return by


# ---------------------------------------------------------------- CLI

def cli(a, say, nxt, confirm, ctx, refresh) -> int:
    from . import BIN
    what = a.rule or "status"
    budget = min(a.budget, 0.25) if a.budget else 0.25
    if what == "status":
        cur = pipeline.version()
        if a.json:
            print(json.dumps({"active": cur, "versions": pipeline.versions()}))
            return 0
        say(f"Active pipeline: {cur}")
        for v in pipeline.versions():
            c = pipeline.load(v)
            say(f"  {'*' if v == cur else ' '} {v:<5} {c.get('note', '')}" + (f"  ({c['created']})" if c.get("created") else ""))
        n = sum(1 for r in learn.history() if r.get("outcome") == "queued")
        d = sum(1 for r in learn.history() if r.get("decision"))
        say(f"\n{n} suggestions logged, {d} decided: the ground the outer loop learns on.")
        say("It tunes detectors, the drafter, the ranking policy and rule-form priors. It cannot change lint, redaction,")
        say("the review gate or the enforcement test, and nothing it makes is used before you promote it.")
        nxt(f"{BIN} lab meta detectors    the cheapest step: which flags lead to rules you keep")
        return 0
    if what == "detectors":
        st = detector_stats()
        if a.json:
            print(json.dumps(st))
            return 0
        if not st:
            say("No flags yet. They come from captured sessions.")
            return 0
        say(f"  {'Detector':<18} {'Flags':>5} {'Confirmed':>9} {'Dismissed':>9} {'Rules':>5} {'Per 100':>7}  Repos")
        for k, b in sorted(st.items(), key=lambda kv: -kv[1]["flags"]):
            say(f"  {k:<18} {b['flags']:>5} {b['confirmed']:>9} {b['dismissed']:>9} {b['accepted']:>5} {b['accepted_per_100']:>7}  {b['repos']}")
        new, changes = tune_detectors(st, pipeline.active())
        if not changes:
            say("\nNo change suggested yet: a detector needs 10 to 20 flags before its precision means anything.")
            return 0
        say("\nSuggested changes:")
        for c in changes:
            say(f"  - {c}")
        bt = backtest_detectors(pipeline.active(), new)
        say(f"\nBacktest on the logged sessions: flags behind accepted rules {bt['accepted_kept'][1]} -> {bt['accepted_kept'][0]}, "
            f"other flags {bt['other_flags'][1]} -> {bt['other_flags'][0]}.")
        if a.dry_run:
            return 0
        if not bt["passes"]:
            say("Not saved: the new settings would lose flags that led to rules you kept.")
            return 1
        vid = pipeline.save_new(new, "detectors: " + "; ".join(changes)[:200])
        say(f"Saved as {vid}. It is not used until you promote it.")
        nxt(f"{BIN} lab meta promote {vid}")
        return 0
    if what == "benchmark":
        es = benchmark(rebuild=True)
        say(f"Froze {len(es)} decided suggestions into {pipeline.folder() / 'benchmark.jsonl'}"
            f" ({len({e['repo'] for e in es})} repos, {sum(1 for e in es if e['replay'] == 'passes')} with a passing replay).")
        say("Tuning compares versions on this set until you rebuild it.")
        return 0
    if what == "drafter":
        es = benchmark()
        names = [x.strip() for x in a.arg2.split(",")] if a.arg2 else None
        nv = len([v for v in VARIANTS if not names or v[0] in names])
        calls = len(es) * (nv + 1) + max(1, len(es) // 3) * 2
        say(f"Drafter tuning: {nv} variants on {len(es)} past episodes, about {calls} drafting calls, up to ${calls * budget:.2f}.")
        say("Each variant redrafts each episode seeing only the decisions made before it. A winner must also beat the")
        say("current drafter on held-out episodes (another repo when there is one), and is saved, not promoted.")
        if a.dry_run or not confirm(a):
            return 0 if a.dry_run else 1
        res = tune_drafter(a.drafter_cmd, budget, names, progress=say)
        if a.json:
            print(json.dumps(res))
            return 0
        if res.get("error"):
            say(f"Not run: {res['error']}. Decide more suggestions in  {BIN} review  first.")
            return 1
        if not res.get("variant"):
            say("\nNo variant beat the current drafter.")
            return 0
        say(f"\nBest variant: {res['variant']}. Held-out ({res['split']}): {res['held']['current']['score']:.3f} -> "
            f"{res['held']['new']['score']:.3f}")
        if res["improved"]:
            say(f"Saved as {res['version']}.")
            nxt(f"{BIN} lab meta promote {res['version']}")
        else:
            say("It did not beat the current drafter on the held-out episodes, so nothing was saved.")
        return 0
    if what == "ranking":
        res = update_ranking()
        if a.json:
            print(json.dumps(res))
            return 0
        if res.get("error"):
            say(f"Not run: {res['error']}.")
            return 1
        say(f"Ranking policy from {res['sessions']} sessions ({res['competing']} where rules competed for the budget), "
            f"mean reward {res['mean_reward']}.")
        for f, w in res["strongest"]:
            say(f"  {f:<28} {w:+.3f}")
        say(f"Saved as {res['version']}. It explores: now and then a lower-ranked rule gets the last slot.")
        if res["competing"] < 20:
            say("Fewer than 20 sessions where rules competed: the weights are a weak signal so far.")
        nxt(f"{BIN} lab meta promote {res['version']}")
        return 0
    if what == "priors":
        res = priors()
        if a.json:
            print(json.dumps(res))
            return 0
        if res.get("error"):
            say(f"Not run: {res['error']}.")
            return 1
        say(f"{res['n']} decided rules, {res['overall']:.0%} survived (still active, not retired by regression, followed).")
        for f in res["findings"]:
            say(f"  {f['feature']}={f['value']:<8} {f['rate']:.0%} survived (n={f['n']}), {f['direction']}")
        if not res["findings"]:
            say("No pattern strong enough yet (a feature value needs 5 rules and 15 points from the average).")
            return 0
        if a.dry_run:
            return 0
        vid = apply_priors(res)
        say(f"Saved as {vid}: the drafter is told these, and lint warns (never blocks).")
        nxt(f"{BIN} lab meta promote {vid}")
        return 0
    if what == "compare":
        slug, root = ctx()
        vs = [x.strip() for x in (a.arg2 or "").split(",") if x.strip()] or ["p0", pipeline.version()]
        if len(set(vs)) < 2:
            say(f"Name two versions: {BIN} lab meta compare p0,p1")
            return 2
        eps = learn.episodes(slug)
        n = min(len(eps), a.limit if a.limit != 60 else 10)
        say(f"Blind comparison on {slug}: {n} new episodes drafted by {' and '.join(vs)}, about {n * len(vs)} drafting calls,"
            f" up to ${n * len(vs) * budget:.2f}.")
        say("Both pipelines' suggestions go into one review queue with the source hidden. Review them as usual.")
        if not n:
            say("No new episodes here to draft from.")
            return 0
        if a.dry_run or not confirm(a):
            return 0 if a.dry_run else 1
        res = compare(slug, root, vs, a.drafter_cmd, budget, n, progress=say)
        refresh(slug, root)
        say(f"Queued {', '.join(f'{k}: {v}' for k, v in res['queued'].items())}; {res['shared']} drafts were the same and share one suggestion.")
        nxt(f"{BIN} review    then  {BIN} lab meta report")
        return 0
    if what == "report":
        res = report()
        if a.json:
            print(json.dumps(res))
            return 0
        if not res:
            say("Nothing to report yet.")
            return 0
        pct = lambda v: f"{100 * v:.0f}%" if v is not None else "-"
        say(f"  {'Version':<8} {'Queued':>6} {'Decided':>7} {'As written':>10} {'Replay pass':>11} {'Review min/rule':>15}  Sessions to 5 rules")
        for v, x in sorted(res.items()):
            s5 = ", ".join(f"{n} ({r.split('/')[-1]})" for r, n in (x.get("sessions_to_five") or {}).items()) or "-"
            say(f"  {v:<8} {x['queued']:>6} {x['decided']:>7} {pct(x['as_written_rate']):>10} {pct(x['replay_pass_rate']):>11} "
                f"{x['review_min_per_accepted'] if x['review_min_per_accepted'] is not None else '-':>15}  {s5}")
        say("\nA win for a newer version: more accepted as written, a higher replay pass rate, fewer sessions to five")
        say("rules and fewer review minutes, on a repo neither was tuned on. Lift on held-out tasks comes from")
        say(f"  {BIN} lab exp run --arms bare,rules   with each version's accepted rules.")
        say("With three repos this is a case study, not a finding about repos in general; the per-suggestion rates are firmer.")
        return 0
    if what == "promote":
        v = a.arg2
        if not v:
            say(f"Which version? {', '.join(pipeline.versions())}")
            return 2
        if a.dry_run:
            say(f"Would make {v} the active pipeline.")
            return 0
        try:
            prev = pipeline.promote(v)
        except Exception:
            say(f"No pipeline version {v}.")
            return 1
        say(f"{v} is active (was {prev}). New suggestions are drafted by it. Undo: {BIN} lab meta rollback")
        return 0
    if what == "rollback":
        if a.dry_run:
            say("Would go back to the previously active pipeline.")
            return 0
        v = pipeline.rollback()
        say(f"Back to {v}." if v else "Nothing to roll back to.")
        return 0 if v else 1
    say(f"{BIN} lab meta [status|detectors|benchmark|drafter|ranking|priors|compare|report|promote <v>|rollback]")
    return 2
