"""Cost accounting: one headline number, cost per verified task, and the four that explain it.

  - cost per verified task, per arm: total spend over tasks that passed their check (from experiments)
  - memory overhead: injected text is re-read on every later model call, so injected tokens x later calls
  - saving per rule: from its replay, cost (or tokens) per run without the rule minus with it
  - break-even firings: what the rule cost to produce (drafting + replays) over its saving per firing

Prices are not hard-coded. A small dated `prices.yaml` in the once home holds them, you keep it up to date, and
every report names the file and its date. Lab runs report their own dollar cost (claude -p does); interactive
sessions only report tokens, so their dollars need the price file. On a flat-rate plan the figure is an
API-equivalent cost, not what you pay.
"""
import json
import time
from pathlib import Path

import yaml

from . import learn, paths, rules, store


def prices_file() -> Path:
    return paths.home() / "prices.yaml"


def load_prices() -> dict | None:
    try:
        p = yaml.safe_load(prices_file().read_text()) or {}
    except Exception:
        return None
    models = {k: v for k, v in (p.get("models") or {}).items() if isinstance(v, dict) and v.get("input") is not None}
    return {**p, "models": models} if models else None


def init_prices(models: list[str]) -> Path:
    """A template to fill in: dollars per million tokens, per model, with the date you copied them."""
    f = prices_file()
    if f.exists():
        return f
    body = {"date": None, "currency": "USD", "unit": "dollars per million tokens",
            "note": "Copy current prices from your provider's pricing page and set the date. once never guesses them.",
            "models": {m: {"input": None, "output": None, "cache_read": None, "cache_write": None}
                       for m in (models or ["your-model-id"])}}
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml.safe_dump(body, sort_keys=False))
    return f


def dollars(usage: dict, model: str | None, prices: dict | None) -> float | None:
    if not prices or not usage:
        return None
    p = prices["models"].get(model or "") or (next(iter(prices["models"].values())) if len(prices["models"]) == 1 else None)
    if not p:
        return None
    rate = lambda k: float(p.get(k) or p.get("input") or 0) / 1e6
    return (usage.get("input_tokens", 0) * rate("input") + usage.get("output_tokens", 0) * float(p.get("output") or 0) / 1e6
            + usage.get("cache_read_input_tokens", 0) * rate("cache_read")
            + usage.get("cache_creation_input_tokens", 0) * rate("cache_write"))


def report(slug: str, days: int = 30) -> dict:
    since = time.time() - days * 86400
    prices = load_prices()
    con = store.connect()
    store.sync(con)
    tot = {"sessions": 0, "with_usage": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0,
           "cache_creation_input_tokens": 0, "model_calls": 0, "dollars": 0.0, "priced": 0,
           "injected_tokens": 0, "overhead_tokens": 0, "overhead_dollars": 0.0}
    models = set()
    for row in store.sessions(con, slug, 5000):
        if (row["started"] or 0) < since:
            continue
        tot["sessions"] += 1
        evs = store.events(con, row["id"])
        end = next((e for e in evs if e.get("event") == "session_end" and e.get("usage")), None)
        inj = sum(int(e.get("chars") or 0) for e in evs if e.get("event") == "rule_injected") // 4
        tot["injected_tokens"] += inj
        if not end:
            continue
        u = end["usage"]
        tot["with_usage"] += 1
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "model_calls"):
            tot[k] += int(u.get(k) or 0)
        model = (u.get("models") or [None])[0]
        models.add(model)
        d = dollars(u, model, prices)
        if d is not None:
            tot["dollars"] += d
            tot["priced"] += 1
        over = inj * max(int(u.get("model_calls") or 0) - 1, 0)  # re-read on every later call
        tot["overhead_tokens"] += over
        od = dollars({"cache_read_input_tokens": over}, model, prices)
        if od is not None:
            tot["overhead_dollars"] += od
    con.close()
    # Pipeline spend: drafting, replays, regression runs.
    hist = learn.history()
    draft_by = {}
    for r in hist:
        if r.get("repo") == slug and r.get("draft_cost"):
            draft_by[r.get("candidate_id") or r.get("episode")] = draft_by.get(r.get("candidate_id") or r.get("episode"), 0) + r["draft_cost"]
    spend = {"drafting": round(sum(draft_by.values()), 2), "replay": 0.0, "regression": 0.0}
    per_rule = []
    usage = {}
    try:
        from .cli import _rule_usage
        usage = _rule_usage(slug)
    except Exception:
        pass
    for r in rules.load_all(slug, include_shared=False):
        ev = r.get("evidence") or {}
        rp = ev.get("replay") or {}
        rc = float(rp.get("cost_usd") or 0)
        gc = sum(float(h.get("cost_usd") or 0) for h in ev.get("regress") or [])
        spend["replay"] += rc
        spend["regression"] += gc
        if r.get("status") != "active":
            continue
        made = draft_by.get(r["id"], 0) + rc + gc
        pr = rp.get("per_run") or {}
        save_d = (pr.get("without", {}).get("cost_usd") or 0) - (pr.get("with", {}).get("cost_usd") or 0) \
            if pr.get("without", {}).get("cost_usd") is not None and pr.get("with", {}).get("cost_usd") is not None else None
        save_t = (pr.get("without", {}).get("tokens") or 0) - (pr.get("with", {}).get("tokens") or 0) \
            if pr.get("without", {}).get("tokens") is not None and pr.get("with", {}).get("tokens") is not None else None
        save_d = round(save_d, 4) if save_d is not None else None
        fired = usage.get(r["id"], {}).get("fired", 0)
        per_rule.append({"id": r["id"], "cost_to_make": round(made, 2), "saving_per_firing": save_d, "token_saving": save_t,
                         "steps_saved": (rp.get("steps") or {}).get("without", 0) - (rp.get("steps") or {}).get("with", 0) if rp else None,
                         "break_even_firings": round(made / save_d, 1) if save_d and save_d > 0 and made else None,
                         "fired": fired})
    spend = {k: round(v, 2) for k, v in spend.items()}
    # The headline: cost per verified task, per arm, from the latest experiment on this repo.
    headline = None
    try:
        from . import experiment as ex
        got = ex.load(None)
        if got and got[0].get("slug") == slug:
            sm = ex.summarize(*got)
            headline = {"experiment": got[0]["id"], "arms": {a: {"rate": v["rate"], "cost_per_verified": v["cost_per_verified"],
                                                                  "runs": v["runs"]} for a, v in sm["arms"].items()}}
    except Exception:
        pass
    return {"slug": slug, "days": days, "sessions": tot, "pipeline": spend, "rules": per_rule, "headline": headline,
            "prices": {"file": str(prices_file()), "date": (prices or {}).get("date"), "loaded": bool(prices)},
            "models_seen": sorted(m for m in models if m)}


