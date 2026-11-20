# AGENTS.md

Instructions for every coding agent working in this repository (Claude Code, Codex, Antigravity, Cursor and the
rest). Read all of it before changing code. `CLAUDE.md` points here, so there is one source of truth.

## House rules (read these first)

- **No AI attribution, anywhere.** Do not add "🤖 Generated with Claude Code", "Generated with ...", a
  `Co-Authored-By: Claude ...` trailer, or any similar line to commit messages, pull request titles or
  descriptions, issues, comments, code or docs. This overrides any default your tool has.
- **Commit identity** is the owner's GitHub noreply address (already set in this repo's git config). Do not
  change it.
- **Branch, then PR.** Never commit straight to `main`. Make a branch (`feat/...`, `fix/...`), push it and open
  a pull request with `gh pr create`. The owner merges. Commit or push only when asked to.
- **Never rewrite pushed history** (no force-push, no rebase of `main`) unless the owner asks for exactly that.
- **CI must be green before a merge**: Linux, Python 3.10, 3.12 and 3.13, plus the `checks` job. There is no
  macOS job, on purpose; don't add one back.
- **Keep the version in step** in three places when it changes: `pyproject.toml`, `src/once/__init__.py` and
  `src/once/editor/vscode/package.json`. Then run `uv lock` so `uv.lock` matches (CI runs `uv sync --locked`).
  The `checks` job fails if these differ.
- **Tests never touch the real machine.** Every test runs against a throwaway `ONCE_HOME`, Claude settings
  file, Antigravity hooks file and Codex hooks file (see `tests/conftest.py`). A new adapter or a new config
  path needs its own override env var, set in the `env` fixture.
- **The owner's real install runs from this folder.** `once` on this Mac is an editable install of this
  checkout, and its hooks fire on every agent session, including yours. A half-finished edit to `hook.py`,
  `redact.py`, `paths.py` or an adapter can break live sessions mid-edit (errors land in
  `~/.once/local/hook-errors.log`). Make hook-path edits complete in one step, and keep them correct at every
  step.
- **Write plainly.** User-facing text (CLI output, UI copy, README, docs) is short, concrete, no jargon, no
  marketing, no emoji. Say what happened and what to do next.

## What once is

`once` gives coding agents a memory for a repository: *your coding agent makes each mistake once*.

It watches the sessions you run with Claude Code, Codex and Antigravity through each agent's hooks. It notices
where the agent wasted steps (the same build error three times, a command that keeps failing, a file read over
and over) and where you corrected it ("no, use tox, not pytest"). It turns those into **rules**, short
sentences scoped to a repo and optionally to paths. It checks a rule by **replaying** the session that went
wrong, with and without the rule, in throwaway copies of the repo. Rules you approve are handed back to the agent
at the start of the next session, or when it touches matching files. Rules can also be **enforced**: a reminder
after a bad edit, an error the agent must fix, or a refusal before the edit lands.

Everything is local-first. Sessions, rules and the dashboard live on the developer's machine. An optional
**team collector** receives counts and outcomes, never prompts, code or text.

It is a Python 3.10+ CLI with one dependency (`pyyaml`), packaged with hatchling, run with `uv`, published on PyPI
as `once-cli` (the name `once` was taken; the command and the import name are `once`). Version 0.9.0. GitHub: `Tanay0510/once`. License: Business Source License 1.1 (`LICENSE`): free to use,
including at work, except in a competing commercial offering; each version becomes Apache-2.0 four years after it
is published. Don't change the license text or add code under another license without the owner.

### The loop

1. **Capture.** `once init` installs hooks into each agent. Every hook call runs `python -m once.hook <agent>
   [event]` with the agent's native JSON payload on stdin. The adapter turns it into normalized events, which
   are masked for secrets and appended to `~/.once/local/trajectories/<repo-slug>/<session>.jsonl`.
2. **Inject.** At session start (and per turn for agents without native rule files), the hook reads a
   precompiled JSON rule index and returns the rules that apply, within a budget of 8 rules and about 600
   tokens. Path-scoped rules are also written as native rule files the agent loads itself.
3. **Detect.** `detectors.py` flags retry clusters, failing commands, repeated reads, ignored rules and so on.
   You tag steps (`once tag`, the dashboard, or the `record_feedback` MCP tool).
