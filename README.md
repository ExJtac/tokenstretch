# tokenstretch

**Stop paying for wasted AI coding credits. One command finds the waste, explains it, and fixes it, with undo.**

tokenstretch is **free and open source (MIT)**. It runs **100% on your machine**. There's no account, no sign-up, no telemetry, and no server. It looks at how Claude Code, Codex, and other coding agents are set up on your computer, shows you in plain English where your credits are going, and applies a small set of safe fixes. It backs up every file first, and `tokenstretch undo` puts everything back.

It doesn't replace your tools. It checks, configures, and measures them, including RTK and Caveman.

## What you get

- **A plain-English checkup.** `doctor` runs 40+ local checks: the top model set as your everyday default, giant sessions that re-read hundreds of thousands of tokens on every step, duplicate instruction files you pay for twice, telemetry quietly left on, proxy chains, and setups that risk your provider's terms.
- **Fixes it applies for you.** `setup` explains each change (what it is, why it costs you, which file and key it touches, and how to undo it), asks once, then applies it with live progress.
- **A live view.** `status` shows what's applied, which model each tool defaults to, and how much history your newest sessions re-read on every step. It flags sessions over ~150K tokens with a "`/clear` at the next task break" tip.
- **A live watch.** `watch` runs quietly in a terminal and warns you, once per problem, when a session's history gets too big, the top model is doing simple steps, the same file keeps getting re-read, or history is growing fast. Each warning says what's wrong, why it costs credits, and the one-line fix.
- **A model picker.** `model suggest "<task>"` recommends light, mid, or top for a task and names the model for each tool you have. `model default --tier mid` changes your default model, with a backup and undo.
- **A shareable report.** `report` writes a single self-contained HTML file with the findings, the plan, what was applied, undo commands, and current status. It loads nothing from the internet.
- **Real measurements, never guesses.** `bench` compares before and after on a throwaway copy of your repo, using your agent's own usage logs. If there's no data, it says `no measurement`.
- **Full undo.** Every changed file is backed up under `~/.tokenstretch/backups/`, and each change is recorded in a manifest.

## 30-second quickstart

```bash
pip install .                # from a clone of this repo; Python 3.11+, no dependencies
cd ~/your-project
tokenstretch setup           # explains the plan, asks [y/N], applies with backups
tokenstretch status          # what's on now, plus your newest sessions
tokenstretch report          # writes a local HTML report and prints its path
tokenstretch watch           # live warnings while you work (Ctrl-C to stop)
tokenstretch model suggest "fix the flaky login test"
tokenstretch undo            # restores everything the last setup changed
```

`python -m tokenstretch setup` works too. With no terminal to ask in (CI, pipes), `setup` is a **dry run** unless you pass `--yes`. Use `--dry-run` to preview anywhere.

Sample (trimmed):

```
Plan (3 changes, all local, every file backed up first):
  2. Make sonnet the default Claude Code model
     Why: Your default is opus (from ~/.claude/settings.json). The top model costs several times more per step. ...
     Changes: edit ~/.claude/settings.json (key: model; model: opus -> sonnet)
     Undo: `tokenstretch undo`
     Note: For hard tracing or design work, type `/model opus` in Claude Code, then `/model sonnet` when done.
Apply these 3 change(s)? [y/N] y
✔ switched default model to sonnet, backed up ~/.claude/settings.json → ~/.tokenstretch/backups/…/home/.claude/settings.json
```

## What setup changes, and how to undo it

Setup only proposes a change when it applies to your machine, and it always shows the exact file and key first.

| Change | File | Why |
|---|---|---|
| Default Claude Code model → `sonnet` (only if the default is Opus/Fable or unset) | `~/.claude/settings.json` (or the project settings file that sets `model`), key `model` | The top model costs several times more per step. You can still use it for hard work with `/model opus`. |
| 5 short credit-saving rules: start each task fresh, read narrowly, don't re-read, run subagents on the mid-tier model, keep replies short | project `CLAUDE.md` (and `AGENTS.md` if Codex is installed), in a marked block | Long sessions re-read their whole history on every step. |
| Caveman telemetry off | `~/.caveman/cloud.json`, key `telemetry.enabled` | The Caveman CLI reports usage, including your IP, by default. |
| RTK telemetry back off (only if someone turned it on) | `~/.config/rtk/config.toml`, key `telemetry` | Privacy. |
| *Optional, asked separately because it downloads:* pinned installs of RTK OSS v1.2.0 (`RTK_EDITION=oss`, commit `e288a18`) and the Caveman **skill only** v4.1.0 (commit `1e91f13`) | pinned in `tokenstretch/catalog.py` | Shell-output compression and terse replies, from pinned, hash-checked sources. |

To undo, run **`tokenstretch undo`**. It restores every file from the latest setup run, newest step first, and deletes files that setup created. If you've edited a file since then, undo stops and tells you, unless you pass `--force`. To undo a single step, pass that step's manifest from `~/.tokenstretch/manifests/`.

Shell rc files are never edited. If `ANTHROPIC_MODEL` is set in your shell, setup tells you rather than changing it.

## Watch while you work

`tokenstretch watch` re-reads your local session logs every 10 seconds (`--interval`) and prints a warning the first time a session shows one of these. It never repeats a warning for the same session.

| Warning | Why it costs credits | Fix it suggests |
|---|---|---|
| History over 150K tokens (`--max-context`) | Every step re-sends the whole conversation. | `/clear` (fresh start) or `/compact` |
| Top model on the last 3 simple steps (short replies, few tool calls) | The top model costs several times more per step. | `/model sonnet` for routine steps |
| The same file read 3+ times (`--max-reads`) | Each re-read is added to history and re-sent. | Ask for line ranges or a note; start fresh |
| History grew 60K+ tokens in 5 minutes | Big reads or noisy output pile up fast. | Read narrowly, trim output |

