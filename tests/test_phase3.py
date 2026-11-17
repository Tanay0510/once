import json
import sys
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from conftest import payloads, run_hook
from once import cli, detectors, experiment as ex, learn, paths, rules, store, tasks, ui
from once.adapters import claude_code as cc
from test_tasks import SLUG, _setup

HERE = Path(__file__).parent
DRAFTER = f"{sys.executable} {HERE / 'fake_drafter.py'} {{prompt}}"
FIXER = f"{sys.executable} {HERE / 'fake_fixer.py'} {{prompt}}"


def test_more_detectors():
    def ev(seq, tool, target="a.py", **kw):
        return {"seq": seq, "event": "tool_result", "tool": tool, "target": target, "ok": True, **kw}
    evs = [{"seq": 0, "event": "user_prompt", "prompt": "p"},
           ev(1, "edit", edit_sig={"old": "A", "new": "B"}), ev(2, "shell", "make"),
           ev(3, "edit", edit_sig={"old": "B", "new": "A"})]
    assert [f["detector"] for f in detectors.detect(evs)] == ["reverted_edit"]
    evs = [{"seq": 0, "event": "user_prompt", "prompt": "p"}] + [ev(i, "search", f"q{i}") for i in range(1, 10)] + [ev(10, "edit")]
    fl = detectors.detect(evs)
    assert fl[0]["detector"] == "wandering_search" and (fl[0]["step_from"], fl[0]["step_to"]) == (2, 10)


def test_usage_and_end_line(env, tmp_path):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    assert (env["settings"].parent / "commands" / "once-teach.md").exists()
    tr = tmp_path / "t.jsonl"
    tr.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {"type": "assistant", "message": {"model": "m1", "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}}},
        {"type": "assistant", "message": {"model": "m1", "usage": {"input_tokens": 3, "output_tokens": 7, "cache_creation_input_tokens": 20}}},
    ]))
    ps = payloads(repo, "u1")
    for p in ps[:-1]:
        run_hook(p)
    out = run_hook({**ps[-1], "transcript_path": str(tr)}).stdout
    assert "1 avoidable pattern (retry cluster). Details:  once last" in json.loads(out)["systemMessage"]
    con = store.connect()
    store.sync(con)
    end = [e for e in store.events(con, "u1") if e.get("event") == "session_end"][0]
    assert end["usage"] == {"input_tokens": 13, "output_tokens": 12, "cache_creation_input_tokens": 20,
                            "cache_read_input_tokens": 100, "model_calls": 2, "models": ["m1"]}
    ps2 = payloads(repo, "u2")  # a clean session ends silently
    for p in (ps2[0], ps2[1], ps2[-1]):
        last = run_hook(p).stdout
    assert last == ""
    cli.main(["uninstall"])
    assert not (env["settings"].parent / "commands" / "once-teach.md").exists()


