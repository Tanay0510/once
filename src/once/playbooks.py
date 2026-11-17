"""Playbooks: how to do a recurring kind of task. Stored as markdown with a small header."""
import re
from pathlib import Path

import yaml

from . import paths

TEMPLATE = """---
id: {id}
description: {description}
scope: []
related_rules: []
---
1. First step.
2. Second step.
3. Verify: the command that proves it worked.
"""


def folder(slug: str) -> Path:
    return paths.repo_memory(slug) / "playbooks"


def load_all(slug: str) -> list[dict]:
    out = []
    d = folder(slug)
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.md")):
        text = f.read_text()
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
        try:
            head = yaml.safe_load(m.group(1)) if m else {}
        except Exception:
            head = {}
        head = head if isinstance(head, dict) else {}
        out.append({"id": str(head.get("id") or f.stem), "description": head.get("description", ""),
                    "scope": head.get("scope") or [], "body": (m.group(2) if m else text), "_file": str(f),
                    "draft": bool(head.get("draft"))})
    return out


def find(slug: str, name: str) -> dict | None:
    name = name.removeprefix("once-")
    hits = [p for p in load_all(slug) if p["id"].startswith(name)]
    exact = [p for p in hits if p["id"] == name]
    return exact[0] if exact else (hits[0] if len(hits) == 1 else None)


def new(slug: str, pid: str, description: str) -> Path:
    pid = re.sub(r"[^a-z0-9\-]+", "-", pid.lower()).strip("-")[:48] or "playbook"
    d = folder(slug)
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{pid}.md"
    if not f.exists():
        f.write_text(TEMPLATE.format(id=pid, description=description or "Use when ..."))
    return f