```
⚠ [claude-code abcd1234 · -Users-me-app] history is ~200K tokens (over 150K).
   Why it costs credits: every step re-sends the whole conversation, so each reply now costs at least that much input.
   Fix: finish this step, then type /clear (fresh start) or /compact (keeps a short summary).
```

It's read-only and only looks at token counts, model names, tool-call counts, and the paths of files the agent read (never their contents). `--once` does a single pass. `--notify` also pops a desktop notification through your OS's own `osascript` or `notify-send`, if present. Claude Code is fully supported. Codex gets the history, top-model, and growth warnings (its logs don't record file reads). Other tools say "not supported yet".

## Pick the right model

```
$ tokenstretch model suggest "Debug the race condition in the websocket reconnect logic"
Suggested tier: TOP: hard reasoning across many parts; worth the top model for this task only.
Why: +2 mentions concurrency/leak bug (score +2).

  Claude Code: opus  (switch now: `/model opus`)
```

`model suggest` scores the task with simple rules you can read in `tokenstretch/models.py`: words like architecture, broad refactor, migration, race condition, security, or root cause push toward **top**; rename, typo, formatting, docs, or a version bump push toward **light**; tests and small fixes lean **mid**; naming many files or a long description pushes up. It makes no model calls. **It's a rule of thumb, not a measured result**, so move up a tier if the answer comes back wrong. `--file prompt.txt` reads the task from a file and `--json` prints machine-readable output.

`tokenstretch model default --tier mid` shows the change to your default model (Claude Code: `model` in `settings.json`). Add `--apply` to make it. The file is backed up first and `tokenstretch undo` restores it. `--tool claude` limits it to one tool. Codex has no tier map yet, so the picker tells you to use `/model` there.

## Honest results

We only publish numbers from real bench runs.

**A large real-world web app repo (Claude Code on a subscription, 3 runs each side, same prompt):**

| | Claude-reported cost (3 runs) | Model |
|---|---|---|
| Before | $3.18 | top-tier model |
| After | $0.33 | mid-tier model + tokenstretch project files |

- That's about **90% lower Claude-reported cost**, and **nearly all of it came from the model switch**, not from the other files.
- **Quality trade-off:** all 3 "before" answers traced the right feature. 2 of the 3 cheaper answers were correct, with less depth. **1 of the 3 traced the wrong feature** and said it was unsure. So we recommend the mid-tier model for everyday work and the top model for hard tracing and design, which is exactly what setup's note tells you.
- An earlier bench on a small repo, with no model switch, was a **tie**. The project files alone didn't measurably change the history re-read.

Your mileage will differ. Run `tokenstretch bench` on your own repo (see `LIVE-BENCH.md`).

## Privacy promises

- **No telemetry, no analytics, no phone-home, no update checks.** The package imports no network libraries, and that's checked in the test suite.
- **No network calls**, except the pinned installs you confirm in a separate prompt.
- **Never opens credential files.** Login files and tokens are only `stat`'d (checked for existence and permissions), never read, copied, symlinked, or proxied. Tests plant canary credentials and fail if any are opened.
- **Your prompts, code, and logs never leave your machine because of this tool.** Session logs are read locally, only to show model and history size.
- **Every file change is backed up**, and `undo` restores it.
- **Checkable:** the whole test suite also runs with networking disabled (`unshare -rn`).

## Supported tools

| Tier | Tools | What works |
|---|---|---|
| 1 (full) | **Claude Code**, **Codex** | doctor, setup, status, watch, model suggest, bench, pinned installs, undo (`model default`: Claude Code) |
| Doctor-only | Gemini CLI, Copilot CLI, Cursor, Aider, OpenCode, Cline | doctor checks and privacy listing |
| Planned | Goose, Devin (see `ROADMAP.md`) | |

Every tool-specific path lives in its adapter (`tokenstretch/adapters/`). To add one, see `CONTRIBUTING.md` and `ADAPTERS.md`.

## FAQ

**Does it replace RTK or Caveman?** No. It checks, configures, and verifies them. It installs the safe editions from pinned sources (RTK's OSS edition, Caveman as a skill only), keeps their telemetry off, warns when two compressors or proxies stack up, and lets you measure whether they help on your repo. It also checks the token savers you already have (RTK, Caveman, TokenEconomizer, ccusage, Headroom, Kompressor, and similar) to confirm they're set up right and actually working. On one real machine, for example, it found Caveman's telemetry on and RTK's commands being blocked by Claude's permissions.

**Will it slow my agent down or cut work?** No. The biggest win is choosing the right model for the job and not re-reading old history. The top model is one `/model` command away whenever you need it.

**Does it change anything without asking?** No. Doctor, status, report, watch, and model suggest are read-only. `model default` is a dry run unless you pass `--apply`. Setup asks once, and it's a dry run without a terminal unless you pass `--yes`.

**Does it touch my login or API keys?** Never.

**Is it really free?** Yes. It's MIT licensed, with no paid tier, no account, and no server.

**What's the command exit code for doctor?** 0 = clean or info only, 1 = waste findings, 2 = a terms-of-service or privacy high.

## More

- All commands: `tokenstretch --help`. The others are `doctor`, `install`, `bench`, `watch`, `model`, and `undo`.
- Technical details and pins: `docs/DETAILS.md`, `ADAPTERS.md`, `tokenstretch/catalog.py`
- Contributing: `CONTRIBUTING.md` · Roadmap: `ROADMAP.md` · Security: `SECURITY.md`

MIT © 2026 Leverage Lab AI
