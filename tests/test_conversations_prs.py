"""Pull requests and Conversations pages, for Claude Code, Codex and Antigravity: each agent's PR links are
captured from its own tool output, each agent's transcript format is read, and both pages render them."""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from conftest import run_hook
from once import cli, conversations, prs, store, ui

SLUG = "github.com/acme/api"
KEY = "sk-ant-api03-Zq8xW2vB7nM4kL9pR3tY6uI1oP5aS0dF"


@pytest.fixture(autouse=True)
def no_github(monkeypatch):
    monkeypatch.setattr(prs, "gh_available", lambda: False)  # never call the real gh from a test


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                          capture_output=True, text=True, check=True).stdout.strip()


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".000Z"


def agy(event, payload):
    r = subprocess.run([sys.executable, "-m", "once.hook", "antigravity", event], text=True, capture_output=True,
                       input=json.dumps(payload), env=os.environ.copy())
    assert r.returncode == 0, r.stderr


def session(con, sid):
    store.sync(con)
    row = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    return row, store.events(con, sid)


# ---------------------------------------------------------------- one session per agent

def claude_session(repo, tmp_path, sid="cc1"):
    t0 = time.time() - 60
    t = tmp_path / f"{sid}.jsonl"
    t.write_text("\n".join(json.dumps(x) for x in [
        {"type": "user", "timestamp": iso(t0), "message": {"role": "user", "content": "Fix the timezone bug in token expiry"}},
        {"type": "user", "isMeta": True, "timestamp": iso(t0), "message": {"role": "user", "content": "<system-reminder>x</system-reminder>"}},
        {"type": "assistant", "timestamp": iso(t0 + 1), "message": {"id": "m1", "role": "assistant", "content": [
            {"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": "Expiry uses local time; I will move it to UTC."}]}},
        {"type": "assistant", "timestamp": iso(t0 + 2), "message": {"id": "m1", "role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "gh pr create"}}]}},
        {"type": "user", "timestamp": iso(t0 + 3), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "https://github.com/Acme/API/pull/482"}]}},
    ]) + "\n")
    base = {"session_id": sid, "cwd": str(repo), "transcript_path": str(t)}
    run_hook({**base, "hook_event_name": "SessionStart", "source": "startup", "model": "claude-test"})
    run_hook({**base, "hook_event_name": "UserPromptSubmit", "prompt": "Fix the timezone bug in token expiry"})
    run_hook({**base, "hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "gh pr create --fill"},
              "tool_response": {"stdout": "Creating pull request\nhttps://github.com/Acme/API/pull/482\n", "stderr": ""}})
    with open(t, "a") as f:  # the reply after the tool call is written after the hook saw it
        f.write(json.dumps({"type": "assistant", "timestamp": iso(time.time() + 1), "message": {"id": "m2", "role": "assistant",
                            "content": [{"type": "text", "text": f"Opened #482. Never paste {KEY} anywhere."}]}}) + "\n")
    run_hook({**base, "hook_event_name": "SessionEnd", "reason": "exit"})


def codex_session(repo, tmp_path, sid="cx1"):
    t0 = time.time() - 50
    t = tmp_path / "rollout-2026-11-19T10-00-00-cx1.jsonl"
    t.write_text("\n".join(json.dumps(x) for x in [
        {"timestamp": iso(t0), "type": "session_meta", "payload": {"id": sid, "cwd": str(repo)}},
        {"timestamp": iso(t0), "type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>cwd</environment_context>"}]}},
        {"timestamp": iso(t0), "type": "response_item", "payload": {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "Address the review on the refresh path"}]}},
        {"timestamp": iso(t0), "type": "event_msg", "payload": {"type": "user_message", "message": "Address the review on the refresh path"}},
        {"timestamp": iso(t0 + 1), "type": "event_msg", "payload": {"type": "agent_message", "message": "Switched refresh to Utc::now and pushed."}},
        {"timestamp": iso(t0 + 1), "type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
            {"type": "output_text", "text": "Switched refresh to Utc::now and pushed."}]}},
    ]) + "\n")
    base = {"session_id": sid, "cwd": str(repo), "transcript_path": str(t), "model": "gpt-test"}
    run_hook({**base, "hook_event_name": "SessionStart", "source": "startup"}, "codex")
    run_hook({**base, "hook_event_name": "UserPromptSubmit", "prompt": "Address the review on the refresh path"}, "codex")
    run_hook({**base, "hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": ["bash", "-lc", "gh pr view 482"]},
              "tool_response": "Exit code: 0\nOutput:\ntitle: Fix expiry\nurl: https://github.com/acme/api/pull/482\n"}, "codex")
    run_hook({**base, "hook_event_name": "Stop"}, "codex")


