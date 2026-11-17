# once (v0.8.0)

Your coding agent makes each mistake once. `once` watches your Claude Code, Codex and Antigravity sessions, turns
failures and your corrections into rules for the repository, tests each rule by replaying the session with and
without it, and hands the rules that work back to your agent at the start of the next session.

```bash
once demo        # the whole loop on a bundled toy repo in a few seconds; touches nothing of yours
```

## New in 0.8

Everything the design doc described that 0.7 did not have yet.

```bash
once demo [--real]                       # a failing session, a flag, a rule, a replay, a passing rerun
once teach --level user|owner|stack|agent "..."   # rules above the repo, inherited by every repo below
once rules --all-levels                  # this repo's rules and the ones it inherits
once rules lift <id> stack|owner|user    # move a rule up; a stack rule needs passing replays in two repos
once rules promote <id> [remind|check|block]      # enforcement: needs a detector, and check/block need the
                                                  # agent to have ignored the rule first
once lab bootstrap                       # first suggestions from CI, lint, format and lockfiles; no model
once lab cost [--init-prices]            # tokens, memory overhead, saving and break-even per rule
once lab meta ...                        # the outer loop: the pipeline learns from what became of its rules
once off --session | --capture           # this session only; or stop recording too
once purge [--older-than DAYS]           # delete captured sessions
once last --web                          # the session in the browser
once completion zsh|bash|fish            # tab completion for commands, rule ids and sessions
once undo                                # now also takes back a tag or a promotion
```

Every command takes `--json`, `--quiet` and `--dry-run`, before or after the command name. An unknown flag is
an error now; before, `once off --session` silently paused the whole repo.

**Enforcement levels.** `instruct` is context, as before. `remind` shows the rule right after an edit that matches
its detector. `check` returns such an edit to the agent as an error to fix. `block` refuses the edit or shell
command before it lands. Rules above `instruct` cost no context tokens; they speak only when broken. Claude Code
only; Codex and Antigravity keep `instruct`.

**Codex and Antigravity.** Enforcement works for all three agents now. Codex: block-level rules are refused at
`PreToolUse` (shell and `apply_patch`, matched on the lines a patch adds), check- and remind-level findings come
back from `PostToolUse`. Antigravity: `PreToolUse` answers `allow` (which hands the call to the default policy and
grants nothing) or `deny`; its `PostToolUse` cannot speak to the model, so findings arrive at the next turn.
`once init` registers the MCP server with Codex (`codex mcp add`) and Antigravity (`~/.gemini/config/mcp_config.json`).
Path-scoped rules become Antigravity glob rules in `.agents/rules/once-*.md`; playbooks become skills in
`.agents/skills/once-*/`, which Codex reads too. Both are kept out of git through `.git/info/exclude`. After
upgrading, run `once init` again; Codex asks you to trust its hooks again in `/hooks` because the matcher changed.

**The central collector.** One small server for a team: `once collector serve` (or the `once-collector` container),
then each developer runs `once collector connect <url> --token <t>`. After every session a summary goes to it in
the background: counts, flags by detector, the folders flagged steps touched, rule ids and wording, outcomes,
review decisions. Never prompts, commands, error text or file contents; `once collector push --dry-run` shows
exactly what is sent. The team sees it at `<url>/?token=...` or with `once collector team`. It binds to
127.0.0.1 unless given `--host`; for a team put it behind TLS.

**Scope levels.** A rule can live with you (`_user`), one agent (`_agents/<agent>`), a toolchain
(`_stacks/<stack>`, from lockfiles and manifests) or an organization (`<host>/<owner>/_shared`). The most specific
level wins, a repo rule can name the one it replaces with `overrides: <id>`, and inherited rules get at most 2
of the 8 slots per injection.

**The outer loop (`once lab meta`).** The pipeline is versioned data (`~/.once/memory/_pipeline/`): drafter
wording and examples, detector thresholds and weights, source order, ranking weights. Nothing else, so lint,
redaction, the review gate and the promotion test are out of its reach. `detectors` tunes thresholds from which
flags led to rules you kept, and backtests the change. `benchmark` freezes past decisions; `drafter` tries
variants and keeps one only if it beats the current drafter on held-out episodes (another repo when there is
one). `ranking` fits a policy pooled over rule features from session outcomes, with exploration. `priors` finds
which kinds of rule survive and tells the drafter (lint warns, never blocks). `compare p0,p1` drafts a repo's
new episodes with two versions into one blind review queue; `report` gives the measures from the plan. Every
version is saved, not used, until `once lab meta promote`; `once lab meta rollback` undoes it.

