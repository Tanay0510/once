"""Rule files: load, lint, write, and compile the hot-path index. Not imported by the hook."""
import json
import os
import re
import subprocess
import time
from pathlib import Path

import yaml

from . import SCHEMA, globs, paths

KINDS = {"invariant", "negative_constraint", "command", "pointer"}
STATUSES = {"candidate", "active", "deprecated"}
MAX_DIRECTIVE = 400
_FORBIDDEN = re.compile(
    r"(?i)(https?://|ignore (all|any|previous)|system prompt|api[_ -]?key|password|credential|"
    r"permission|bypass|sudo |rm -rf|curl |wget )")


def rules_dir(slug: str) -> Path:
    return paths.repo_memory(slug) / "rules"


SHARED_DIR = ".once/rules"  # rules a team commits to the repo; see `once share`


def _read(files, shared=False) -> list[dict]:
    out = []
    for f in files:
        try:
            r = yaml.safe_load(f.read_text()) or {}
            r["_file"] = str(f)
            if shared:
                r["_shared"] = True
                r.setdefault("status", "active")
            out.append(r)
        except Exception as e:  # a broken file must not take the rest down
            out.append({"id": f.stem, "status": "deprecated", "_file": str(f), "_error": str(e)})
    return out


def load_all(slug: str, include_shared: bool = True) -> list[dict]:
    """This machine's rules, plus the ones committed to the repo by the team. A local file with the same id wins."""
    d = rules_dir(slug)
    out = _read(sorted(d.glob("*.yaml"))) if d.is_dir() else []
    if include_shared:
        from . import repo
        root = repo.known_root(slug)
        sd = root / SHARED_DIR if root else None
        if sd and sd.is_dir():
            mine = {r.get("id") for r in out}
            out += [r for r in _read(sorted(sd.glob("*.yaml")), shared=True) if r.get("id") not in mine]
    return out


def active(slug: str) -> list[dict]:
    return [r for r in load_all(slug) if r.get("status") == "active" and "_error" not in r]


# ---------------------------------------------------------------- scope levels above the repo
# Least specific first. A rule at a more specific level wins over one with the same id further up,
# and a repo rule can name an inherited rule it replaces with `overrides: <id>`.
LEVELS = ("user", "agent", "stack", "owner", "repo")
INHERITED_SLOTS = 2  # of the 8 per injection: inherited rules must not crowd out the repo's own


def owner_of(slug: str) -> str | None:
    parts = slug.split("/")
    return "/".join(parts[:2]) if len(parts) >= 3 and not slug.startswith("local/") else None


def level_dir(level: str, slug: str | None = None, stack: str | None = None, agent: str | None = None) -> Path:
    m = paths.memory()
    if level == "user":
        return m / "_user" / "rules"
    if level == "agent":
        return m / "_agents" / (agent or "_") / "rules"
    if level == "stack":
        return m / "_stacks" / (stack or "_") / "rules"
    if level == "owner":
        own = owner_of(slug or "")
        if not own:
            raise ValueError(f"{slug} has no owner (it is not on a code host)")
        return m / own / "_shared" / "rules"
    return rules_dir(slug)


def stacks_for(slug: str) -> list[str]:
    """repo.yaml can name the stacks; otherwise they are read from marker files in the checkout."""
    try:
        cfg = yaml.safe_load((paths.repo_memory(slug) / "repo.yaml").read_text()) or {}
        if isinstance(cfg.get("stacks"), list):
            return [str(s) for s in cfg["stacks"]]
    except Exception:
        pass
    from . import repo
    return repo.stacks(repo.known_root(slug))


def inherited(slug: str) -> list[dict]:
    """Active rules this repo inherits from the user, agent, stack and owner levels, each marked with its level."""
    out = []
    spots = [("user", None, level_dir("user"))]
    agents_dir = paths.memory() / "_agents"
    for d in (sorted(agents_dir.iterdir()) if agents_dir.is_dir() else []):
        spots.append(("agent", d.name, d / "rules"))
    for st in stacks_for(slug):
        spots.append(("stack", st, level_dir("stack", stack=st)))
    if owner_of(slug):
        spots.append(("owner", owner_of(slug), level_dir("owner", slug)))
    for level, key, d in spots:
        if not d.is_dir():
            continue
        for r in _read(sorted(d.glob("*.yaml"))):
            if r.get("status") == "active" and "_error" not in r:
                r["_level"], r["_level_key"] = level, key
                if level == "agent":
                    r.setdefault("scope", {})["agents"] = [key]
                out.append(r)
    return out


