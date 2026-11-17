"""Tree snapshots into a private ref, without touching the user's index or working tree.

Run detached from the hook (`python -m once.snapshot <root> <session> start|end`) so a large
repo never delays a session.
"""
import os
import subprocess
import sys
import tempfile


def ref(session_id: str, which: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id)[:80]
    return f"refs/once/{safe}/{which}"


def take(root: str, session_id: str, which: str) -> str | None:
    fd, idx = tempfile.mkstemp(prefix="once-index-")
    os.close(fd)
    os.unlink(idx)
    env = {**os.environ, "GIT_INDEX_FILE": idx, "GIT_AUTHOR_NAME": "once", "GIT_AUTHOR_EMAIL": "once@localhost",
           "GIT_COMMITTER_NAME": "once", "GIT_COMMITTER_EMAIL": "once@localhost"}

    def git(*a):
        return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, env=env, timeout=120)
    try:
        head = git("rev-parse", "-q", "--verify", "HEAD").stdout.strip()
        if head:
            git("read-tree", head)
        git("add", "-A")  # respects .gitignore; writes only to the temporary index
        tree = git("write-tree").stdout.strip()
        if not tree:
            return None
        args = ["commit-tree", tree, "-m", f"once {which} snapshot"] + (["-p", head] if head else [])
        commit = git(*args).stdout.strip()
        if commit:
            git("update-ref", ref(session_id, which), commit)
        return commit or None
    except Exception:
        return None
    finally:
        try:
            os.unlink(idx)
        except OSError:
            pass


def changed(root: str, session_id: str) -> list[str] | None:
    """Files that differ between the start and end snapshots of a session."""
    r = subprocess.run(["git", "-C", root, "diff", "--name-only", ref(session_id, "start"), ref(session_id, "end")],
                       capture_output=True, text=True)
    return [l for l in r.stdout.splitlines() if l.strip()] if r.returncode == 0 else None


def spawn(root: str, session_id: str, which: str):
    try:
        subprocess.Popen([sys.executable, "-m", "once.snapshot", root, session_id, which],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception:
        pass


if __name__ == "__main__":
    if len(sys.argv) == 4:
        take(sys.argv[1], sys.argv[2], sys.argv[3])