**Dashboard.** Overview tiles show the change from the 30 days before and open the sessions behind them; a weekly
trend of avoidable steps (or verified rate); hotspots open the sessions that touched the folder. Live is pushed
over a server-sent event stream. Rules show how often each was given and ignored, mark stale ones, and can be
edited, promoted or retired. `j`/`k`, `Enter`, `1`-`5` and `w a m g` work as in the terminal; `?` lists them.

## The dashboard (0.7.1, 0.7.2)

`once ui` is now a dark app in the palette of the demo video: a sidebar with a count of rules waiting for review,
sessions drawn as timelines with wasted steps boxed in rose, replay evidence as ✕ and ✓ tiles, and the `once ●`
status line under each session. Fonts are bundled, so it works offline. It also fits a narrow editor sidebar.

New in 0.7.2:

- **Click any step** to see the whole command, the full error text, the files and the time.
- **Make this a rule** on a flagged step, with a first wording filled in from what failed and what worked after.
  The rule remembers the error it targets.
- **What the session changed**: the diff between the snapshots taken at the start and end of the session.
- **Did it work?** on each rule: sessions it was given in, times the agent broke it, and how often its failure
  happened before the rule existed and since.
- **Setup** on Live when nothing is running: the same checks as `once doctor`.

## Renamed from meta

This tool was called `meta` (package `meta-agent`) until 0.6. To move an existing install:

```bash
uv tool uninstall meta-agent
uv tool install --editable ~/Desktop/once      # wherever you unzipped it
cd <your repo> && once init
```

`once init` moves `~/.meta-agent` to `~/.once`, removes the old hooks, status line, slash commands,
MCP server and generated files, and installs the new ones. Your rules and sessions come along.

## Install and try (about five minutes)

```bash
uv tool install .            # or: pipx install .   /   pip install -e .
cd <a git repo you use with Claude Code>
once init                    # shows a plan, installs hooks on one confirmation
once teach "always run the formatter before finishing"
claude                       # work as usual; the rule is given at session start
once                         # status and the one next step
once last                    # the session as a timeline, flagged steps marked with !
once tag 4-8 missing "what it should have done"
once why                     # which rules the last session got, and why
```

New in v0.2:

```bash
once ui                                   # Live, Sessions (with tagging), Overview, Rules, in the browser
once playbooks new add-endpoint "Use when adding an HTTP endpoint"
once playbooks sync                       # publishes playbooks to Claude Code as skills
once lab replay <rule-id> --prompt "the task that went wrong" --signature "regex of the error"
```

New in v0.3, the task miner (needs the GitHub CLI signed in, or `GITHUB_TOKEN`):

```bash
once lab tasks mine                 # merged PRs that link an issue and change tests become tasks
once lab tasks validate             # keeps tasks whose check fails before the fix and passes after
once lab tasks                      # list them, with the train / held-out split by merge date
once lab run <pr-number>            # run the agent on one task; the session shows up in `once last`
once lab run <pr-number> --with-rules --model <cheaper-model>
```

New in v0.4, the learning pipeline and the experiment:

```bash
once lab sweep                       # every valid train task once; shows failures and files the agent missed
once learn                           # drafts suggested rules from tags, repeated failures, missed files, review comments
once review                          # accept, edit or reject each suggestion; nothing is active before this
once lab stats                       # how suggestions fare, by source
once lab cochange                    # files that change together, as raw material for playbooks
once lab exp baseline generated      # prepare a baseline arm (also: native, experimental)
once lab exp run --arms bare,rules,generated --repeats 3 --max-cost 20
once lab exp report                  # verified rate with intervals, task-by-task comparison, cost per verified task
```

`once learn` calls a model through `claude -p` once per suggestion that needs wording (about a few cents each)
and asks first. Suggestions from missed files need no model. In Claude Code, `/once-teach`, `/once-flag` and
`/once-why` now exist as slash commands.

New in v0.5:

```bash
once init                            # now also connects Antigravity (agy) when it is installed
once lab regress                     # rerun every rule's replay: needed, obsolete, broken or harmful
once lab regress --agent antigravity # the same rules on another agent: how many transfer
once lab replay|run|sweep|exp ... --agent antigravity
once rules detect <id> "<regex>"     # flag edits that go against a rule the agent was given
once learn --replay                  # replay each new suggestion on the session it came from
once import                          # bring in sessions Claude Code already recorded (experimental)
once export -o CONVENTIONS.md        # active rules as a plain file, with replay results
once lab cochange --write            # draft playbooks from files that change together
once config                          # auto_learn, daily_cost_cap, quiet, snapshots, holdout_fraction
```

Inside Claude Code you can now say "flag that as wrong: it should have used X" and the agent records
it on the session through the `record_feedback` tool.