def all_levels(slug: str) -> list[dict]:
    """The rules that apply in this repo, after precedence: most specific level wins, `overrides` removes."""
    own = [dict(r, _level="repo") for r in load_all(slug)]
    over = {str(r.get("overrides")) for r in own if r.get("overrides") and r.get("status") == "active"}
    seen = {r.get("id") for r in own if r.get("status") != "deprecated"}  # a retired or lifted copy hides nothing
    rank = {lv: i for i, lv in enumerate(LEVELS)}
    out = list(own)
    for r in sorted(inherited(slug), key=lambda r: -rank[r["_level"]]):
        if r.get("id") in seen or r.get("id") in over:
            continue
        seen.add(r.get("id"))
        out.append(r)
    return out


def known_slugs() -> list[str]:
    try:
        return sorted(json.loads((paths.local() / "roots.json").read_text()))
    except Exception:
        return []


def similar_elsewhere(slug: str, directive: str, threshold: float = 0.6) -> list[tuple[str, dict]]:
    """Active rules in other repos that say the same thing: the cue to lift a rule to a shared level."""
    def words(t):
        return set(re.findall(r"[a-z0-9_]+", (t or "").lower()))
    w = words(directive)
    hits = []
    for other in known_slugs():
        if other == slug:
            continue
        for r in active(other):
            ow = words((r.get("rule") or {}).get("directive"))
            if w and ow and len(w & ow) / len(w | ow) >= threshold:
                hits.append((other, r))
    return hits


def lint(rule: dict, repo_root: Path | None = None) -> list[str]:
    """Deterministic checks. An empty list means the rule may be stored."""
    errs = []
    rid = rule.get("id", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9\-]{2,63}", rid):
        errs.append("id must be a lowercase slug of 3 to 64 characters")
    if rule.get("status") not in STATUSES:
        errs.append(f"status must be one of {sorted(STATUSES)}")
    if rule.get("kind") not in KINDS:
        errs.append(f"kind must be one of {sorted(KINDS)}")
    d = ((rule.get("rule") or {}).get("directive") or "").strip()
    if not d:
        errs.append("directive is empty")
    if len(d) > MAX_DIRECTIVE:
        errs.append(f"directive is over {MAX_DIRECTIVE} characters")
    # Rules a person wrote, and command rules, get the narrow check; model-drafted text gets the wide one.
    relaxed = rule.get("kind") == "command" or (rule.get("provenance") or {}).get("source_kind") in ("manual", "chat")
    m = (re.search(r"(?i)https?://|ignore (all|any|previous) |system prompt", d) if relaxed
         else _FORBIDDEN.search(d))
    if m:
        errs.append(f"directive contains a disallowed phrase: {m.group(0).strip()!r}")
    gl = (rule.get("scope") or {}).get("path_globs") or []
    if not isinstance(gl, list):
        errs.append("scope.path_globs must be a list")
        gl = []
    if repo_root and not globs.is_repo_wide(gl):
        try:
            files = subprocess.run(["git", "-C", str(repo_root), "ls-files"], capture_output=True,
                                   text=True, timeout=10).stdout.splitlines()
        except Exception:
            files = []
        if files:
            for g in gl:
                rx = re.compile(globs.to_regex(g))
                if not any(rx.match(f) for f in files):
                    errs.append(f"glob matches no tracked file: {g}")
    return errs


def slugify(sentence: str) -> str:
    stop = {"the", "a", "an", "to", "in", "of", "and", "or", "for", "this", "that", "always", "never",
            "use", "with", "on", "is", "be", "do", "not", "repo"}
    words = [w for w in re.findall(r"[a-z0-9]+", sentence.lower())]
    keep = [w for w in words if w not in stop] or words
    return "-".join(keep[:5])[:48].strip("-") or "rule"


def new_manual(sentence: str, path_globs: list[str] | None, who: str) -> dict:
    return {
        "schema": SCHEMA,
        "id": slugify(sentence),
        "status": "active",
        "kind": "negative_constraint" if re.search(r"(?i)\b(never|don't|do not|avoid|no )", sentence) else "invariant",
        "scope": {"path_globs": path_globs or [], "symbol_triggers": [], "agents": ["*"]},
        "rule": {"directive": sentence.strip(), "rationale": ""},
        "provenance": {"source_kind": "manual", "approved_by": who,
                       "created": time.strftime("%Y-%m-%d"), "created_at": round(time.time())},
        "enforcement": {"level": "instruct"},
    }


