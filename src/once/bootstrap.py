"""Cold-start mining: first suggestions from a repo's own files and history, before any agent session runs.

No model and no untrusted text: everything here is read from config files the project already enforces.
Candidates go to review like any other (`source_kind: bootstrap`); none is active until you accept it.

Guards, from the plan:
  - the time split: in a repo with mined tasks, only history before the held-out cutoff is read
  - do not relearn what is documented: a candidate the repo's instruction files already state is dropped
  - cap the flood: at most 15, ranked by how often the underlying signal recurs
  - replay where possible: bootstrap candidates are marked unreplayed until a replay runs
"""
import json
import re
import subprocess
import time
from pathlib import Path

from . import SCHEMA, paths, repo, rules

CAP = 15
INSTRUCTION_FILES = ["CLAUDE.md", "AGENTS.md", "CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md",
                     ".cursorrules", ".github/copilot-instructions.md", "GEMINI.md"]
FIXUP = re.compile(r"(?i)\b(fix(es|ed)? (lint|linting|format(ting)?|style|types?|mypy|ruff|flake8|eslint|prettier|ci|tests?)"
                   r"|address(ed)? (review|comments)|apply suggestions|pre-commit|run (black|ruff|prettier))\b")


class Repo:
    """Reads files at one commit (the training cutoff) or the working tree."""

    def __init__(self, root: Path, at: str | None = None):
        self.root, self.at = root, at

    def read(self, rel: str) -> str | None:
        if self.at:
            r = subprocess.run(["git", "-C", str(self.root), "show", f"{self.at}:{rel}"], capture_output=True, text=True)
            return r.stdout if r.returncode == 0 else None
        f = self.root / rel
        try:
            return f.read_text(errors="ignore") if f.is_file() else None
        except Exception:
            return None

    def exists(self, rel: str) -> bool:
        return self.read(rel) is not None

    def ls(self) -> list[str]:
        args = ["ls-tree", "-r", "--name-only", self.at] if self.at else ["ls-files"]
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True).stdout.splitlines()

    def subjects(self, n: int = 2000) -> list[str]:
        r = subprocess.run(["git", "-C", str(self.root), "log", f"-{n}", "--format=%s", *( [self.at] if self.at else [])],
                           capture_output=True, text=True)
        return r.stdout.splitlines()


def _cutoff(slug: str) -> str | None:
    try:
        from . import tasks
        held = [t for t in tasks.load_all(slug) if t.get("split") == "heldout" and t.get("base_commit")]
        held.sort(key=lambda t: str(t.get("merged_at")))
        return held[0]["base_commit"] if held else None
    except Exception:
        return None


def _known(r: Repo) -> list[str]:
    """Sentences the repo already tells every agent: the do-not-relearn list."""
    out = []
    for f in INSTRUCTION_FILES:
        t = r.read(f)
        if t:
            out += [l.strip(" -*#>\t") for l in t.splitlines() if len(l.strip()) > 12]
    return out


