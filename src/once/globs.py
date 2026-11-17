"""Glob -> regex, so the hook hot path needs only `re`."""
import re


def to_regex(glob: str) -> str:
    g = glob.strip().lstrip("./")
    out, i = [], 0
    while i < len(g):
        c = g[i]
        if g.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif g.startswith("**", i):
            out.append(".*")
            i += 2
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return "^" + "".join(out) + "$"


def depth(glob: str) -> int:
    """Specificity: literal path segments before the first wildcard."""
    n = 0
    for seg in glob.strip("/").split("/"):
        if "*" in seg or "?" in seg:
            break
        n += 1
    return n


def is_repo_wide(globs: list[str]) -> bool:
    return not globs or all(g.strip() in ("**", "**/*", "*") for g in globs)
