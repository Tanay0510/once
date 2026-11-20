# once

**Your coding agent makes each mistake once.**

once watches the sessions you run with **Claude Code**, **Codex** and **Antigravity**. When the agent wastes steps
or you correct it, once turns that into a rule for the repository. It checks the rule by replaying the session with
and without it, and gives the rules that work back to your agent next time. Everything stays on your machine.

![A tour of the dashboard: a session timeline, conversations and pull requests](https://raw.githubusercontent.com/Tanay0510/once/main/docs/img/tour.gif)

## Install

```bash
uv tool install once        # or: pipx install once
once demo                   # the whole loop on a toy repo in a few seconds; touches nothing of yours
cd <a git repo you use with an agent>
once init                   # shows what it will change, then connects every agent it finds
```

Python 3.10 or newer, on macOS or Linux. Codex runs new hooks only after you trust them: start `codex`, type
`/hooks`, and trust the ones that run `once.hook codex`.

## What you get

- **Rules from your sessions.** Repeated failures, the files an agent missed, and corrections you type in the chat
  ("no, use tox, not pytest") become suggested rules. Nothing is active until you accept it.
- **Rules tested before you trust them.** `once lab replay` reruns the session that went wrong with and without the
  rule, in throwaway copies of the repo.
- **Rules where the agent will see them.** At session start, and when the agent touches matching files. A rule can
  also be enforced: a reminder after a bad edit, an error the agent must fix, or a refusal before the edit lands.
- **A local dashboard** (`once ui`):
  - every session as a timeline, with wasted steps marked;
  - every conversation across your agents, searchable;
  - the pull requests each session led to;
  - trends, and whether rules help work ship.
- **No commands day to day.** Answer the agent when it asks about a suggestion, and glance at the status line.
  Read the weekly digest by file, notification, Slack or email.
- **Teams.** Share rules through a pull request (`once share`), run a small collector for a team view, and run
  `once pr-check` as a GitHub Action to flag files a pull request probably missed.

|  | Claude Code | Codex | Antigravity |
| --- | --- | --- | --- |
| Sessions recorded | yes | yes | yes |
| Rules given | session start, path rule files | session start, before commands | every turn, glob rule files |
| Enforcement | yes | yes | yes |
| Conversations and PR links | yes | yes | yes |

## Privacy

- **Secrets are masked before anything is written to disk.**
- **Nothing leaves your machine:** sessions, rules and the dashboard (which listens only on `127.0.0.1`) stay
  local.
- **The team collector is optional.** It receives counts, rule wording and outcomes, never prompts, replies, code,
  commands or error text. `once collector push --dry-run` shows exactly what would be sent.
- **To delete sessions:** `once purge`.
- **To remove once:** `once uninstall` removes the hooks and restores your settings exactly.

## Status

Beta. Seen working with real agents:
- **Claude Code:** recording, rules, enforcement and the MCP tools.
- **Antigravity:** recording, rules, enforcement and the MCP tools.
- **Codex:** recording and conversations.

Still being proven:
- Codex enforcement.
- The learning loop at scale.
- Whether rules measurably help on a held-out set of tasks.

once never stops your agent: if anything in it fails, the agent carries on and the error goes to a log.

## More

- [The guide](https://github.com/Tanay0510/once/blob/main/docs/guide.md): every feature and setting.
- [Changelog and details](https://github.com/Tanay0510/once/blob/main/CHANGELOG.md).
- [AGENTS.md](https://github.com/Tanay0510/once/blob/main/AGENTS.md): how the code is laid out, for people and
  agents working on it.

## License

[Business Source License 1.1](https://github.com/Tanay0510/once/blob/main/LICENSE).

- **Free to use, including at work and on commercial code:** with your own agents, on your own or your
  employer's repositories, and for your team.
- **Not allowed:** using it in a product or service you sell that competes with once.
- **Each version becomes Apache-2.0 four years after it is published.**

For other terms, contact the author.