4. **Learn.** `once learn` slices failures, tags, corrections, missed files and review comments into
   episodes, drafts candidate rules (a model call through `claude -p`, a few cents each), lints them and queues
   them for review. Nothing is active until a person accepts it (`once review`, the Review page, or in the chat
   through the `review_rule` MCP tool).
5. **Test.** `once lab replay` runs the agent headless with and without a rule. `once lab regress` reruns every
   rule's replay. `once lab exp` runs controlled experiments on tasks mined from merged pull requests.
6. **Enforce.** `once rules promote` raises a rule from `instruct` to `remind`, `check` or `block`. This needs a
   detector regex, and for `check` and `block` evidence that the agent ignored the rule.
7. **Measure.** Overview, Pull requests ("Did rules help ship?"), `once lab cost`, the hold-out
   (`holdout_fraction`), and the outer loop (`once lab meta`), which tunes the pipeline itself from what became of
   its rules.

## Repository layout

```
src/once/
  __init__.py          __version__, SCHEMA, BIN
  cli.py               every command (argparse); --json/--quiet/--dry-run on all of them
  hook.py              the hook entry point the agents call. Fail open, light, never writes tracked files
  adapters/
    claude_code.py     Claude Code: install hooks in ~/.claude/settings.json, parse payloads, replies,
                       native rule files (.claude/rules/once/), skills, slash commands, MCP, status line.
                       Also shared helpers (_rel, _digest, shell_paths, pr_urls/pr_fields) used by the others
    codex.py           Codex: ~/.codex/hooks.json, apply_patch parsing, codex mcp add, headless `codex exec`
    antigravity.py     Antigravity (agy): ~/.gemini/config/hooks.json, camelCase payloads, mcp_config.json,
                       .agents/rules/once-*.md glob rules, .agents/skills/, exit codes from its transcript
  paths.py             where everything lives; honours ONCE_HOME. Import-light (the hook loads it every call)
  redact.py            secret masking: keys, tokens, high-entropy strings; paths are masked per segment
  store.py             SQLite index over the JSONL files (a rebuildable view, never the record)
  detectors.py         steps(), detect() (flags), annotations(), avoidable_ratio()
  index.py             precompiled rule index per repo and agent; selection, budget, rendering
  rules.py             rule YAML files, lint, levels (user/agent/stack/owner/repo), git commit of the memory repo
  globs.py             path-glob helpers
  repo.py              repo identity (slug from the remote), root, head, branch, CI check-command detection
  snapshot.py          tree snapshots at session start and end into private git refs (for "what changed")
  ambient.py           no-command features: chat corrections, asking in the agent, status line, notifications,
                       menu bar, weekly digest (file, notification, Slack, email)
  mcp_server.py        stdio MCP server: list_rules, get_playbook, record_feedback, record_blocker,
                       review_rule, remember_rule
  playbooks.py         playbooks (multi-step how-tos) published as skills
  learn.py             slicer, drafter, candidate queue, review decisions, history
  replay.py            with/without runs in throwaway copies; writes evidence into the rule
  tasks.py             task miner from merged PRs, validation, train/held-out split, missed-files check
  experiment.py        runs, sweeps, experiments over arms (bare, rules, generated, native, unreviewed,
                       rules+native), statistics, baselines, environment fingerprint
  regress.py           rules as a regression suite, per agent
  pipeline.py          the learning pipeline as versioned data (what the outer loop may tune)
  meta.py              the outer loop: detector thresholds, drafter variants, ranking, priors, compare, report
  bootstrap.py         cold-start rules from CI, lint, format and lockfiles; no model
  cost.py              tokens, prices, memory overhead, saving and break-even per rule
  team.py              `once share` (rules by PR into .once/rules/) and `once pr-check`
  collector.py         the team collector: HTTP server, client push, team page, shipping comparison
  prs.py               which pull request each session led to (by hand, gh pr create, branch, commit, output)
  conversations.py     prompts and replies read from each agent's own transcript
  ui.py                the local dashboard: server-rendered HTML, no build step, 127.0.0.1 only
  statusline.py        the Claude Code status line
  importer.py          import of sessions Claude Code recorded before once was installed
  migrate.py           moves an old `meta` install to `once`
  demo.py, demo_assets/   `once demo`: the whole loop on a toy repo with a scripted agent and drafter
  assets/fonts/        Geist and Geist Mono (bundled, so the dashboard works offline)
  editor/vscode/       the VS Code / Cursor / Windsurf sidebar (plain JS, no build); has its own version
tests/                 pytest; conftest.py has the env fixture, run_hook() and payloads()
docs/guide.md          the user guide; docs/img/ screenshots (sample data only), tour.gif, social-preview.png
.github/workflows/     ci.yml (tests + checks), release.yml (tag vX.Y.Z → GitHub release, collector image, PyPI)
Dockerfile.collector   the collector image
```