def test_learn_and_review(env, monkeypatch, capsys):
    repo = _setup(env, monkeypatch)
    # Episode 1: a human tag on a real session. Episode 2: a retry cluster nobody tagged.
    for p in payloads(repo, "L1"):
        run_hook(p)
    cli.main(["tag", "4-8", "missing", "use the Utc type"])
    for p in payloads(repo, "L2"):
        run_hook(p)
    # Episode 3: the same file missed in two lab runs. Episode 4: review comments on a train task.
    cli.main(["lab", "tasks", "mine"])
    cli.main(["lab", "tasks", "validate", "10"])
    for sid, task in (("M1", "acme-api-10"), ("M2", "acme-api-99")):
        for p in payloads(repo, sid)[:4]:
            run_hook(p)
        from once import hook
        hook._append(SLUG, sid, {"session_id": sid, "repo": SLUG, "event": "outcome", "verified": True,
                                 "missed_files": ["CHANGES.md"], "task": task, "ts": 1})
    t = tasks.find(SLUG, "10")
    t.update({"split": "train", "review_notes": ["calc.py: please keep this consistent with the other helpers in this module"]})
    tasks.save(SLUG, t)
    capsys.readouterr()

    eps = learn.episodes(SLUG)
    assert [e["source"] for e in eps] == ["human_tag", "missed_file", "pr_review", "retry_cluster"]  # by priority
    assert cli.main(["learn", "--dry-run"]) == 0 and "4 new episodes" in capsys.readouterr().out

    assert cli.main(["learn", "--drafter-cmd", DRAFTER, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "2 suggested, 1 skipped, 1 duplicates" in out  # the retry cluster drafts the same rule as the tag
    cands = [r for r in rules.load_all(SLUG) if r["status"] == "candidate"]
    assert len(cands) == 2 and all(r["provenance"]["drafted_by"] == "p0" for r in cands)
    by_src = {r["provenance"]["source_kind"]: r for r in cands}
    assert "CHANGES.md" in by_src["missed_file"]["rule"]["directive"]
    assert by_src["human_tag"]["scope"]["path_globs"] == ["crates/auth/**/*.rs"]
    assert by_src["human_tag"]["provenance"]["replay_hint"]["prompt"].startswith("Fix the timezone bug")
    # Candidates are never given to agents as rules; the agent is only asked to put them to the developer.
    ctx = json.loads(run_hook(payloads(repo, "after")[0]).stdout)["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("<once-review>") and "<repo-conventions" not in ctx
    assert not (repo / ".claude/rules/once").exists()
    assert cli.main(["learn"]) == 0 and "Nothing new" in capsys.readouterr().out  # idempotent

    cli.main([])
    assert "once review    2 suggested rules waiting" in capsys.readouterr().out
    cli.main(["review", "--list"])
    assert "Suggestion 1 of 2" in capsys.readouterr().out
    assert cli.main(["review", "--accept", by_src["human_tag"]["id"], "--text", "Use chrono Utc types in the auth crate."]) == 0
    assert cli.main(["review", "--reject", by_src["missed_file"]["id"], "--reason", "we have no changelog"]) == 0
    act = rules.active(SLUG)
    assert [r["rule"]["directive"] for r in act] == ["Use chrono Utc types in the auth crate."]
    assert (repo / ".claude/rules/once").is_dir()  # accepted and path-scoped: now emitted
    recs = [r for r in learn.history() if r.get("decision")]
    assert recs[0]["decision"] == "accepted_with_edit" and 0 < recs[0]["edit_ratio"] < 1
    assert recs[1]["decision"] == "rejected" and recs[1]["reject_reason"] == "we have no changelog"
    assert "REJECTED (we have no changelog)" in learn._examples() and "ACCEPTED: Use chrono Utc" in learn._examples()

    st = learn.stats()
    assert st["human_tag"]["acceptance"] == 1.0 and st["human_tag"]["as_written"] == 0.0
    assert st["missed_file"]["acceptance"] == 0.0 and st["pr_review"]["skipped"] == 1 and st["retry_cluster"]["duplicate"] == 1
    capsys.readouterr()
    cli.main(["lab", "stats"])
    assert "human_tag" in capsys.readouterr().out

    assert cli.main(["undo"]) == 0  # the rejection
    assert rules.find(SLUG, by_src["missed_file"]["id"])["status"] == "candidate"


def test_drafted_injection_is_stopped_by_lint(env, monkeypatch, capsys):
    repo = _setup(env, monkeypatch)
    for p in payloads(repo, "X1"):
        run_hook(p)
    cli.main(["tag", "4-8", "wrong", "INJECT"])
    capsys.readouterr()
    cli.main(["learn", "--drafter-cmd", DRAFTER, "--yes"])
    assert "rejected by lint" in capsys.readouterr().out
    assert not [r for r in rules.load_all(SLUG) if r["status"] == "candidate"]


def test_sweep_and_experiment(env, monkeypatch, capsys):
    repo = _setup(env, monkeypatch)
    cli.main(["lab", "tasks", "mine"])
    cli.main(["lab", "tasks", "validate", "10"])
    t = tasks.find(SLUG, "10")
    monkeypatch.setenv("FAKE_FIX", "ctx")  # the stand-in agent fixes the bug only when it was given context
    capsys.readouterr()

    t["split"] = "train"
    tasks.save(SLUG, t)
    assert cli.main(["lab", "sweep", "--agent-cmd", FIXER, "--yes"]) == 0
    out = capsys.readouterr().out
    assert "bare" in out and "0% (0-" in out and "failed tests" in out and "once learn" in out

    t["split"] = "heldout"
    tasks.save(SLUG, t)
    assert cli.main(["lab", "exp", "run", "--arms", "bare,rules,generated", "--agent-cmd", FIXER, "--yes"]) == 1
    assert "generated baseline is not prepared" in capsys.readouterr().out
    d = ex.baseline_dir(SLUG)
    d.mkdir(parents=True)
    (d / "generated.md").write_text("# Notes\nRun the tests.\n")
    cli.main(["teach", "keep functions pure"])
    capsys.readouterr()

    assert cli.main(["lab", "exp", "run", "--arms", "bare,rules,generated", "--repeats", "2", "--agent-cmd", FIXER,
                     "--max-cost", "0.05", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "1 heldout tasks x 3 arms" in out and "reached the spend cap" in out and "lab exp resume" in out
    m, rows = ex.load(None)
    assert 0 < len(rows) < 6
    assert cli.main(["lab", "exp", "resume", "--agent-cmd", FIXER]) == 0
    m, rows = ex.load(m["id"])
    assert len(rows) == 6 and {(r["arm"], r["repeat"]) for r in rows} == {(a, r) for a in ("bare", "rules", "generated") for r in (1, 2)}
    sm = ex.summarize(m, rows)
    assert sm["arms"]["bare"]["rate"] == 0.0 and sm["arms"]["rules"]["rate"] == 1.0 and sm["arms"]["generated"]["rate"] == 1.0
    assert sm["pairs"][0] == {"arm": "rules", "against": "bare", "better": 1, "worse": 0, "same": 0, "p": 1.0,
                              "steps_diff": sm["pairs"][0]["steps_diff"]}
    assert all("CLAUDE.md" not in r["changed"] for r in rows)  # baseline files are not counted as the agent's work
    assert sm["arms"]["rules"]["cost_per_verified"] == 0.02
    out = capsys.readouterr().out
    assert "rules against bare" in out and "100% (" in out

    # A changed environment refuses to mix results.
    man = ex.exp_dir(m["id"]) / "manifest.json"
    mm = json.loads(man.read_text())
    mm["repeats"], mm["fingerprint"]["agent"] = 3, "0.0.0 (old)"
    man.write_text(json.dumps(mm))
    cli.main(["lab", "exp", "resume", m["id"], "--agent-cmd", FIXER])
    assert "environment changed" in capsys.readouterr().out and len(ex.load(m["id"])[1]) == 6

    # Outcomes are on the sessions, and lab sessions show them.
    con = store.connect()
    store.sync(con)
    ocs = [e for r in store.sessions(con, SLUG, 50) for e in store.events(con, r["id"]) if e.get("event") == "outcome"]
    assert len(ocs) == 7 and {o["arm"] for o in ocs} == {"bare", "rules", "generated"}
    assert ex.wilson(0, 0) == (0.0, 0.0) and 0.2 < ex.wilson(5, 10)[0] < 0.5 < ex.wilson(5, 10)[1] < 0.8
    assert ex.sign_test(8, 0) < 0.01 and ex.sign_test(3, 3) == 1.0

    cli.main(["lab", "cochange"])
    assert capsys.readouterr().out  # runs on any repo without error


def test_review_in_viewer(env, monkeypatch):
    repo = _setup(env, monkeypatch)
    for p in payloads(repo, "V1"):
        run_hook(p)
    cli.main(["tag", "4-8", "missing", "use the Utc type"])
    learn.learn(SLUG, repo, drafter_cmd=DRAFTER, progress=lambda *_: None)
    rid = [r for r in rules.load_all(SLUG) if r["status"] == "candidate"][0]["id"]
    srv = ui.serve(SLUG, lambda s: cli._refresh(s), 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + f"/review?repo={SLUG}", timeout=5).read().decode()
        assert rid in page and "Accept" in page and "Reject" in page
        data = urllib.parse.urlencode({"token": ui.TOKEN, "slug": SLUG, "id": rid, "decision": "accept", "text": ""}).encode()
        assert urllib.request.urlopen(urllib.request.Request(base + "/review", data=data), timeout=5).status == 200
        assert rules.find(SLUG, rid)["status"] == "active"
        assert "No suggested rules waiting" in urllib.request.urlopen(base + f"/review?repo={SLUG}", timeout=5).read().decode()
    finally:
        srv.shutdown()


def test_agent_that_does_no_work_is_not_recorded_as_a_failure(env, monkeypatch, capsys):
    """A usage limit or login problem must stop the sweep with the reason, not log eight failed tasks."""
    repo = _setup(env, monkeypatch)
    cli.main(["lab", "tasks", "mine"])
    cli.main(["lab", "tasks", "validate", "10"])
    t = tasks.find(SLUG, "10")
    t["split"] = "train"
    tasks.save(SLUG, t)
    dead = f"{sys.executable} {HERE / 'dead_agent.py'} {{prompt}}"
    capsys.readouterr()
    cli.main(["lab", "sweep", "--agent-cmd", dead, "--yes"])
    out = capsys.readouterr().out
    assert "Usage limit reached" in out and "Stopped" in out and "left out" in out
    assert "worth a look" not in out and "0% (" not in out
    con = store.connect()
    store.sync(con)
    assert not [e for r in store.sessions(con, SLUG, 50) for e in store.events(con, r["id"]) if e.get("event") == "outcome"]
    assert cli.main(["lab", "run", "10", "--agent-cmd", dead, "--yes"]) == 1
    assert "did no work on this task: Usage limit reached" in capsys.readouterr().out

    # The drafter failing is reported with its reason, and the episode is offered again later.
    for p in payloads(repo, "D1"):
        run_hook(p)
    cli.main(["tag", "4-8", "missing", "use the Utc type"])
    capsys.readouterr()
    cli.main(["learn", "--drafter-cmd", dead, "--yes"])
    out = capsys.readouterr().out
    assert "drafter failed: Usage limit reached" in out and "offered again" in out
    assert len(learn.episodes(SLUG)) == 1
    cli.main(["learn", "--drafter-cmd", DRAFTER, "--yes"])
    assert "1 suggested" in capsys.readouterr().out
