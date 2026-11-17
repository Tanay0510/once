"""`once demo`: the whole loop on a bundled toy repo, in about a minute, without touching your code or your rules.

A failing session, a flag, a suggested rule, your approval, a replay that proves it, and a passing rerun.
Everything happens in a temporary folder with its own once home; nothing is written to ~/.once or your repos.
By default a scripted agent and drafter stand in for the model, so it is free and the same every time.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ASSETS = Path(__file__).parent / "demo_assets"
PROMPT = "Add a French greeting to toy/core.py and make sure the tests pass"
SIGNATURE = r"fixture .* not found"

FILES = {
    "README.md": "# toy\n\nA tiny greeting library.\n",
    "toy/__init__.py": "",
    "toy/core.py": 'GREETINGS = {"en": "Hello"}\n\n\ndef greet(lang="en"):\n    return GREETINGS.get(lang, GREETINGS["en"])\n',
    "tests/__init__.py": "",
    "tests/test_core.py": ("import json\nimport os\nimport unittest\n\nfrom toy.core import greet\n\n\n"
                           "class T(unittest.TestCase):\n    def setUp(self):\n"
                           "        p = os.path.join(os.path.dirname(__file__), 'fixtures', 'greetings.json')\n"
                           "        if not os.path.exists(p):\n"
                           "            raise FileNotFoundError('fixture tests/fixtures/greetings.json not found')\n"
                           "        self.cases = json.load(open(p))\n\n"
                           "    def test_greet(self):\n        for lang, word in self.cases.items():\n"
                           "            self.assertEqual(greet(lang), word)\n"),
    # The planted convention: the fixtures only exist after `make check` generates them.
    "Makefile": ("check:\n\t@mkdir -p tests/fixtures\n"
                 "\t@echo '{\"en\": \"Hello\"}' > tests/fixtures/greetings.json\n"
                 "\t@python3 -m unittest -q\n"),
    ".gitignore": "tests/fixtures/\n",
}


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                          env={**os.environ, "GIT_AUTHOR_NAME": "once demo", "GIT_AUTHOR_EMAIL": "demo@localhost",
                               "GIT_COMMITTER_NAME": "once demo", "GIT_COMMITTER_EMAIL": "demo@localhost"})


def make_repo(dest: Path) -> Path:
    for rel, text in FILES.items():
        f = dest / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    _git(dest, "init", "-q")
    _git(dest, "remote", "add", "origin", "https://github.com/once-demo/toy.git")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-qm", "toy: greetings")
    return dest


def _session(root: Path, agent_cmd: str, budget: float) -> None:
    import shlex
    argv = [a.format(prompt=PROMPT, budget=budget) for a in shlex.split(agent_cmd)]
    subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL)


def _latest(con, slug):
    from . import store
    store.sync(con)
    rows = store.sessions(con, slug, 1)
    return rows[0] if rows else None


def run(real: bool = False, keep: bool = False, say=print, step_pause: float = 0.0) -> dict:
    """Run the loop. Returns a summary; prints as it goes through `say`."""
    tmp = Path(tempfile.mkdtemp(prefix="once-demo-"))
    saved = {k: os.environ.get(k) for k in ("ONCE_HOME", "ONCE_OLD_HOME", "ONCE_NOTIFY_LOG", "ONCE_SLUG", "ONCE_INDEX")}
    os.environ.update({"ONCE_HOME": str(tmp / "home"), "ONCE_OLD_HOME": str(tmp / "no-old-install"),
                       "ONCE_NOTIFY_LOG": str(tmp / "notifications.log")})
    for k in ("ONCE_SLUG", "ONCE_INDEX"):
        os.environ.pop(k, None)
    out = {"dir": str(tmp)}
    try:
        from . import adapters, cli, detectors, learn, paths, replay, repo, rules, store
        home = paths.home()
        home.mkdir(parents=True, exist_ok=True)
        # Quiet: no snapshots, notifications or digests from the demo's own sessions.
        (home / "config.json").write_text(json.dumps({"snapshots": False, "notifications": False, "digest": "off",
                                                      "ask_in_agent": False, "teach_by_talking": False}))
        root = make_repo(tmp / "toy")
        slug, _ = repo.slug(str(root))
        repo.remember_root(slug, root)
        (paths.repo_memory(slug) / "rules").mkdir(parents=True, exist_ok=True)
        rules.compile_index(slug)
        if real:
            agent_cmd, drafter_cmd = adapters.get("claude-code").HEADLESS, None
        else:
            agent_cmd = f'"{sys.executable}" "{ASSETS / "agent.py"}" {{prompt}}'
            drafter_cmd = f'"{sys.executable}" "{ASSETS / "drafter.py"}" {{prompt}}'
        budget = 0.5
        con = store.connect()

        def show(row):
            evs = store.events(con, row["id"])
            flags = detectors.detect(evs)
            for mark, num, kind, text in cli._timeline(evs, flags):
                say(f"   {mark} {num:>3}  {kind:<7} {text}")
            return evs, flags

        say(f"A toy repo with a planted convention: its tests only pass through `make check`.\n  {root}\n")
        say("1. The agent works on it, with no rules yet.")
        _session(root, agent_cmd, budget)
        row = _latest(con, slug)
        if row is None:
            say("   The session was not captured. Nothing else to show.")
            out["error"] = "not captured"
            return out
        evs, flags = show(row)
        out["before_steps"] = row["steps"]
        say(f"\n2. once flags it: {len(flags)} flagged range{'s' if len(flags) != 1 else ''}, "
            f"{round(100 * detectors.avoidable_ratio(evs, flags))}% of the steps were avoidable.")
        time.sleep(step_pause)

        say("\n3. once learn drafts a rule from the flagged steps.")
        st = learn.learn(slug, root, 3, drafter_cmd, budget, progress=lambda m: say("  " + m))
        cand = next((r for r in rules.load_all(slug) if r.get("status") == "candidate"), None)
        if not cand:
            say("   No rule was drafted, so the loop stops here.")
            out["error"] = "nothing drafted"
            return out
        say(f'   Suggested: "{cand["rule"]["directive"]}"')

        say("\n4. You approve it. (Normally in `once review`, or by answering your agent at the start of a session.)")
        learn.decide(slug, cand, "accept", "you (demo)")
        cli._refresh(slug, root)
        rule = rules.find(slug, cand["id"])

        say("\n5. Replay proves it: 3 runs without the rule and 3 with it, from the same commit.")
        res = replay.replay(slug, root, rule, PROMPT, "HEAD", 3, None, SIGNATURE, agent_cmd, budget,
                            progress=lambda m: None)
        if res["verdict"] == "error":
            say(f"   Could not replay: {res.get('error')}")
            out["error"] = res.get("error")
            return out
        ev = res["evidence"]
        lane = lambda n: " ".join("✕" if k < n else "✓" for k in range(ev["runs"]))
        say(f"   without the rule   {lane(ev['without_rule']['failed'])}")
        say(f"   with the rule      {lane(ev['with_rule']['failed'])}")
        say(f"   {replay.VERDICTS[res['verdict']]}")
        out["verdict"] = res["verdict"]

        say("\n6. The next session gets the rule at the start, and the mistake does not happen.")
        _git(root, "checkout", "-q", "--", ".")
        _git(root, "clean", "-qfdx")
        _session(root, agent_cmd, budget)
        row2 = _latest(con, slug)
        evs2, flags2 = show(row2)
        out["after_steps"] = row2["steps"]
        out["flags_after"] = len(flags2)
        say(f"\nBefore: {out['before_steps']} steps, {len(flags)} flagged.  After: {row2['steps']} steps, {len(flags2)} flagged.")
        say("The agent made that mistake once.")
        con.close()
        return out
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if keep:
            say(f"\nKept everything in {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
