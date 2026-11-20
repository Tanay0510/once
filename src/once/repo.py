"""Repo identity: a fork and a fresh clone of the same project share one memory folder."""
import re
import subprocess
from pathlib import Path


def _git(cwd, *args) -> str:
    try:
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def root(cwd) -> Path | None:
    p = Path(cwd).resolve()
    for d in [p, *p.parents]:
        if (d / ".git").exists():
            return d
    return None


def normalize_remote(url: str) -> str:
    """git@github.com:Owner/Repo.git and https://github.com/Owner/Repo -> github.com/owner/repo"""
    u = url.strip()
    u = re.sub(r"^[a-z+]+://", "", u)
    u = re.sub(r"^[^@/]+@", "", u)
    u = u.replace(":", "/", 1) if re.match(r"^[^/]+:[^/0-9]", u) else u
    u = re.sub(r"\.git$", "", u).strip("/")
    return u.lower()


def slug(cwd) -> tuple[str, Path | None]:
    r = root(cwd)
    if r is None:
        return "local/" + Path(cwd).resolve().name.lower(), None
    url = _git(r, "config", "--get", "remote.upstream.url") or _git(r, "config", "--get", "remote.origin.url")
    if url:
        return normalize_remote(url), r
    return "local/" + r.name.lower(), r


def head(cwd) -> str:
    return _git(cwd, "rev-parse", "--short", "HEAD")


def branch(cwd) -> str:
    """The checked-out branch, or "" when HEAD is detached."""
    b = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    return "" if b == "HEAD" else b


def ensure_excluded(repo_root: Path, line: str) -> bool:
    """Ignore generated files through .git/info/exclude: no change to the project's .gitignore."""
    info = repo_root / ".git" / "info"
    if not (repo_root / ".git").is_dir():
        return False  # worktree or submodule: leave it alone
    info.mkdir(exist_ok=True)
    f = info / "exclude"
    cur = f.read_text() if f.exists() else ""
    if line in cur.splitlines():
        return False
    f.write_text(cur + ("" if cur.endswith("\n") or not cur else "\n") + line + "\n")
    return True


_CHECKS = [
    (r"\b(uv run )?pytest\b[^\n]*", "pytest -q"),
    (r"\bcargo test\b[^\n]*", "cargo test"),
    (r"\bgo test\b[^\n]*", "go test ./..."),
    (r"\b(npm|pnpm|yarn) (run )?test\b[^\n]*", None),
    (r"\bmake test\b", "make test"),
]


def detect_check_command(repo_root: Path) -> tuple[str, str] | None:
    """Read the real check command from CI config. Returns (command, source file)."""
    wf = repo_root / ".github" / "workflows"
    files = sorted(wf.glob("*.y*ml")) if wf.is_dir() else []
    for f in files:
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            s = line.strip()
            if not s.startswith(("run:", "- run:")) and "run:" not in s:
                continue
            cmd = s.split("run:", 1)[1].strip().strip("|>").strip()
            for pat, default in _CHECKS:
                m = re.search(pat, cmd)
                if m:
                    return (m.group(0).strip() if default is None or len(m.group(0)) < 60 else default,
                            str(f.relative_to(repo_root)))
    return None


def detect_stack(repo_root: Path) -> str:
    marks = [("uv.lock", "Python, uv"), ("poetry.lock", "Python, poetry"), ("pyproject.toml", "Python"),
             ("Cargo.toml", "Rust"), ("go.mod", "Go"), ("pnpm-lock.yaml", "Node, pnpm"),
             ("package.json", "Node"), ("pom.xml", "Java, Maven"), ("build.gradle", "JVM, Gradle")]
    for f, name in marks:
        if (repo_root / f).exists():
            return name
    return "unknown"


STACK_MARKS = [("uv.lock", ["python", "uv"]), ("poetry.lock", ["python", "poetry"]), ("pyproject.toml", ["python"]),
               ("requirements.txt", ["python"]), ("Cargo.toml", ["rust"]), ("go.mod", ["go"]),
               ("pnpm-lock.yaml", ["node", "pnpm"]), ("yarn.lock", ["node", "yarn"]), ("package.json", ["node"]),
               ("pom.xml", ["java", "maven"]), ("build.gradle", ["jvm", "gradle"]), ("Gemfile", ["ruby"])]


def stacks(repo_root) -> list[str]:
    """Toolchain slugs from marker files, most general first: ['python', 'uv']."""
    out = []
    if not repo_root:
        return out
    for f, names in STACK_MARKS:
        if (Path(repo_root) / f).exists():
            out += [n for n in names if n not in out]
    return out


def remember_root(slug: str, root) -> None:
    """Where each repo lives on this machine, so rule files can be regenerated from anywhere."""
    import json
    from . import paths
    f = paths.local() / "roots.json"
    try:
        cur = json.loads(f.read_text())
    except Exception:
        cur = {}
    if root and cur.get(slug) != str(root):
        cur[slug] = str(root)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(cur))


def known_root(slug: str):
    import json
    from . import paths
    try:
        r = json.loads((paths.local() / "roots.json").read_text()).get(slug)
        return Path(r) if r and Path(r).is_dir() else None
    except Exception:
        return None