def agy_session(repo, tmp_path, sid="ag1"):
    t0 = time.time() - 40
    t = tmp_path / "brain" / sid / "transcript_full.jsonl"
    t.parent.mkdir(parents=True)
    rows = [
        {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": iso(t0)[:19] + "Z",
         "content": "<USER_REQUEST>\nAdd cursor pagination to orders\n</USER_REQUEST>\n<ADDITIONAL_METADATA>x</ADDITIONAL_METADATA>"},
        {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE", "created_at": iso(t0 + 1)[:19] + "Z",
         "tool_calls": [{"name": "run_command", "args": {"CommandLine": "gh pr create --fill"}}]},
        {"step_index": 2, "source": "MODEL", "type": "GENERIC", "created_at": iso(t0 + 2)[:19] + "Z",
         "content": "Command exited with code 0.\n\nCommand output:\nhttps://github.com/acme/api/pull/479\n"},
    ]
    t.write_text("\n".join(json.dumps(x) for x in rows) + "\n")
    base = {"conversationId": sid, "workspacePaths": [str(repo)], "transcriptPath": str(t)}
    agy("SessionStart", base)
    agy("PreInvocation", {**base, "lastUserInput": "Add cursor pagination to orders"})
    agy("PostToolUse", {**base, "stepIdx": 2, "toolCall": {"name": "run_command", "args": {"CommandLine": "gh pr create --fill"}}})
    with open(t, "a") as f:
        f.write(json.dumps({"step_index": 3, "source": "MODEL", "type": "PLANNER_RESPONSE",
                            "created_at": iso(time.time() + 2)[:19] + "Z", "content": "Opened the pagination PR."}) + "\n")
    agy("Stop", base)


def test_each_agent_links_its_pr_and_reads_its_transcript(env, tmp_path):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    claude_session(repo, tmp_path)
    codex_session(repo, tmp_path)
    agy_session(repo, tmp_path)
    con = store.connect()

    row, evs = session(con, "cc1")
    pr, how = prs.link(SLUG, "cc1", evs, row["started"], [])
    assert (pr["number"], how) == (482, "gh pr create")
    items, have = conversations.thread(evs, "cc1", "claude-code")
    assert have and [i["kind"] for i in items] == ["user", "agent", "steps", "agent"]
    assert "moved" not in items[1]["text"] and "UTC" in items[1]["text"] and "hidden" not in json.dumps(items, default=str)
    assert KEY not in items[-1]["text"] and "[REDACTED]" in items[-1]["text"]

    row, evs = session(con, "cx1")
    pr, how = prs.link(SLUG, "cx1", evs, row["started"], [])
    assert (pr["number"], how) == (482, "tool output")  # gh pr view: seen, not created
    items, have = conversations.thread(evs, "cx1", "codex")
    texts = [i.get("text") for i in items if i["kind"] != "steps"]
    assert have and texts == ["Address the review on the refresh path", "Switched refresh to Utc::now and pushed."]

    row, evs = session(con, "ag1")
    pr, how = prs.link(SLUG, "ag1", evs, row["started"], [])
    assert (pr["number"], how) == (479, "gh pr create")
    items, have = conversations.thread(evs, "ag1", "antigravity")
    assert have and [i["kind"] for i in items] == ["user", "steps", "agent"]
    assert items[0]["text"] == "Add cursor pagination to orders" and items[-1]["text"] == "Opened the pagination PR."