def _words(t: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+", (t or "").lower()))


def _stated(directive: str, known: list[str]) -> str | None:
    w = _words(directive)
    key = set(re.findall(r"`([^`]+)`", directive))
    for line in known:
        lw = _words(line)
        if (w and lw and len(w & lw) / len(w | lw) >= 0.5) or (key and all(k in line for k in key)):
            return line
    return None


def _run(prefix: str, cmd: str) -> str:
    return f"{prefix}{cmd}" if prefix else cmd


def signals(r: Repo, slug: str) -> tuple[list[dict], list[str]]:
    """Every candidate the files support, with its evidence and how often the signal recurs. Plus findings."""
    files = set(r.ls())
    names = {Path(f).name for f in files}
    py = r.read("pyproject.toml") or ""
    pkg = {}
    try:
        pkg = json.loads(r.read("package.json") or "{}")
    except Exception:
        pass
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    scripts = pkg.get("scripts") or {}
    subjects = r.subjects()
    fix = [s for s in subjects if FIXUP.search(s)]
    count = lambda rx: sum(1 for s in fix if re.search(rx, s, re.I))
    uv = "uv.lock" in files
    poetry = "poetry.lock" in files
    prefix = "uv run " if uv else ("poetry run " if poetry else "")
    out, findings = [], []

    def add(directive, why, evidence, recur=1, kind="command"):
        out.append({"directive": directive, "rationale": why, "evidence": evidence, "recur": recur, "kind": kind})

    # 1. CI: the real check command. The CI-config reader already fills repo.yaml; this makes it a rule.
    check = repo.detect_check_command(r.root)  # CI config is not a held-out answer, so the working tree is fine
    if check:
        add(f"Before you finish, run `{check[0]}`; CI runs it on every pull request.",
            "The pull request fails CI when this does.", check[1], 1 + count(r"\b(ci|tests?)\b"))
    # 2. Package managers.
    if uv:
        add("Run Python tools through `uv run` (for example `uv run pytest`); never call bare pip or python.",
            "The project is managed with uv; a bare interpreter misses its environment.", "uv.lock")
    elif poetry:
        add("Run Python tools through `poetry run`; never call bare pip.", "The project is managed with poetry.", "poetry.lock")
    if "pnpm-lock.yaml" in files:
        add("Use pnpm, not npm or yarn: `pnpm install`, `pnpm run <script>`.", "The lockfile is pnpm's.", "pnpm-lock.yaml")
    elif "yarn.lock" in files:
        add("Use yarn, not npm: `yarn install`, `yarn <script>`.", "The lockfile is yarn's.", "yarn.lock")
    # 3. Lint, format and type-check configs: conventions the project already enforces.
    if "[tool.ruff" in py or {"ruff.toml", ".ruff.toml"} & names:
        add(f"Run `{_run(prefix, 'ruff check')}` and `{_run(prefix, 'ruff format')}` on the files you changed before finishing.",
            "ruff is configured in this repo and its CI or pre-commit enforces it.",
            "pyproject.toml [tool.ruff]" if "[tool.ruff" in py else "ruff.toml", 1 + count(r"ruff|lint|format|style"))
    elif "[tool.black" in py:
        add(f"Format Python with `{_run(prefix, 'black')}` before finishing.", "black is configured in pyproject.toml.",
            "pyproject.toml [tool.black]", 1 + count(r"black|format|style"))
    if "[tool.mypy" in py or "mypy.ini" in names:
        add(f"Run `{_run(prefix, 'mypy')}` after changing typed code; type errors fail CI.", "mypy is configured.",
            "mypy config", 1 + count(r"mypy|types?"))
    if any(n.startswith(".prettierrc") for n in names) or "prettier" in deps:
        add("Format changed files with prettier before finishing.", "prettier is configured.", "prettier config",
            1 + count(r"prettier|format|style"))
    if any(n.startswith(".eslintrc") or n.startswith("eslint.config") for n in names) or "eslint" in deps:
        cmd = "npm run lint" if "lint" in scripts else "eslint"
        add(f"Run `{cmd}` on the files you changed; lint errors fail CI.", "eslint is configured.", "eslint config",
            1 + count(r"eslint|lint"))
    if "Cargo.toml" in files:
        add("Run `cargo fmt` and `cargo clippy` before finishing.", "rustfmt and clippy are the Rust project's norms.",
            "Cargo.toml", 1 + count(r"fmt|clippy|lint|format"))
    if "go.mod" in files:
        add("Run `gofmt -w` on changed files and `go vet ./...` before finishing.", "Go projects are checked with gofmt and vet.",
            "go.mod", 1 + count(r"fmt|vet|lint"))
    if ".pre-commit-config.yaml" in files:
        add(f"Run `{_run(prefix, 'pre-commit run --files <changed files>')}` before finishing.",
            "The repo's pre-commit hooks reject what they find.", ".pre-commit-config.yaml",
            1 + count(r"pre-commit|lint|format"))
    # 4. Doc drift: the contributing guide names one test command, CI runs another.
    guide = r.read("CONTRIBUTING.md") or r.read(".github/CONTRIBUTING.md") or ""
    if guide and check:
        named = re.findall(r"`((?:uv run |poetry run )?(?:pytest|tox|nox|make \w+|npm (?:run )?test|cargo test|go test)[^`]*)`", guide)
        if named and not any(check[0].split()[0] in n or n.split()[0] in check[0] for n in named):
            findings.append(f"Doc drift: CONTRIBUTING names `{named[0]}` but CI runs `{check[0]}` ({check[1]}).")
    if fix:
        findings.append(f"{len(fix)} fix-up commits in recent history (e.g. \"{fix[0][:60]}\"): first attempts trip on these.")
    return out, findings


def run(slug: str, root: Path, dry_run: bool = False) -> dict:
    cut = _cutoff(slug)
    r = Repo(root, cut)
    cands, findings = signals(r, slug)
    known = _known(r)
    cands.sort(key=lambda c: -c["recur"])
    kept, dropped = [], []
    for c in cands:
        stated = _stated(c["directive"], known)
        dup = None
        if not stated:
            from . import learn
            dup = learn.duplicate_of(slug, c["directive"])
        if stated or dup:
            dropped.append({**c, "why": f"already in the repo's instructions: {stated[:70]}" if stated else f"duplicate of {dup}"})
            continue
        kept.append(c)
    kept = kept[:CAP]
    written = []
    if not dry_run:
        from . import learn
        for c in kept:
            rule = {"schema": SCHEMA, "id": rules.unique_id(slug, rules.slugify(c["directive"])), "status": "candidate",
                    "kind": c["kind"], "scope": {"path_globs": [], "symbol_triggers": [], "agents": ["*"]},
                    "rule": {"directive": c["directive"], "rationale": c["rationale"]},
                    "provenance": {"source_kind": "bootstrap", "evidence_file": c["evidence"], "recurrence": c["recur"],
                                   "created": time.strftime("%Y-%m-%d"), "read_at": cut or "working tree",
                                   "replayed": False},
                    "enforcement": {"level": "instruct"}}
            if rules.lint(rule, root):
                continue
            rules.save(slug, rule)
            written.append(rule["id"])
            learn.log({"episode": f"bootstrap:{rule['id']}", "source_kind": "bootstrap", "repo": slug,
                       "candidate_id": rule["id"], "outcome": "queued", "draft_text": c["directive"], "lint": "pass",
                       "features": learn.rule_features(rule), "draft_cost": 0.0})
        if written:
            rules.commit(f"bootstrap {slug}: {len(written)} candidates")
        paths.repo_memory(slug).mkdir(parents=True, exist_ok=True)
        (paths.repo_memory(slug) / "known.json").write_text(json.dumps(known[:400]))
    return {"cutoff": cut, "candidates": kept, "written": written, "dropped": dropped, "findings": findings}