**Replay spends money and is not sandboxed.** It runs the agent headless 6 times by default (3 without
the rule, 3 with), each in a throwaway copy of the repo with no history, with edit and shell access on
your machine. It prints the plan and the spend ceiling and asks before running. `--dry-run` shows the plan only.

Leave at any time: `once off` pauses, `once undo` reverses the last teach or drop,
`once uninstall` removes the hooks and generated files. `ONCE_DISABLE=1` makes every hook a no-op.

## Codex (0.7)

`once init` finds `codex` on your PATH and adds six hooks to `~/.codex/hooks.json` (SessionStart,
UserPromptSubmit, PreToolUse for shell commands, PostToolUse, Stop, SessionEnd), keeping any hooks you
already have. It also registers the `once` MCP server with `codex mcp add`, so your agent can record your
answers about suggested rules.

**One manual step:** Codex runs a new hook only after you approve it. Start `codex`, type `/hooks`, and trust
the hooks that run `once.hook codex`. Until you do, Codex sessions are not recorded; `once doctor` tells you.
The hook command stays the same across updates, so you approve it once.

Needs Codex 0.124 or newer (hooks became stable there). Rules arrive at session start and before shell
commands that touch matching files. Edits made with `apply_patch` are recorded from the patch headers.
Lab runs work with `--agent codex` (`codex exec --json`), but Codex reports no cost, so `--max-cost` cannot
stop a Codex sweep; set a run count instead. Written from the Codex hooks reference and not yet run against a
real `codex`.

## Without typing a command (0.6)

After `once init`, the daily path needs no command:

- **Tell your agent.** When you correct it in the chat ("no, use tox instead of pytest", "remember that we never
  edit generated files"), `once` keeps that as a suggested rule. Switch off with `once config teach_by_talking false`.
- **Answer in the chat.** At the start of the next session your agent asks you about up to two suggestions,
  at most twice each, and records your yes, no or wording through the `review_rule` tool.
  Say "remember to ..." and the agent saves it at once through `remember_rule`.
- **Glance at the status line.** The bottom of Claude Code shows `once ● 2 of 5 rules in use · 1 to review ·
  2 flagged`. A status line you already had keeps running, first.
- **A line when the session ends**, only if something was flagged or noted.
- **Notifications** only when rules are waiting after background drafting, and for the weekly digest.
- **Weekly digest** to `~/.once/digests/`, a notification, and Slack if you set
  `once config slack_webhook https://hooks.slack.com/...`. `once digest` shows it any time.
- **Menu bar** (macOS, needs the free SwiftBar or xbar): `once menubar --install`.
- **Editor sidebar** for VS Code, Cursor or Windsurf: `once editor install`.
- **Team**: `once share` opens a pull request that adds your rules to `.once/rules/` (built in a separate
  worktree; your checkout is not touched). Everyone who uses `once` in the repo gets them when it merges.
  `once pr-check [number] [--post]` lists files a pull request probably missed, from the repo's history.

## What it touches

| Where | What |
| --- | --- |
| `~/.claude/settings.json` | Six hook entries, marked `once.hook`. A one-time backup is kept beside it. |
| Claude Code MCP config | One user-scope server named `once`, added with `claude mcp add`. |
| `~/.once/memory/` | Your rules, as YAML, in a git repo you own. One folder per project. |
| `~/.once/local/` | Captured sessions (secrets masked before writing), the SQLite index, hook error log. |
| `<repo>/.claude/rules/once/` | Generated files for path-scoped rules. Ignored through `.git/info/exclude`. |
| `<repo>/.claude/skills/once-*/` | Generated skills, one per playbook. Ignored the same way. |
| `~/.claude/settings.json` `statusLine` | The `once` status line; any previous one is kept and run first. |
| SwiftBar or xbar plugin folder | `once.1m.sh`, only after `once menubar --install`. |
| `~/.vscode/extensions/` (or Cursor, Windsurf) | The sidebar extension, only after `once editor install`. |

No tracked file in your repo is ever written, except by `once share`, which does it on a new branch for a
pull request you review.

## Development and releases

```bash
uv sync                  # Python 3.10 or newer, pytest included
uv run pytest -q         # 81 tests, about a minute
uv run once demo         # the loop end to end
```

CI (`.github/workflows/ci.yml`) runs the tests on Linux and macOS with Python 3.10, 3.12 and 3.13, builds the
wheel, checks it carries its assets and runs `once demo` from it. A tag such as `v0.8.0`, matching the version in
`pyproject.toml`, runs `release.yml`: tests, a GitHub release with the wheel and sdist, the collector image on
`ghcr.io/<owner>/once-collector`, and PyPI when the repository variable `PUBLISH_PYPI` is `true` and PyPI trusted
publishing is set up.

## Commands

`demo` `init` `doctor [--fix|--data]` `status` (the default) `last [n] [--web]` `tag` `teach [--path glob] [--level]`
`why [file]` `rules [show|drop|detect|promote|lift <id>] [--all-levels]` `playbooks [new|show|sync]` `ui`
`learn` `review` `off|on [--session|--capture|--everywhere]` `undo` `purge` `uninstall` `digest` `share`
`pr-check` `collector [serve|connect|push|status|team|disconnect]` `menubar` `editor` `statusline` `completion`, and `lab tasks|run|sweep|replay|exp|regress|cochange|stats|
bootstrap|meta|cost`. Every command takes `--json`, `--quiet` and `--dry-run`.

## Layout

```
src/once/
  hook.py              fail-open entry point the agents call; no yaml, no sqlite
  adapters/claude_code.py   Claude Code: install, parse, reply, native rule files, skills, slash commands
  adapters/antigravity.py   Antigravity CLI: hooks.json group, camelCase payloads, per-turn injection
  index.py             precompiled rule index, selection, budget, rendering
  rules.py             rule files, lint, commit to the memory repo
  redact.py            secret masking
  detectors.py         retry cluster, failed command, repeated read
  store.py             SQLite view over the JSONL trajectories
  repo.py              repo identity, CI check-command detection
  mcp_server.py        stdio MCP server: playbooks, rules, feedback, review_rule, remember_rule
  ambient.py           chat corrections, asking in the agent, status line, notifications, menu bar, digest
  team.py              share rules by pull request; pull request check from co-change history
  editor/vscode/       the VS Code / Cursor extension (plain JavaScript, no build step)
  playbooks.py         playbook files
  replay.py            with and without runs in throwaway copies; writes evidence into the rule
  tasks.py             task miner, validation, time split, missed-files check
  experiment.py        task runs, arms, sweeps, resumable experiments, statistics, baselines
  learn.py             slicer, drafter, candidate queue, review decisions, pipeline stats
  regress.py           rules as a regression suite, per agent
  snapshot.py          tree snapshots into private refs
  importer.py          import of existing Claude Code transcripts
  ui.py                the local web viewer, server-rendered, bound to 127.0.0.1
  pipeline.py          the learning pipeline as versioned data: what the outer loop may tune
  meta.py              the outer loop: detectors, drafter, ranking, priors, blind comparison, report
  bootstrap.py         cold-start mining from CI, lint, format and lockfiles
  cost.py              tokens, prices, memory overhead, saving and break-even per rule
  demo.py              `once demo`, with a scripted agent and drafter in demo_assets/
  collector.py         the team's central collector: server, client and team dashboard
  cli.py               the commands
tests/                 81 tests, including full simulated sessions through the real hook
```

## Known gaps

Verified on a real machine: install, capture, rules at session start, failure detection, `teach`, `last`,
`tag`, `why`, `doctor`, replay, mining, validation and one `lab run`.

Built and tested only with stand-ins, not yet run for real:
- Everything in "Without typing once": the status line in a real Claude Code, the agent asking about
  suggestions and calling `review_rule`, SwiftBar, the editor extension (never loaded in a real editor),
  Slack, and `once share` against GitHub (tested with a local remote and a stand-in for `gh`).
- Everything for Antigravity. The adapter was written from the published hooks reference and has never
  talked to a real `agy`. Its headless command (`agy --output-format json --mode accept-edits -p ...`) is a
  best guess; override it with `--agent-cmd` if it does not fit your version.
- `once learn` drafting, `once lab sweep`, `once lab exp`, `once lab regress`, both baseline arms.
  The first real sweep and learn ended with the agent doing no work; 0.4.1 reports the reason.
- Token capture, the session-end line, slash commands, the MCP server in a live session, skills,
  tree snapshots, transcript import, the ignored-rule detector, automatic drafting after sessions.

Built in 0.8 and tested with stand-ins only: everything under "New in 0.8". The enforcement replies follow the
Claude Code hooks reference (`permissionDecision: deny` before a tool call, `decision: block` after one) and have
not been seen working in a live session. The outer loop has run only on synthetic history; its numbers mean
something after weeks of real decisions, and the phase 5 claim (a new repo learns faster) stays unproven until
`once lab meta compare` has run on a third repo.

Not built:
- A container sandbox. It needs the agent signed in inside the container, which cannot be set up blind.
- Team review of rule changes beyond `once share`; the collector reports, it does not distribute rules.
- Cold-start sources that need the hosting API: failed CI runs on merged pull requests, and code owners as
  pointer rules. Review comments are already mined with tasks.
- A lint rule written into the repo's own check command as the `block` level; `block` uses the agent's hook.
- Light mode for the dashboard (it is dark by design since 0.7.1).