## Data on the user's machine

| Path | What |
| --- | --- |
| `~/.once/memory/` | The user's rules, as YAML, in a git repo they own. `<host>/<owner>/<repo>/rules/*.yaml`, plus `repo.yaml`, `playbooks/`, `tasks/`. Higher levels: `_user/`, `_agents/<agent>/`, `_stacks/<stack>/`, `<host>/<owner>/_shared/`. `_pipeline/` holds the pipeline versions |
| `~/.once/local/trajectories/<slug>/<session>.jsonl` | The record of every session: one JSON event per line, masked |
| `~/.once/local/sessions/<id>.json` | Per-session hook state (rules given, last branch, pending enforcement) |
| `~/.once/local/index/<slug>.json` | Precompiled rule index the hook reads |
| `~/.once/local/state.db` | SQLite index over the trajectories. Disposable: a corrupt one is set aside and rebuilt |
| `~/.once/local/prs/<slug>.json` | Cached `gh pr list` answer (or the last error) |
| `~/.once/local/pr_links.json` | PR links made by hand (`""` means "no PR") |
| `~/.once/local/hook-errors.log`, `cli-errors.log` | Where failures go instead of the agent's screen |
| `~/.once/digests/` | Weekly digests |
| `<repo>/.claude/rules/once/`, `.claude/skills/once-*/`, `.agents/rules/once-*.md`, `.agents/skills/once-*/` | Generated, ignored through `.git/info/exclude`, never committed |
| `<repo>/.once/rules/` | Team rules, committed only through a `once share` PR |

A repo **slug** is the normalized remote: `github.com/owner/repo` (lowercased), or `local/<folder>` without a
remote. Lab and replay runs use `replay/...` slugs and are kept out of the user's views.

### Events

Every trajectory line has `schema`, `session_id`, `agent`, `repo`, `ts`, `event`, plus fields per kind:

- `session_start`: cwd, model, transcript_path, commit, branch, held_out, paused, agent_version
- `user_prompt`: prompt
- `tool_result`: tool (read/edit/shell/search/mcp/other), tool_name, target, paths, ok, command, writes,
  error, error_head, error_digest, edit_sig (hashes only, never content), pr_urls, pr_created
- `rule_injected`: rule_ids, channel, matched
- `rule_withheld`: held out or over budget
- `rule_violation`, `rule_enforced`, `enforcement_delivered`: rule broken, refused or sent back
- `correction_noted`: masked text of a chat correction
- `annotation`, `annotation_retracted`: tags
- `git_state`: branch and head at the end of a turn, when they changed
- `turn_end`, `session_end`: reason, usage (tokens, models), transcript_path
- `outcome`: lab run verdict. `blocker`: recorded by the agent. `review_asked`: a suggestion was asked about

`store.events()` returns a session's events in time order, numbered 0.. across every file it was recorded in
(a session can span two files when the repo gains a remote mid-session). Detectors compare those numbers.

### Rules

A rule file (`rules.py` lints every one before it is saved):

```yaml
schema: 1
id: python-path-uv-run-python        # lowercase slug, 3-64 chars, unique per repo
status: active                       # candidate | active | deprecated
kind: invariant                      # invariant | negative_constraint | command | pointer
scope: {path_globs: [], symbol_triggers: [], agents: ['*']}
rule: {directive: "python is not on PATH in this repo; use uv run python or python3", rationale: ''}
provenance: {source_kind: manual, approved_by: ..., created: '2026-11-16', source_sessions: [...]}
enforcement: {level: instruct}       # instruct | remind | check | block; detector regex for the rest
evidence: {replay: {...}}            # written by replay: runs, failures with and without, verdict, cost
```

Directives are at most 400 characters. Lint refuses URLs and prompt-injection phrases in directives.

