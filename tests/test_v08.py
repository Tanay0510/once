"""0.8: everything the design doc described that 0.7 lacked. Demo, the full CLI, scope levels, enforcement levels,
cold-start mining, cost accounting, the meta-learning outer loop, and the dashboard's trend, keys and stream."""
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from conftest import payloads, run_hook
from once import cli, cost, detectors, index, learn, meta, paths, pipeline, rules, store, ui

SLUG = "github.com/acme/api"
HERE = Path(__file__).parent
DRAFTER = f"{sys.executable} {HERE / 'fake_drafter.py'} {{prompt}}"


def _session(repo, sid, upto=None):
    for p in payloads(repo, sid)[:upto]:
        run_hook(p)


def _hook(repo, sid, event, **kw):
    r = run_hook({"session_id": sid, "cwd": str(repo), "hook_event_name": event, **kw})
    return json.loads(r.stdout) if r.stdout.strip() else None


def _enforce(repo, sid, **kw):
    r = subprocess.run([sys.executable, "-m", "once.hook", "claude-code", "enforce"], text=True, capture_output=True,
                       input=json.dumps({"session_id": sid, "cwd": str(repo), "hook_event_name": "PostToolUse", **kw}))
    return json.loads(r.stdout) if r.stdout.strip() else None


# ---------------------------------------------------------------- demo and CLI hygiene

