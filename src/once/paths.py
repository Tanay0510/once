"""Where everything lives. Kept import-light: the hook loads this on every call."""
import os
from pathlib import Path


def home() -> Path:
    return Path(os.environ.get("ONCE_HOME") or Path.home() / ".once")


def local() -> Path:
    return home() / "local"


def memory() -> Path:
    """The overlay store: one git repo the user owns."""
    return home() / "memory"


def repo_memory(slug: str) -> Path:
    return memory() / slug


def trajectories(slug: str) -> Path:
    return local() / "trajectories" / slug


def session_state(session_id: str) -> Path:
    return local() / "sessions" / f"{_safe(session_id)}.json"


def index_file(slug: str) -> Path:
    return local() / "index" / (slug.replace("/", "__") + ".json")


def db() -> Path:
    return local() / "state.db"


def error_log() -> Path:
    return local() / "hook-errors.log"


def off_flag(slug: str | None = None) -> Path:
    name = "GLOBAL" if slug is None else slug.replace("/", "__")
    return local() / "off" / name


def capture_off_flag(slug: str | None = None) -> Path:
    """Present when sessions in this repo (or everywhere) are not recorded at all."""
    name = "GLOBAL" if slug is None else slug.replace("/", "__")
    return local() / "off" / f"{name}.capture"


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in s)[:120]