def save(slug: str, rule: dict) -> Path:
    if rule.get("_level") and rule["_level"] != "repo" and rule.get("_file"):
        f = Path(rule["_file"])  # an inherited rule is changed where it lives, not copied into the repo
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(yaml.safe_dump({k: v for k, v in rule.items() if not k.startswith("_")},
                                    sort_keys=False, allow_unicode=True))
        return f
    d = rules_dir(slug)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{rule['id']}.yaml"
    clean = {k: v for k, v in rule.items() if not k.startswith("_")}
    f.write_text(yaml.safe_dump(clean, sort_keys=False, allow_unicode=True))
    return f


def unique_id(slug: str, rid: str) -> str:
    existing = {r.get("id") for r in load_all(slug)} | {r.get("id") for r in inherited(slug)}
    if rid not in existing:
        return rid
    n = 2
    while f"{rid}-{n}" in existing:
        n += 1
    return f"{rid}-{n}"


def find(slug: str, prefix: str, levels: bool = True) -> dict | None:
    pool = all_levels(slug) if levels else load_all(slug)
    hits = [r for r in pool if str(r.get("id", "")).startswith(prefix)]
    exact = sorted([r for r in hits if r.get("id") == prefix], key=lambda r: r.get("status") == "deprecated")
    if exact:
        return exact[0]  # the live copy before a retired one with the same id
    ids = {r.get("id") for r in hits}
    if len(ids) == 1:
        return sorted(hits, key=lambda r: r.get("status") == "deprecated")[0]
    return None


def compile_index(slug: str) -> int:
    """Rebuild the hot-path index. Called after every rule change; never typed by a person."""
    out = []
    for r in all_levels(slug):
        if r.get("status") != "active" or "_error" in r:
            continue
        gl = (r.get("scope") or {}).get("path_globs") or []
        wide = globs.is_repo_wide(gl)
        enf = r.get("enforcement") or {}
        out.append({
            "id": r["id"],
            "text": (r.get("rule") or {}).get("directive", "").strip(),
            "why": ((r.get("rule") or {}).get("rationale") or "").strip(),
            "globs": gl,
            "regex": [] if wide else [globs.to_regex(g) for g in gl],
            "depth": 0 if wide else max(globs.depth(g) for g in gl),
            "repo_wide": wide,
            "source": (r.get("provenance") or {}).get("source_kind", ""),
            "detector": (enf.get("detector") or {}).get("pattern"),
            "level": enf.get("level", "instruct"),
            "scope_level": r.get("_level", "repo"),
            "agents": (r.get("scope") or {}).get("agents") or ["*"],
            "kind": r.get("kind"),
            "created": (r.get("provenance") or {}).get("created", ""),
        })
    extra = {}
    try:  # the ranking policy's score, so the hook orders rules without loading the policy
        from . import pipeline
        extra = pipeline.annotate_index(slug, out) or {}
    except Exception:
        pass
    # Suggestions waiting for a decision, so the hook and the status line can mention them without yaml.
    pending = [{"id": r["id"], "text": (r.get("rule") or {}).get("directive", "").strip(),
                "source": (r.get("provenance") or {}).get("source_kind", ""),
                "created": (r.get("provenance") or {}).get("created", "")}
               for r in load_all(slug, include_shared=False) if r.get("status") == "candidate" and "_error" not in r]
    f = paths.index_file(slug)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "built": time.time(), "slug": slug, "rules": out, "pending": pending, **extra}))
    os.replace(tmp, f)
    return len(out)


def compile_all() -> int:
    """Rebuild every known repo's index: a rule at the user, agent, stack or owner level reaches all of them."""
    n = 0
    for s in known_slugs():
        try:
            compile_index(s)
            n += 1
        except Exception:
            pass
    return n


def commit(message: str) -> bool:
    """Solo mode: a rule change is a commit in the memory repo. History and decision both stay."""
    m = paths.memory()
    m.mkdir(parents=True, exist_ok=True)
    try:
        if not (m / ".git").exists():
            subprocess.run(["git", "-C", str(m), "init", "-q"], check=True, timeout=10)
        subprocess.run(["git", "-C", str(m), "add", "-A"], check=True, timeout=10)
        r = subprocess.run(["git", "-C", str(m), "-c", "user.name=once", "-c",
                            "user.email=once@localhost", "commit", "-q", "-m", message],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False