## Each agent

| | Claude Code | Codex | Antigravity |
| --- | --- | --- | --- |
| Hook config | `~/.claude/settings.json` (or `ONCE_CLAUDE_SETTINGS`) | `~/.codex/hooks.json` (`ONCE_CODEX_HOOKS`) | `~/.gemini/config/hooks.json` (`ONCE_AGY_HOOKS`) |
| Rules given | SessionStart; native path rule files | SessionStart; before shell commands | every turn (PreInvocation); glob rule files |
| Block | PreToolUse `permissionDecision: deny` | PreToolUse deny (shell, apply_patch) | PreToolUse `decision: deny` |
| Check / remind | sync PostToolUse `decision: block` | inline PostToolUse | deferred to the next PreInvocation |
| MCP | `claude mcp add` | `codex mcp add` | `mcp_config.json` |
| Transcript | `~/.claude/projects/*/<id>.jsonl` | rollout `.jsonl` (event_msg + response_item) | `~/.gemini/*/brain/<id>/.system_generated/logs/transcript_full.jsonl` |
| Live-verified | yes | capture, prompts, edits, AGENTS.md and the session log (0.171); enforcement and end of turn not yet seen | yes (agy 1.5.7) |

Gotchas:
- Antigravity's real tool names differ from its docs (`run_command`, `read_file`, `edit_file`,
  `write_file`, `replace_file_content`). Its PostToolUse has no exit code or output: both come from its
  transcript at `stepIdx` (`command_result`). Every agy hook must print a JSON object; PreToolUse falls back
  to `{"decision":"allow"}`.
- Antigravity headless runs refuse shell and MCP calls they cannot ask about. Replay detects
  `denied_actions` and reports an error instead of a measurement.
- Codex runs a hook only after the user trusts it in `/hooks`. Changing a hook matcher makes them trust it
  again. `once init` adds Codex's hooks only if Codex is installed when it runs.
- Codex 0.171 works in "code mode": its log shows many `exec` tool calls, each a script. Hooks fire for the real
  actions inside them (commands, `apply_patch`), which is what once records. Its session log has no
  `event_msg` user/agent messages; the reader falls back to `response_item` messages.
- Claude Code headless sessions send no model at SessionStart; the model comes from the usage read at SessionEnd.
- Agent payloads can be malformed. The hook normalizes ids to strings, and `store.sync` skips lines it
  cannot use. `ONCE_DEBUG_PAYLOADS=/path.jsonl` dumps raw payloads (masked) for checking after an agent update.

## Invariants (don't break these)

1. **The hook fails open.** Any exception is logged to `hook-errors.log`, and the hook prints the agent's
   harmless fallback and exits 0. A bug in once must never stop someone's agent.