def test_branch_and_commit_links_and_by_hand(env, tmp_path):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    git(repo, "checkout", "-q", "-b", "fix/token-expiry-tz")
    base = {"session_id": "br1", "cwd": str(repo)}
    run_hook({**base, "hook_event_name": "SessionStart"})
    (repo / "a.txt").write_text("x")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "work")
    sha = git(repo, "rev-parse", "HEAD")
    run_hook({**base, "hook_event_name": "SessionEnd", "reason": "exit"})
    con = store.connect()
    row, evs = session(con, "br1")
    assert evs[0]["branch"] == "fix/token-expiry-tz" and any(e["event"] == "git_state" and e["head"] for e in evs)
    now = time.time()
    pr_b = {"number": 9, "url": "https://github.com/acme/api/pull/9", "state": "open", "head": "fix/token-expiry-tz",
            "created": now, "commits": []}
    assert prs.link(SLUG, "br1", evs, row["started"], [pr_b], repo)[1] == "branch"
    pr_c = {"number": 10, "url": "https://github.com/acme/api/pull/10", "state": "merged", "head": "other", "commits": [sha]}
    assert prs.link(SLUG, "br1", evs, row["started"], [pr_c], repo) == (pr_c, "commit")
    assert prs.link(SLUG, "br1", evs, row["started"], [], repo) == (None, "")
    assert prs.parse_ref("#12", SLUG) == "https://github.com/acme/api/pull/12"
    assert prs.parse_ref("github.com/o/r/pull/3/files", SLUG) == "https://github.com/o/r/pull/3"
    assert prs.parse_ref("nope", SLUG) is None
    prs.set_manual("br1", prs.parse_ref("77", SLUG))
    assert prs.link(SLUG, "br1", evs, row["started"], [pr_c], repo)[0]["number"] == 77
    prs.set_manual("br1", "")
    assert prs.link(SLUG, "br1", evs, row["started"], [pr_c], repo) == (None, "")
    assert prs.checks_state([{"conclusion": "SUCCESS"}, {"state": "FAILURE"}]) == "failing"
    assert prs.checks_state([{"conclusion": "SUCCESS"}]) == "passing"