def cli(a, say, ctx) -> int:
    from . import BIN
    slug, _ = ctx()
    if a.init_prices:
        rep = report(slug, a.days)
        f = init_prices(rep["models_seen"])
        say(f"Fill in {f}: dollars per million tokens for each model, and the date you copied them.")
        say("once never guesses prices; reports name this file and its date.")
        return 0
    r = report(slug, a.days)
    if a.json:
        print(json.dumps(r))
        return 0
    s = r["sessions"]
    say(f"Cost for {slug}, last {r['days']} days")
    if r["headline"]:
        say(f"\n  Cost per verified task (experiment {r['headline']['experiment']}):")
        for arm, v in r["headline"]["arms"].items():
            cpv = f"${v['cost_per_verified']:.2f}" if v["cost_per_verified"] else "-"
            say(f"    {arm:<10} {cpv:>8}   verified {100 * v['rate']:.0f}% of {v['runs']} runs")
    else:
        say(f"\n  Cost per verified task: no experiment yet ({BIN} lab exp run).")
    tokens = s["input_tokens"] + s["output_tokens"] + s["cache_read_input_tokens"] + s["cache_creation_input_tokens"]
    say(f"\n  Sessions          {s['sessions']}, {s['with_usage']} with token counts")
    say(f"  Tokens            {tokens:,} ({s['output_tokens']:,} out) over {s['model_calls']:,} model calls")
    if r["prices"]["loaded"]:
        say(f"  API-equivalent    ${s['dollars']:.2f} for {s['priced']} priced sessions"
            f"   (prices from {r['prices']['file']}, dated {r['prices']['date'] or 'no date given'})")
    else:
        say(f"  Dollars           no prices set. Run  {BIN} lab cost --init-prices  and fill in the file.")
    say(f"  Injected by once  {s['injected_tokens']:,} tokens; re-read on later calls: {s['overhead_tokens']:,} tokens"
        + (f" (${s['overhead_dollars']:.2f} at the cached-input rate)" if r["prices"]["loaded"] else ""))
    p = r["pipeline"]
    say(f"  Pipeline spend    drafting ${p['drafting']:.2f}, replays ${p['replay']:.2f}, regression ${p['regression']:.2f}")
    if r["rules"]:
        say(f"\n  {'Rule':<32} {'To make':>8} {'Saves/run':>10} {'Break-even':>11} {'Fired':>6}")
        for x in sorted(r["rules"], key=lambda x: -(x["fired"] or 0)):
            sv = f"${x['saving_per_firing']:.3f}" if x["saving_per_firing"] is not None else (
                f"{x['steps_saved']:+.1f} steps" if x["steps_saved"] else "-")
            be = f"{x['break_even_firings']:.0f} firings" if x["break_even_firings"] else "-"
            say(f"  {x['id'][:32]:<32} {('$%.2f' % x['cost_to_make']) if x['cost_to_make'] else '-':>8} {sv:>10} {be:>11} {x['fired']:>6}")
        say("\n  A rule that needs hundreds of firings to pay back and fires twice a month is not worth keeping on cost")
        say("  grounds, though it may be on correctness grounds. Savings from one replay are noisy; trust them in aggregate.")
    return 0