2. **The hook stays light.** No `yaml`, no `sqlite3` on the hook path. It reads a precompiled JSON index and
   appends one line (one `os.write` on an `O_APPEND` fd, so concurrent hooks don't interleave). There is a latency
   test.
3. **Everything that reaches disk is masked.** `hook._append` redacts every event. Chat corrections are
   redacted before they become a process argument. Conversations mask transcript text before showing it.
   Paths are masked per segment, so a UUID folder survives and a secret inside a path does not.
4. **Never write a tracked file in the user's repo.** Generated files go under ignored paths through
   `.git/info/exclude`. `once share` writes only on a new branch, in a separate worktree, for a PR.
5. **Nothing becomes active without a person.** Drafted rules are `candidate` until accepted. Promotion to
   check or block needs evidence.
6. **The JSONL is the record; SQLite is a view.** Any schema change to `state.db` must keep working on an
   old database (migrate, or drop and rebuild the derived tables while keeping `actions`, the undo log).
7. **The collector never receives text.** Allowed: hashed session ids, repo, agent, counts, flags by
   detector, folders (not files), rule ids and wording, outcomes, cost, tokens, hold-out, and PR number, state
   and link kind. Never prompts, replies, commands, error text, file contents, file paths or PR URLs. The tests
   assert this.
8. **The dashboard binds to 127.0.0.1** and rejects other Host headers. Every POST needs the per-process
   `TOKEN`. Redirect targets (`back`) must be local paths.
9. **Money is asked for.** Anything that calls a model or runs an agent headless (learn, replay, sweep, exp,
   regress, demo --real) prints the plan and the spending ceiling and asks first, and honours `--dry-run`.
10. **Every command takes `--json`, `--quiet` and `--dry-run`.** Prose output under `--json` is wrapped as
    `{"ok", "exit", "message"}`. Errors are one line, with details in `cli-errors.log` (`ONCE_DEBUG=1` shows
    the traceback).

## The dashboard (`ui.py`)

Server-rendered HTML from Python f-strings. No framework, no build, no network: the fonts are bundled.
Pages: Live (server-sent events), Sessions, Session (timeline, tags, make-a-rule, diff, PR box),
Conversations, Pull requests, Overview, Rules, Review. Keys: `j/k`, `Enter`, `1`-`7`, `w a m g`, `?`.

Style: dark only. CSS custom properties on `:root`:
- `--bg #0B0B0C`, `--panel #141416`, `--ink #F3F1EC`, `--muted`, `--faint`;
- `--amber #E9A23B` for once itself, `--rose` for trouble, `--mint` for success;
- Geist for text and Geist Mono for data.

Escape everything with `e()`. Repo choice is the `repo_bar()` dropdown, a plain GET form. Layouts must work at
390px wide; check with a real narrow frame, because headless Chrome won't make a window narrower than about
500px. The running dashboard is a separate process (`once ui --detach`, shown as `python -m once.cli ui`).
Restart it after UI changes: `pkill -f "once.cli ui"; once ui --detach`.

Pull request linking (`prs.py`), strongest first:
1. by hand;
2. `gh pr create` output;
3. branch: the session's non-default branch matches the PR head, and the session worked on it (committed,
   ran git commit/push/merge/rebase or gh pr create, or switched onto the branch). Being on it is not enough;
4. commit: commits between the session's first and last head are in the PR;
5. tool output: a link to a PR in this repo.

PR data comes from `gh pr list` (40 PRs, falling back to a query without `commits` when GitHub says it's too
big), cached for 5 minutes and refreshed in a background thread. A failure is stored and shown, not retried in
a loop. A session that created several PRs is listed under each.

## Working on it

```bash
uv sync                       # Python 3.10+, pytest in the dev group
uv run pytest -q              # ~105 tests, about 100 s; all must pass before a PR
uv run pytest -q tests/test_conversations_prs.py   # one file
uv run once demo              # the loop end to end in a few seconds, no model calls
.venv/bin/once ...            # or `once` (the editable install)
```

- **Tests:** `tests/conftest.py` gives the `env` fixture: an isolated home, settings files and a small git
  repo with a GitHub remote (`github.com/acme/api`). `run_hook(payload, agent)` runs the real hook in a
  subprocess, exactly as an agent does. `payloads(repo, sid)` is a ready-made Claude Code session with a retry
  cluster. Never call the real `gh`, `claude`, `codex` or `agy` from a test: monkeypatch
  `prs.gh_available`, `prs._gh_list`, or pass a fake agent command.
- **New behaviour gets a test**, and a fixed bug gets a regression test (`tests/test_hardening.py` collects
  those).
- **Live checks** with real agents spend real money. Keep them small and say what they cost. Use
  `--max-budget-usd`, and never `--dangerously-skip-permissions` on Antigravity: allow specific commands
  through `--settings` instead.
- **Releasing:** bump the three version strings, `uv lock`, merge, then tag `vX.Y.Z` on `main`. `release.yml`
  checks that the tag matches, runs the tests, builds, makes the GitHub release and the collector image, and
  publishes to PyPI only when the repo variable `PUBLISH_PYPI` is `true`.
- **Docs:** `README.md` is the short introduction that PyPI shows too: keep its links and images absolute
  (github.com / raw.githubusercontent.com URLs). `CHANGELOG.md` is the release history and details: add a section
  for every version. `docs/guide.md` is the user guide. Update both
  when user-facing behaviour changes, and keep "Known gaps" honest about what was verified live and what was
  only tested with stand-ins. Screenshots in `docs/img/` must come from sample data, never a real user's
  sessions.

## Where the plan lives

- The design doc ("once: Architecture Review & Build Plan") is a Claude Doc with a Build status tab. Keep that
  tab in step with what ships.
- Work is tracked in the Notion database "once: build tickets". Close tickets only when the code and a test
  (or a live check) show they're done, and say which ones need the owner at their Mac.
