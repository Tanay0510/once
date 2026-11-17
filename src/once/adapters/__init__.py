"""One adapter per agent. Everything outside this package is agent-neutral."""


def get(name: str):
    if name in ("claude-code", "claude"):
        from . import claude_code
        return claude_code
    if name == "codex":
        from . import codex
        return codex
    if name in ("antigravity", "agy"):
        from . import antigravity
        return antigravity
    raise KeyError(f"no adapter for agent {name!r}")


KNOWN = {"claude-code": "claude", "codex": "codex", "antigravity": "agy"}  # adapter name -> binary
IMPLEMENTED = ("claude-code", "codex", "antigravity")