def test_both_pages_render_and_link_by_hand(env, tmp_path):
    repo = env["repo"]
    cli.main(["init", "--yes"])
    claude_session(repo, tmp_path)
    codex_session(repo, tmp_path)
    agy_session(repo, tmp_path)
    base_ = {"session_id": "lone", "cwd": str(repo)}
    run_hook({**base_, "hook_event_name": "SessionStart"})
    run_hook({**base_, "hook_event_name": "UserPromptSubmit", "prompt": "Explain the auth crate layout"})
    cache = prs._cache_file(SLUG)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"fetched": time.time(), "prs": [
        {"number": 482, "url": "https://github.com/Acme/API/pull/482", "title": "Fix token expiry timezone handling",
         "state": "merged", "head": "fix/tz", "merged": time.time() - 7200, "additions": 84, "deletions": 21,
         "checks": "passing", "commits": []}]}))
    srv = ui.serve(SLUG, lambda s: cli._refresh(s), 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    get = lambda p: urllib.request.urlopen(base + p, timeout=10).read().decode()

    def post(url, **form):
        req = urllib.request.Request(base + url, data=urllib.parse.urlencode(form).encode())
        try:
            return urllib.request.urlopen(req, timeout=10).url
        except urllib.error.HTTPError as e:
            return e.code
    try:
        p = get(f"/prs?repo={SLUG}")
        assert "Fix token expiry timezone handling" in p and "checks passing" in p and "+84" in p
        assert "gh pr create" in p and "tool output" in p and "#479" in p and "Explain the auth crate layout" in p
        assert "Sessions with no PR yet" in p and 'href="/prs"' in get("/live")  # in the sidebar
        assert "Fix token expiry" in get(f"/prs?repo={SLUG}&state=merged") and "#479" not in get(f"/prs?repo={SLUG}&state=merged")

        c = get(f"/conversations?repo={SLUG}&sid=cc1")
        assert "Expiry uses local time" in c and "#482 merged" in c and KEY not in c and "Copy as Markdown" in c
        assert "Switched refresh to Utc::now" in get(f"/conversations?repo={SLUG}&sid=cx1")
        assert "Opened the pagination PR." in get(f"/conversations?repo={SLUG}&sid=ag1")
        only = get(f"/conversations?repo={SLUG}&agent=codex")
        assert "Address the review" in only and "Add cursor pagination" not in only
        found = get(f"/conversations?repo={SLUG}&q=" + urllib.parse.quote("pagination PR"))  # words only in a reply
        assert "1 conversation matching" in found and "Add cursor pagination" in found
        lone = get(f"/conversations?repo={SLUG}&sid=lone")
        assert "transcript is not on this machine" in lone and "Explain the auth crate layout" in lone

        tok = ui.TOKEN
        assert "Not linked" in urllib.parse.unquote(str(post("/prs/link", token=tok, sid="lone", ref="banana", back="/prs")))
        post("/prs/link", token=tok, sid="lone", ref="#482", back="/session/lone")
        assert "Linked by by hand" in get("/session/lone")
        p = get(f"/prs?repo={SLUG}")
        assert "Sessions with no PR yet" not in p and "by hand" in p  # now under #482, with the rest
        post("/prs/link", token=tok, sid="lone", action="auto", back="/session/lone")
        assert "No PR found" in get("/session/lone")
        assert post("/prs/link", token="wrong", sid="lone", ref="1") == 403
        assert post("/prs/link", token=tok, sid="lone", ref="1", back="https://evil.example").endswith("/prs")
    finally:
        srv.shutdown()


def test_transcripts_that_are_missing_odd_or_huge_do_not_break_the_page(env, tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n[1,2]\n" + json.dumps({"type": "assistant", "message": "str"}) + "\n")
    assert conversations.messages(str(bad), "claude-code") == []
    assert conversations.messages(str(tmp_path / "missing.jsonl"), "codex") == []
    assert conversations.read_codex(str(bad)) == [] and conversations.read_antigravity(str(bad)) == []
    cmd = tmp_path / "cmd.jsonl"
    cmd.write_text(json.dumps({"type": "user", "message": {"role": "user", "content":
                   "<command-name>/once-why</command-name><command-args></command-args>"}}) + "\n")
    assert conversations.read_claude(str(cmd))[0]["text"] == "/once-why"


def test_a_too_big_gh_query_falls_back_and_a_failure_is_shown(env, monkeypatch):
    calls = []

    def fake(slug, limit, fields):
        calls.append(fields)
        if "commits" in fields:
            return subprocess.CompletedProcess([], 1, "", "GraphQL: ... which exceeds the maximum limit of 500,000.")
        return subprocess.CompletedProcess([], 0, json.dumps([{"number": 5, "title": "t", "state": "OPEN", "url": "https://github.com/acme/api/pull/5",
                                                                "headRefName": "x", "statusCheckRollup": []}]), "")
    monkeypatch.setattr(prs, "gh_available", lambda: True)
    monkeypatch.setattr(prs, "_gh_list", fake)
    assert prs.fetch(SLUG) == (True, "1 PRs") and len(calls) == 2 and prs.cached(SLUG)[0][0]["commits"] == []
    monkeypatch.setattr(prs, "_gh_list", lambda *a: subprocess.CompletedProcess([], 1, "", "HTTP 401: Bad credentials"))
    assert prs.fetch(SLUG) == (False, "HTTP 401: Bad credentials")
    assert prs.last_error(SLUG)[0] == "HTTP 401: Bad credentials" and not prs.refresh_async(SLUG)  # no retry storm
    con = store.connect()
    cli.main(["init", "--yes"])
    run_hook({"session_id": "e1", "cwd": str(env["repo"]), "hook_event_name": "SessionStart"})
    store.sync(con)
    assert "GitHub did not answer" in ui.view_prs(con, SLUG) and "Bad credentials" in ui.view_prs(con, SLUG)