def test_demo_runs_the_whole_loop_without_touching_anything(env, capsys):
    assert cli.main(["demo", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "passes" and out["flags_after"] == 0 and out["after_steps"] < out["before_steps"]
    assert not Path(out["dir"]).exists()  # cleaned up
    assert not paths.memory().exists() and not (paths.local() / "trajectories").exists()  # the user's home untouched


def test_unknown_flags_are_refused_and_flags_go_anywhere(env, capsys):
    cli.main(["init", "--yes"])
    assert cli.main(["off", "--bogus"]) == 2
    assert not paths.off_flag(SLUG).exists()  # it used to pause the whole repo
    assert cli.main(["rules", "show"]) == 1 and "Name a rule" in capsys.readouterr().out
    capsys.readouterr()
    assert cli.main(["--quiet", "status"]) == 0 and capsys.readouterr().out == ""
    assert cli.main(["--json", "status"]) == 0 and json.loads(capsys.readouterr().out)["repo"] == SLUG
    assert cli.main(["teach", "--dry-run", "never commit secrets"]) == 0
    assert "Would add" in capsys.readouterr().out and not rules.load_all(SLUG)
    assert cli.main(["uninstall", "--dry-run"]) == 0 and all(cli.adapters.get("claude-code").installed().values())


def test_off_for_one_session_and_off_for_capture(env):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    cli.main(["teach", "--path", "crates/auth/**/*.rs", "use chrono DateTime with Utc; never SystemTime"])
    _hook(repo, "o1", "SessionStart", source="startup")
    assert cli.main(["off", "--session"]) == 0
    assert json.loads(paths.session_state("o1").read_text())["paused"] is True
    pre = {"tool_name": "Bash", "tool_input": {"command": "sed -i '' 's/a/b/' crates/auth/src/jwt.rs"}}
    assert _hook(repo, "o1", "PreToolUse", **pre) is None  # paused: no path rule for this session
    assert not paths.off_flag(SLUG).exists()  # other sessions unaffected
    assert cli.main(["on", "--session"]) == 0
    assert "SystemTime" in _hook(repo, "o1", "PreToolUse", **pre)["hookSpecificOutput"]["additionalContext"]

    assert cli.main(["off", "--capture"]) == 0
    _session(repo, "o2")
    assert not (paths.trajectories(SLUG) / "o2.jsonl").exists()  # nothing recorded
    assert cli.main(["on"]) == 0
    _session(repo, "o3")
    assert (paths.trajectories(SLUG) / "o3.jsonl").exists()


def test_undo_reverses_a_tag_and_purge_deletes_sessions(env, capsys):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    _session(repo, "u1")
    assert cli.main(["tag", "4-8", "avoidable", "read the test first"]) == 0
    con = store.connect()
    store.sync(con)
    assert store.sessions(con, SLUG)[0]["tags"] == 1 and learn.episodes(SLUG)[0]["source"] == "human_tag"
    assert cli.main(["undo"]) == 0 and "Removed the tag on steps 4-8" in capsys.readouterr().out
    store.sync(con)
    assert store.sessions(con, SLUG)[0]["tags"] == 0
    assert all(e["source"] != "human_tag" for e in learn.episodes(SLUG))

    _session(repo, "u2")
    assert cli.main(["purge", "--older-than", "1", "--yes"]) == 0  # nothing that old
    assert len(store.sessions(store.connect(), SLUG)) == 2
    assert cli.main(["purge", "--dry-run"]) == 0 and len(store.sessions(store.connect(), SLUG)) == 2
    assert cli.main(["purge", "--yes"]) == 0
    con = store.connect()
    store.sync(con)
    assert store.sessions(con, SLUG) == [] and not list(paths.trajectories(SLUG).glob("*.jsonl"))


def test_last_web_and_completion(env, capsys, monkeypatch):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    _session(repo, "c1")
    from once import ambient
    monkeypatch.setattr(ambient, "ensure_ui", lambda port, cwd: f"http://127.0.0.1:{port}")
    capsys.readouterr()
    assert cli.main(["last", "--web", "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["url"].endswith("/session/c1")
    cli.main(["teach", "always run cargo fmt before finishing"])
    capsys.readouterr()
    assert cli.main(["completion", "zsh"]) == 0 and "compdef _once once" in capsys.readouterr().out
    cli.main(["_complete", "rules", "show"])
    assert "run-cargo-fmt-before-finishing" in capsys.readouterr().out.split()
    cli.main(["_complete", "rules", "promote", "x"])
    assert capsys.readouterr().out.split() == ["remind", "check", "block"]


# ---------------------------------------------------------------- scope levels

def test_scope_levels_inherit_with_precedence_and_a_separate_budget(env, capsys):
    repo = env["repo"]
    (repo / "Cargo.toml").write_text("[package]\nname='api'\n")
    cli.main(["init", "--yes"])
    assert cli.main(["teach", "--level", "user", "run the narrowest test first"]) == 0
    assert cli.main(["teach", "--level", "stack", "in rust repos run cargo clippy before finishing"]) == 0
    assert cli.main(["teach", "--level", "owner", "use conventional commit messages"]) == 0
    assert cli.main(["teach", "--level", "agent", "--agent", "codex", "prefer apply_patch for edits"]) == 0
    assert cli.main(["teach", "never edit the generated schema"]) == 0
    lv = {r["id"]: r["_level"] for r in rules.all_levels(SLUG)}
    assert lv["run-narrowest-test-first"] == "user" and lv["conventional-commit-messages"] == "owner"
    assert (paths.memory() / "_stacks" / "rust" / "rules").is_dir()

    # Claude Code gets the repo rule and at most two inherited ones; the codex-only rule never reaches it.
    ctx = _hook(repo, "lv1", "SessionStart", source="startup")["hookSpecificOutput"]["additionalContext"]
    assert "never edit the generated schema" in ctx and "apply_patch" not in ctx
    assert sum(t in ctx for t in ("narrowest test", "cargo clippy", "conventional commit")) == 2
    assert "(shared across this organization's repos)" in ctx
    held = [e for e in store.events(store.connect(), "lv1") if e.get("event") == "rule_withheld"] if store.sync() >= 0 else []
    assert held and held[0]["reason"] == "budget"
    agent_idx = index.for_agent(index.load(SLUG), "codex")
    assert any("apply_patch" in r["text"] for r in agent_idx["rules"])

    # A repo rule that overrides an inherited one removes it here only.
    r = rules.find(SLUG, "never-edit-generated-schema") or rules.find(SLUG, "edit-generated-schema")
    r = {k: v for k, v in r.items() if not k.startswith("_")}
    r["overrides"] = "conventional-commit-messages"
    rules.save(SLUG, r)
    assert "conventional-commit-messages" not in {x["id"] for x in rules.all_levels(SLUG)}

    # Lifting to the stack level needs passing replays in two repos, unless forced.
    assert cli.main(["rules", "lift", r["id"], "stack"]) == 1 and "two repos" in capsys.readouterr().out
    assert cli.main(["rules", "lift", r["id"], "stack", "--force"]) == 0
    assert rules.find(SLUG, r["id"], levels=False)["status"] == "deprecated"
    assert next(x for x in rules.all_levels(SLUG) if x["id"] == r["id"] and x.get("_level") == "stack")


# ---------------------------------------------------------------- enforcement levels

def test_enforcement_ladder_remind_check_block(env, capsys):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    cli.main(["teach", "--path", "crates/auth/**/*.rs", "never use SystemTime in the auth crate"])
    rid = rules.active(SLUG)[0]["id"]
    assert cli.main(["rules", "promote", rid]) == 2 and "no detector" in capsys.readouterr().out
    assert cli.main(["rules", "detect", rid, r"SystemTime::now"]) == 0
    assert cli.main(["rules", "promote", rid, "remind"]) == 0
    edit = {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "crates/auth/src/jwt.rs"), "old_string": "a",
                                                 "new_string": "let t = SystemTime::now();"}, "tool_response": {}}
    _hook(repo, "en1", "SessionStart", source="startup")
    out = _enforce(repo, "en1", **edit)
    assert "SystemTime" in out["hookSpecificOutput"]["additionalContext"] and "decision" not in out
    assert _enforce(repo, "en1", **{**edit, "tool_input": {**edit["tool_input"], "new_string": "Utc::now()"}}) is None
    elsewhere = {**edit, "tool_input": {**edit["tool_input"], "file_path": str(repo / "README.md")}}
    assert _enforce(repo, "en1", **elsewhere) is None  # out of the rule's scope

    # check and block need the agent to have ignored the rule first: the promotion test.
    capsys.readouterr()
    assert cli.main(["rules", "promote", rid, "check"]) == 1 and "has not ignored" in capsys.readouterr().out
    _hook(repo, "en2", "SessionStart", source="startup")
    _hook(repo, "en2", "PostToolUse", tool_name="Read", tool_input={"file_path": str(repo / "crates/auth/src/jwt.rs")},
          tool_response={})
    assert cli.main(["rules", "promote", rid, "remind"]) == 0  # stays; ignored count rises with real violations
    cli.main(["rules", "promote", rid, "instruct"])
    _hook(repo, "en3", "SessionStart", source="startup")
    _hook(repo, "en3", "PostToolUse", tool_name="Read", tool_input={"file_path": str(repo / "crates/auth/src/jwt.rs")},
          tool_response={})
    _hook(repo, "en3", "PostToolUse", **edit)  # an ignored rule
    assert cli.main(["rules", "promote", rid, "check"]) == 0
    out = _enforce(repo, "en3", **edit)
    assert out["decision"] == "block" and "SystemTime" in out["reason"]
    # Enforced rules cost nothing up front: not in native rule files, not in context.
    assert not (repo / ".claude/rules/once").exists() or not list((repo / ".claude/rules/once").glob("*.md"))

    assert cli.main(["rules", "promote", rid, "block"]) == 0
    _hook(repo, "en4", "SessionStart", source="startup")
    deny = _hook(repo, "en4", "PreToolUse", tool_name="Write",
                 tool_input={"file_path": str(repo / "crates/auth/src/jwt.rs"), "content": "SystemTime::now()"})
    assert deny["hookSpecificOutput"]["permissionDecision"] == "deny"
    ok = _hook(repo, "en4", "PreToolUse", tool_name="Write",
               tool_input={"file_path": str(repo / "crates/auth/src/jwt.rs"), "content": "Utc::now()"})
    assert ok is None or "permissionDecision" not in ok.get("hookSpecificOutput", {})
    assert cli.main(["undo"]) == 0 and rules.find(SLUG, rid)["enforcement"]["level"] == "check"
    capsys.readouterr()
    cli.main(["rules", "show", rid])
    shown = capsys.readouterr().out
    assert "Enforcement  check" in shown and "ignored in 1" in shown


# ---------------------------------------------------------------- cold-start mining

def test_bootstrap_reads_config_skips_what_is_documented_and_finds_drift(env, capsys):
    repo = env["repo"]
    (repo / "pyproject.toml").write_text("[project]\nname='api'\n[tool.ruff]\nline-length = 100\n[tool.mypy]\nstrict = true\n")
    (repo / "uv.lock").write_text("")
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n")
    (repo / "CONTRIBUTING.md").write_text("# Contributing\n\nRun the tests with `tox -e py`.\n")
    (repo / "CLAUDE.md").write_text("- Run mypy after changing typed code; type errors fail CI.\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "fix lint"], check=True)
    cli.main(["init", "--yes"])
    capsys.readouterr()
    assert cli.main(["lab", "bootstrap"]) == 0
    out = capsys.readouterr().out
    assert "Doc drift: CONTRIBUTING names `tox -e py` but CI runs `cargo test`" in out
    cands = [r for r in rules.load_all(SLUG) if r["status"] == "candidate"]
    texts = " | ".join(r["rule"]["directive"] for r in cands)
    assert "ruff check" in texts and "uv run" in texts and "pre-commit" in texts and "cargo test" in texts
    assert "mypy" not in texts  # already in the repo's instructions: not relearned
    assert all(r["provenance"]["source_kind"] == "bootstrap" for r in cands) and len(cands) <= 15
    assert not rules.active(SLUG)  # suggestions only
    # The do-not-relearn list also stops drafted suggestions.
    assert learn.duplicate_of(SLUG, "Run mypy after changing typed code; type errors fail CI.") == "the repo's instruction files"


# ---------------------------------------------------------------- the outer loop

def _decided_history(env, monkeypatch, n=6):
    """Sessions with tags, drafted and decided: the ground the outer loop learns on."""
    repo = env["repo"]
    cli.main(["init", "--yes"])
    for i in range(n):
        _session(repo, f"m{i}")
        cli.main(["tag", "4-8", "missing", "use the Utc type"])
        learn.learn(SLUG, repo, 3, DRAFTER, 0.1, progress=lambda *_: None)
        for c in [r for r in rules.load_all(SLUG) if r["status"] == "candidate"]:
            learn.decide(SLUG, c, "accept" if i % 2 == 0 else "reject", "t",
                         reason=None if i % 2 == 0 else "one-off", review_secs=30)
            if i % 2 == 0:  # keep the next draft from being a duplicate
                cur = rules.find(SLUG, c["id"])
                cur["status"] = "deprecated"
                rules.save(SLUG, cur)
    return repo


def test_pipeline_versions_drive_detectors_and_roll_back(env):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    _session(repo, "pv1")
    evs = store.events(store.connect(), "pv1") if store.sync() >= 0 else []
    assert any(f["detector"] == "retry_cluster" for f in detectors.detect(evs))
    cfg = pipeline.active()
    cfg["detectors"]["retry_cluster"]["min_repeats"] = 4
    vid = pipeline.save_new(cfg, "test")
    assert pipeline.version() == "p0"  # saved, not used
    pipeline.promote(vid)
    assert not any(f["detector"] == "retry_cluster" for f in detectors.detect(evs))  # 3 repeats < 4
    assert pipeline.rollback() == "p0"
    assert any(f["detector"] == "retry_cluster" for f in detectors.detect(evs))
    # The pipeline version is part of an experiment's fingerprint.
    from once import experiment as ex
    assert ex.fingerprint(SLUG, [], None, "x")["pipeline"] == "p0"
    # A version cannot carry anything outside what the outer loop may tune.
    bad = dict(cfg, lint={"off": True}, redaction=False)
    v2 = pipeline.save_new(bad, "sneaky")
    raw = json.loads((pipeline.folder() / "versions" / f"{v2}.json").read_text())
    assert "lint" not in raw and "redaction" not in raw


def test_meta_detectors_benchmark_drafter_ranking_priors_compare_report(env, monkeypatch, capsys):
    repo = _decided_history(env, monkeypatch)
    st = meta.detector_stats()
    assert st["retry_cluster"]["flags"] == 6 and st["retry_cluster"]["confirmed"] == 6 and st["retry_cluster"]["accepted"] >= 3

    es = meta.benchmark(rebuild=True)
    assert len(es) == 6 and {e["decision"] for e in es} == {"accepted", "rejected"}
    res = meta.tune_drafter(DRAFTER, 0.1, ["short", "no examples"], progress=lambda *_: None)
    assert "error" not in res and "split" in res

    for i in range(5):  # sessions with a rule given, for the ranking policy
        cli.main(["teach", f"rule number {i} for the ranking test"])
        _session(repo, f"rk{i}")
    rk = meta.update_ranking()
    assert rk.get("version") and rk["sessions"] >= 5
    pipeline.promote(rk["version"])
    rules.compile_index(SLUG)
    assert all("score" in r for r in index.load(SLUG)["rules"]) and index.load(SLUG)["policy"]["version"] == rk["version"]
    pipeline.rollback()

    pr = meta.priors(min_n=3)
    assert pr["n"] >= 6 and "overall" in pr

    # Blind comparison: two versions draft the same episodes; the same draft is queued once and shared.
    _session(repo, "cmp1")
    cli.main(["tag", "4-8", "missing", "use the Utc type"])
    for c in [r for r in rules.load_all(SLUG) if r["status"] == "active" and "chrono" in r["rule"]["directive"]]:
        c["status"] = "deprecated"
        rules.save(SLUG, c)
    v1 = pipeline.save_new(pipeline.active(), "copy")
    out = meta.compare(SLUG, repo, ["p0", v1], DRAFTER, 0.1, 5, progress=lambda *_: None)
    assert sum(out["queued"].values()) == 1 and out["shared"] == 1
    rep = meta.report()
    assert rep["p0"]["decided"] >= 6 and rep["p0"]["as_written_rate"] is not None
    assert rep["p0"]["review_min_per_accepted"] == 1.0  # 6 decisions x 30 s, over 3 accepted: rejections cost time too
    capsys.readouterr()
    assert cli.main(["lab", "meta"]) == 0 and "Active pipeline: p0" in capsys.readouterr().out
    assert cli.main(["lab", "meta", "report"]) == 0 and "As written" in capsys.readouterr().out


def test_priors_warn_and_reach_the_drafter(env):
    cfg = pipeline.active()
    vid = meta.apply_priors({"n": 10, "overall": 0.5, "findings": [
        {"feature": "directive_chars", "value": "long", "rate": 0.1, "n": 6, "direction": "worse"}]})
    pipeline.promote(vid)
    long_rule = {"kind": "invariant", "rule": {"directive": "x" * 300}, "scope": {}}
    assert learn.lint_warnings(long_rule) and "short" in learn.lint_warnings(long_rule)[0]
    assert "Keep directives short" in learn.prompt_for({"source": "human_tag", "text": "t"})
    pipeline.rollback()
    assert not learn.lint_warnings(long_rule)


# ---------------------------------------------------------------- cost accounting

def test_cost_report_saving_and_break_even(env, capsys):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    cli.main(["teach", "always run cargo fmt before finishing"])
    r = rules.active(SLUG)[0]
    r["evidence"] = {"replay": {"verdict": "passes", "runs": 3, "cost_usd": 0.6, "steps": {"without": 12, "with": 7},
                                "without_rule": {"failed": 3}, "with_rule": {"failed": 0},
                                "per_run": {"without": {"cost_usd": 0.12, "tokens": 9000},
                                            "with": {"cost_usd": 0.08, "tokens": 6000}}}}
    rules.save(SLUG, r)
    _session(repo, "cost1")
    rep = cost.report(SLUG)
    row = rep["rules"][0]
    assert row["saving_per_firing"] == 0.04 and row["break_even_firings"] == 15.0 and row["token_saving"] == 3000
    assert rep["sessions"]["injected_tokens"] > 0 and rep["prices"]["loaded"] is False
    cost.init_prices(["claude-test"])
    p = yaml.safe_load(cost.prices_file().read_text())
    assert p["models"]["claude-test"]["input"] is None  # never guessed
    p["models"]["claude-test"] = {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}
    p["date"] = "2026-11-01"
    cost.prices_file().write_text(yaml.safe_dump(p))
    assert round(cost.dollars({"input_tokens": 1_000_000, "output_tokens": 0}, "claude-test", cost.load_prices()), 2) == 3.0
    capsys.readouterr()
    assert cli.main(["lab", "cost"]) == 0
    out = capsys.readouterr().out
    assert "15 firings" in out and "2026-11-01" not in out or "prices from" in out


# ---------------------------------------------------------------- dashboard

def test_dashboard_trend_filters_keys_stream_and_rule_actions(env):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    cli.main(["teach", "--path", "crates/auth/**/*.rs", "never use SystemTime in the auth crate"])
    rid = rules.active(SLUG)[0]["id"]
    for sid in ("d1", "d2"):
        _session(repo, sid)
    srv = ui.serve(SLUG, lambda s: cli._refresh(s), 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    get = lambda p: urllib.request.urlopen(base + p, timeout=5).read().decode()

    def post(url, **form):
        req = urllib.request.Request(base + url, data=urllib.parse.urlencode(form).encode())
        try:
            r = urllib.request.urlopen(req, timeout=5)
            return r.status, r.url
        except urllib.error.HTTPError as e:
            return e.code, ""
    try:
        ov = get(f"/overview?repo={SLUG}")
        assert "<svg viewBox" in ov and "By week" in ov and "verified rate" in ov and "no earlier period" in ov
        assert f"/sessions?repo={urllib.parse.quote(SLUG)}&amp;flagged=1" in ov or "flagged=1" in ov
        assert "Showing sessions touching crates/auth/src/" in get(f"/sessions?repo={SLUG}&path=crates/auth/src/")
        assert "No sessions" not in get(f"/sessions?repo={SLUG}&flagged=1")
        sess = get("/session/d1")
        assert 'id="keys"' in sess and 'data-step="4"' in sess and 'id=tagform' in sess
        # The live stream pushes the view as a server-sent event.
        with urllib.request.urlopen(base + f"/live/stream?repo={SLUG}", timeout=5) as r:
            assert r.headers["Content-Type"] == "text/event-stream"
            assert r.readline().decode().startswith("data: ")
        rp = get(f"/rules?repo={SLUG}&id={rid}")
        assert "ignored in 0" in rp and "Promote to remind" in rp and "Save wording" in rp
        code, url = post("/rules/promote", token=ui.TOKEN, slug=SLUG, id=rid)
        assert code == 200 and "no+detector" in url.replace("%20", "+") or "no detector" in urllib.parse.unquote(url)
        assert post("/rules/edit", token=ui.TOKEN, slug=SLUG, id=rid, text="Never use SystemTime in crates/auth.")[0] == 200
        assert rules.find(SLUG, rid)["rule"]["directive"] == "Never use SystemTime in crates/auth."
        assert post("/rules/edit", slug=SLUG, id=rid, text="x")[0] == 403  # still needs the token
    finally:
        srv.shutdown()
