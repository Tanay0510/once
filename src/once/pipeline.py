"""The learning pipeline as versioned data: what the outer loop may tune, and nothing else.

A version holds the drafter's extra instructions and example policy, detector thresholds and weights, the
order in which episode sources are drafted, and the ranking policy's weights. Lint, redaction, the review
gate and the enforcement promotion test are not in here, so the outer loop cannot reach them.

Versions live in the memory repo (`_pipeline/`), so promoting one is a commit and rolling back is one command.
Kept import-light (json only): the hook reads detector thresholds and ranking weights on the hot path.
"""
import json
import time
from pathlib import Path

from . import paths

DEFAULT = {
    "version": "p0",
    "parent": None,
    "note": "the starting pipeline",
    "drafter": {"extra": [], "examples": {"accepted": 4, "rejected": 4}, "prefer": []},
    "detectors": {
        "retry_cluster": {"min_repeats": 2, "weight": 1.0},
        "failed_command": {"enabled": True, "weight": 1.0},
        "repeated_read": {"min_reads": 3, "weight": 1.0},
        "reverted_edit": {"weight": 1.0},
        "wandering_search": {"min_steps": 8, "weight": 1.0},
        "ignored_rule": {"weight": 1.0},
    },
    "source_priority": {"human_tag": 0, "missed_file": 1, "pr_review": 2, "retry_cluster": 3, "bootstrap": 4},
    "ranking": {"explore": 0.1, "weights": {}},
}
ALLOWED = set(DEFAULT) | {"created", "benchmark", "changes"}


def folder() -> Path:
    return paths.memory() / "_pipeline"


def _state() -> dict:
    try:
        return json.loads((folder() / "active.json").read_text())
    except Exception:
        return {"active": "p0", "history": []}


_cache: dict = {}


def load(version: str | None = None) -> dict:
    """A version merged over the defaults, with unknown keys dropped."""
    v = version or _state().get("active", "p0")
    key = (str(folder()), v)  # one process can see several once homes (tests, the demo)
    if key in _cache:
        return _cache[key]
    cfg = json.loads(json.dumps(DEFAULT))
    if v != "p0" or (folder() / "versions" / "p0.json").exists():
        try:
            got = json.loads((folder() / "versions" / f"{v}.json").read_text())
            for k, val in got.items():
                if k not in ALLOWED:
                    continue
                if isinstance(val, dict) and isinstance(cfg.get(k), dict):
                    cfg[k] = _merge(cfg[k], val)
                else:
                    cfg[k] = val
        except Exception:
            if v != "p0":
                raise KeyError(f"no pipeline version {v}")
    cfg["version"] = v
    _cache[key] = cfg
    return cfg


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def active() -> dict:
    return load()


def version() -> str:
    return _state().get("active", "p0")


def versions() -> list[str]:
    d = folder() / "versions"
    found = {f.stem for f in d.glob("p*.json")} if d.is_dir() else set()
    return sorted(found | {"p0"}, key=lambda v: int(v[1:]) if v[1:].isdigit() else 0)


def save_new(cfg: dict, note: str, parent: str | None = None) -> str:
    """Store a tuned pipeline as the next version. It is not used until promoted."""
    nums = [int(v[1:]) for v in versions() if v[1:].isdigit()]
    vid = f"p{max(nums, default=0) + 1}"
    clean = {k: v for k, v in cfg.items() if k in ALLOWED}
    clean.update({"version": vid, "parent": parent or cfg.get("version"), "note": note,
                  "created": time.strftime("%Y-%m-%d %H:%M")})
    d = folder() / "versions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{vid}.json").write_text(json.dumps(clean, indent=2))
    _cache.pop((str(folder()), vid), None)
    from . import rules
    rules.commit(f"pipeline: draft {vid} ({note})")
    return vid


def promote(vid: str) -> str:
    load(vid)  # raises when it does not exist
    st = _state()
    prev = st.get("active", "p0")
    if prev != vid:
        st["history"] = (st.get("history") or []) + [prev]
    st["active"] = vid
    folder().mkdir(parents=True, exist_ok=True)
    (folder() / "active.json").write_text(json.dumps(st))
    _cache.clear()
    from . import rules
    rules.commit(f"pipeline: promote {vid} (was {prev})")
    rules.compile_all()
    return prev


def rollback() -> str | None:
    st = _state()
    hist = st.get("history") or []
    if not hist:
        return None
    st["active"] = hist.pop()
    st["history"] = hist
    (folder() / "active.json").write_text(json.dumps(st))
    _cache.clear()
    from . import rules
    rules.commit(f"pipeline: roll back to {st['active']}")
    rules.compile_all()
    return st["active"]


# ---------------------------------------------------------------- ranking policy (used when indexes are built)

def _bucket_age(created: str) -> str:
    try:
        days = (time.time() - time.mktime(time.strptime(created[:10], "%Y-%m-%d"))) / 86400
    except Exception:
        return "unknown"
    return "new" if days < 30 else ("mid" if days < 180 else "old")


def features(r: dict, stats: dict | None = None) -> list[str]:
    """Rule features the policy learns weights for. Pooled across every rule, so data from one rule helps all."""
    s = (stats or {}).get(r.get("id"), {})
    fired, ignored = s.get("fired", 0), s.get("ignored", 0)
    fire = "never" if not fired else ("some" if fired < 10 else "often")
    ign = "none" if not ignored else ("some" if ignored / max(fired, 1) < 0.2 else "often")
    return [f"kind={r.get('kind') or '?'}", f"wide={bool(r.get('repo_wide'))}",
            f"depth={min(int(r.get('depth') or 0), 3)}", f"age={_bucket_age(r.get('created') or '')}",
            f"level={r.get('scope_level', 'repo')}", f"source={r.get('source') or '?'}",
            f"fired={fire}", f"ignored={ign}"]


def rule_stats(slug: str) -> dict:
    try:
        return json.loads((paths.local() / "rule_stats.json").read_text()).get(slug, {})
    except Exception:
        return {}


def annotate_index(slug: str, entries: list[dict]) -> dict:
    cfg = active()
    w = (cfg.get("ranking") or {}).get("weights") or {}
    st = rule_stats(slug)
    for r in entries:
        r["score"] = round(sum(float(w.get(f, 0.0)) for f in features(r, st)), 4)
    return {"policy": {"version": cfg["version"], "explore": float((cfg.get("ranking") or {}).get("explore", 0.1))}}


# ---------------------------------------------------------------- detectors (hot path: no yaml)

def detector_cfg() -> dict:
    try:
        return active().get("detectors") or DEFAULT["detectors"]
    except Exception:
        return DEFAULT["detectors"]


def source_priority() -> dict:
    try:
        return active().get("source_priority") or DEFAULT["source_priority"]
    except Exception:
        return DEFAULT["source_priority"]


def reset_cache():
    _cache.clear()
