# tokenstretch

**Stop paying for wasted AI coding credits. One command finds the waste, explains it, and fixes it, with undo.**

## Support tokenstretch

tokenstretch is free and stays free, with no paid tier and no tracking. If it saves you credits and you'd like to say thanks: [buy me a coffee on Ko-fi](https://ko-fi.com/exjtac), or send Bitcoin (on-chain) to `bc1qt892nv80l50grylqddnzf9u4203gm2gqmr22ku`. The QR code is [at the bottom](#say-thanks).

## What tokenstretch does

**Finds the waste**
- Runs 40+ checks on your own computer and explains each problem in plain words.
- Shows what every session re-reads on each step, and which files cost the most.

**Fixes it, with your yes**
- `tokenstretch start` lists every change, asks once, then applies the safe ones.
- `tokenstretch undo` puts every changed file back.

**Runs by itself after setup (Claude Code)**
- Long sessions are summarized automatically at about 200K tokens.
- The summary is saved as a note, and the work carries on from it without you typing anything.
- A new chat picks the task up from the newest note.
- Your plan's weekly use is read automatically (Claude Code in a terminal), so it can tell you if you are on track.

**A dashboard in your browser**
- `tokenstretch dashboard` opens one live page on your own machine: checkup results, live sessions, warnings, weekly pace, team queue and more. It refreshes every 5 seconds. See [Dashboard](#dashboard).

**Turns long instructions into skills (Claude Code)**
- `tokenstretch trim --to-skills` moves step-by-step sections out of your CLAUDE.md into skills, leaving a one-line pointer, so that text is no longer resent on every turn. It shows the plan first and changes nothing until you pass `--apply`. See [Trim instruction files](#trim-instruction-files).

**Also included**
- Live warnings while you work, a status-bar line, a model picker, a task brief writer, a team queue and weekly reports. Full list: [What you get](#what-you-get).

**Private by design**
- No account, no telemetry, nothing leaves your machine.

## Quick start

tokenstretch finds wasted AI coding credits on your computer and fixes the safe causes. It runs only on your machine, has no account and no telemetry, and every change can be undone.

**1. Install.** One line, for macOS or Linux with Python 3.11 or newer:

```bash
curl -fsSL https://raw.githubusercontent.com/ExJtac/tokenstretch/main/scripts/install.sh | sh
```

If it ends by saying `~/.local/bin` is not on your PATH, add the `export` line it prints to your shell startup file, then open a new terminal. Or install with the line below instead, and the installer adds it for you, with a backup:

```bash
curl -fsSL https://raw.githubusercontent.com/ExJtac/tokenstretch/main/scripts/install.sh | sh -s -- --add-to-path
```

**2. Set up.** In a terminal, go to your project folder and run:

```bash
tokenstretch start
```

It checks your computer, lists every change in plain words, and asks once. Nothing changes until you answer `y`.

`start` is in versions newer than 0.5.1. If you see `invalid choice: 'start'`, run `tokenstretch --version`: 0.5.1 or older does not have it yet, and `tokenstretch setup` applies the fixes that version has.

**3. Undo.** `tokenstretch undo` puts back every file that step changed. `tokenstretch pace auto --off` switches off the automatic plan readings, which are not a file change.

More: [what it does](#what-you-get), [install options](#install), [what `start` does](#what-tokenstretch-start-does), [what setup changes](#what-setup-changes-and-how-to-undo-it).

## Let your AI install it

In your coding assistant, say "Install tokenstretch from the GitHub repo", then paste this:

```text
Install and set up tokenstretch for me. Do these steps in order.

1. Check that Python 3.11 or newer is installed (`python3 --version`). If it is missing or older, tell me in plain words how to get it (https://www.python.org/downloads/) and stop until I say it is installed. Do not install it yourself.
2. Open https://github.com/ExJtac/tokenstretch and read the README. Find the install command in the Install section and run it exactly as written. Do not change the link.
3. Run `tokenstretch --version` and tell me what it prints. If it prints 0.5.1 or older, that version has no `start` command: tell me so, and use `tokenstretch setup` in steps 4 and 7 (with `--yes` in step 7). If the command is not found, the installer printed a line about PATH: tell me what it says, and run `~/.local/bin/tokenstretch` by its full path for the next steps. Do not edit my shell startup files; tell me the line to add instead.
4. In the folder of my main project (ask me which one if you do not know), run `tokenstretch start` with no flags. This changes nothing. It lists what it would change.
5. Explain each listed change to me in plain words: what it is, which file, and why. Tell me that `tokenstretch undo` and `tokenstretch pace auto --off` reverse the changes.
6. Ask me yes or no, and wait for my answer.
7. Only if I say yes, run `tokenstretch start --yes`. If I say no, stop here and change nothing.
8. Tell me what changed, what `tokenstretch start` said about my plan, and the undo commands.

Rules: Never enter, ask for, or print a password, API key or token. Do not use sudo. If any step fails, stop, show me the error, and say which step failed. Do not try workarounds.
```

Limits, so nothing surprises you:
- It needs a coding assistant that can run commands on your computer, such as Claude Code or Codex. A chat-only assistant cannot do this; it can only tell you the commands to type yourself.
- The installer is for macOS and Linux. Windows has no installer yet.
- Automatic plan readings (`pace auto`) arrive only from Claude Code in a terminal, not from the desktop app.

tokenstretch is **free and open source (MIT)**. It runs **100% on your machine**. There's no account, no sign-up, no telemetry, and no server. It looks at how Claude Code, Codex, and other coding agents are set up on your computer, shows you in plain English where your credits are going, and applies a small set of safe fixes. It backs up every file first, and `tokenstretch undo` puts everything back.

It doesn't replace your tools. It checks, configures, and measures them, including RTK and Caveman.

![The tokenstretch dashboard: live sessions, warnings, model usage, and weekly trend](docs/images/dashboard.png)

<sub>The local dashboard (`tokenstretch dashboard`), shown with sample data.</sub>

## What you get

- **A plain-English checkup.** `doctor` runs 40+ local checks: the top model set as your everyday default, giant sessions that re-read hundreds of thousands of tokens on every step, duplicate instruction files you pay for twice, telemetry quietly left on, proxy chains, and setups that risk your provider's terms.
- **Fixes it applies for you.** `setup` explains each change (what it is, why it costs you, which file and key it touches, and how to undo it), asks once, then applies it with live progress.
- **A painless fresh start.** `/handoff` has the agent write a short note before you `/clear`, and `/handoff resume` picks the task up in the new session, so clearing a long session no longer costs you the context.
- **A live view.** `status` shows what's applied, which model each tool defaults to, and how much history your newest sessions re-read on every step. It flags sessions over ~150K tokens with a "`/clear` at the next task break" tip.
- **A live watch.** `watch` runs quietly in a terminal and warns you, once per problem, when a session's history gets too big, the top model is doing simple steps, the same file keeps getting re-read, history is growing fast, a conversation or its helpers ran long at a top reasoning effort, or a pause let the saved-context discount expire. On a dearer model the size limits come down with the price. Each warning says what's wrong, why it costs credits, and the one-line fix.
- **A status-bar line.** `statusline` shows the current session's history size and the most urgent warning inside your tool's own status bar, so you don't need a second terminal.
- **An instruction-file trimmer.** `trim` lists every instruction file your agents resend on each turn (CLAUDE.md, AGENTS.md, GEMINI.md, copilot-instructions.md, and their imports) with approx. token counts, finds duplicated rules, oversized files, pasted logs and code, and stale paths, and proposes a trimmed version that only makes safe mechanical changes. It's a dry run unless you pass `--apply`, and `undo` restores it.
- **A brief writer.** `brief "<task>"` turns a one-line task into a brief that names the files, the notes to work from, what not to do again, the stop condition, and the reply length, and lists what your task leaves open.
- **A model and effort picker.** `model suggest "<task>"` recommends light, mid, or top for a task, a reasoning effort to go with it, and names the model for each tool you have. `model default --tier mid` changes your default model, with a backup and undo.
- **A price view per model and effort.** `model cost` weighs your recent sessions' tokens by published price ratios and shows, for each model and effort, where the credits went (history re-read, history saved again, new content, output and thinking) and the one change that removes the most. `model cost --grid` puts the average cost of a step side by side.
- **A team paced through the week.** `queue` hands agents one task per fresh session from a file-based queue, `startup --agent` shows what a worker session can switch off, `model rework` estimates what redo work cost per model and effort, and `pace` holds the team to an even burn of the plan's weekly limit and says how many agents can run at once. See [Run a team at full effort](#run-a-team-at-full-effort) and [Make a week last at full effort](#make-a-week-last-at-full-effort).
- **A shareable report.** `report` writes a single self-contained HTML file with the findings, the plan, what was applied, undo commands, and current status. It loads nothing from the internet.
- **A weekly report.** `report --weekly` adds up your local usage logs per week: tokens by type and by model, your biggest session histories, waste spots, and a before/after view since setup. Cost appears only where your logs report it.
- **A local dashboard.** `dashboard` opens one live page on your own machine with all of the above, updated every 5 seconds. See [Dashboard](#dashboard).
- **Real measurements, never guesses.** `bench` compares before and after on a throwaway copy of your repo, using your agent's own usage logs. If there's no data, it says `no measurement`.
- **Full undo.** Every changed file is backed up under `~/.tokenstretch/backups/`, and each change is recorded in a manifest.

## Install

One line, for macOS or Linux with Python 3.11 or newer:

```bash
curl -fsSL https://raw.githubusercontent.com/ExJtac/tokenstretch/main/scripts/install.sh | sh
```

Or inspect it first (recommended): download it, read it, preview it, and then run it:

```bash
curl -fsSLo install.sh https://raw.githubusercontent.com/ExJtac/tokenstretch/main/scripts/install.sh
less install.sh                 # the pinned tag and commit are at the top
sh install.sh --dry-run         # prints every step; downloads and changes nothing
sh install.sh
```

What the installer does:
- It installs one pinned release, never "latest". The tag and commit are written at the top of the script. With git, it clones the pinned tag and refuses if the tag doesn't point at the pinned commit. Without git, it downloads that commit's tarball and refuses unless its SHA-256 matches the pinned hash. That fetch is its only network use.
- It installs into its own folder, `~/.local/share/tokenstretch`, with a private venv made by `python -m venv --without-pip`. tokenstretch has no dependencies, so there's no pip and nothing from PyPI.
- It adds a `tokenstretch` launcher in `~/.local/bin`.
- It never edits your shell startup files unless you pass `--add-to-path`, and it backs them up first when you do.
- It never runs `setup` or installs other tools. There's no telemetry and no install ping.

`sh install.sh --uninstall` removes exactly what it created: the folder, the launcher, and the PATH line. Your `~/.tokenstretch` backups stay, so run `tokenstretch undo` first if you want setup's changes reverted.

Options: `--add-to-path`, `--python PATH`, `--data-dir DIR`, `--bin-dir DIR`, `--dry-run`, `--uninstall` (`sh install.sh --help` lists them). For an install link that can never change, use the commit link in the release notes instead of `main`. The installer doesn't use pipx, because pipx would download build tools from PyPI. To install from a clone instead, see the quickstart.

## What `tokenstretch start` does

`start` is the one command for someone who just wants it set up. Run with no flags it changes nothing.

1. It checks the computer: the Python version, which supported coding tools it finds, whether the setup steps are already in place, and whether automatic plan readings are on.
2. It lists the changes it would make, one plain line each: the steps `tokenstretch setup` applies by its safe defaults (see [What setup changes](#what-setup-changes-and-how-to-undo-it)), plus switching on [`pace auto`](#make-a-week-last-at-full-effort) when Claude Code is found. It never runs the optional pinned installs, and it downloads nothing.
3. With a person at the terminal it asks `Apply these changes? [y/N]`. Without one (a pipe, CI, an AI assistant's shell) it prints the list and the exact command to apply, `tokenstretch start --yes`, and changes nothing.
4. After applying, or when everything is already in place, it prints up to three plain lines: how much of the plan's week is used, whether that is on track, too fast or with room to spare, and one thing to do next. With no reading yet it says where one comes from.

Running it twice is safe: the second run says it is already set up and changes nothing.

`tokenstretch undo` restores every file the setup steps changed and deletes the files they created. The `pace auto` switch is not a file edit, so it has its own off switch: `tokenstretch pace auto --off`.

| Flag | Effect |
|---|---|
| `--yes`, `-y` | Apply without asking. |
| `--detail` | Also print the full `tokenstretch pace` output. |
| `--json` | Print one JSON result (checks, planned changes, whether anything was applied, the plan summary). It never asks and changes nothing without `--yes`. |
| `--home`, `--root`, `--plan` | Same meaning as in `setup`. |

Exit codes: `0` when it finished, including when nothing was changed or you answered no. `1` on a real failure: Python older than 3.11, or a step failed while applying (the message says how to undo what was already done). `2` for a flag it does not know.

## 30-second quickstart

```bash
pip install .                # from a clone of this repo; Python 3.11+, no dependencies
cd ~/your-project
tokenstretch setup           # explains the plan, asks [y/N], applies with backups
tokenstretch setup --list    # the steps available here, each with its id (changes nothing)
tokenstretch setup --only claude-credits-skill   # apply one step; --skip <id> leaves one out
tokenstretch status          # what's on now, plus your newest sessions
tokenstretch report          # writes a local HTML report and prints its path
tokenstretch watch           # live warnings while you work (Ctrl-C to stop)
tokenstretch statusline      # the same checks as one line, for your tool's status bar
tokenstretch dashboard       # live local page in your browser (Ctrl-C to stop)
tokenstretch report --weekly # weekly usage roll-up as a local HTML file
tokenstretch model suggest "fix the flaky login test"
tokenstretch trim            # what your instruction files cost per turn, and a safe trim (dry run)
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

You don't have to take the whole plan. Each step has an id, shown in [brackets] in the plan and by `tokenstretch setup --list`. `setup --only <id>` applies that one step and `setup --skip <id>` leaves one out; both take several ids, repeated or separated by commas. `--only` skips the checkup summary, so it is quick. A step that would start a new file in your project (a `CLAUDE.md` or `AGENTS.md` that doesn't exist yet) is optional: the plan lists it, and it's applied only when you ask for it with `--only`.

| Change | File | Why |
|---|---|---|
| Default Claude Code model → `sonnet` (only if the default is Opus/Fable or unset) | `~/.claude/settings.json` (or the project settings file that sets `model`), key `model` | The top model costs several times more per step. You can still use it for hard work with `/model opus`. |
| 5 short credit-saving rules: start each task fresh, read narrowly, don't re-read, run subagents on the mid-tier model, keep replies short | project `CLAUDE.md` (and `AGENTS.md` if Codex is installed; an existing `GEMINI.md` for Gemini CLI or `.github/copilot-instructions.md` for Copilot CLI, unless Copilot already loads a rules block from `AGENTS.md`/`CLAUDE.md`/`GEMINI.md`), in a marked block. Setup never creates a new Gemini or Copilot instruction file. | Long sessions re-read their whole history on every step. |
| Credit warnings inside Claude Code: the status line, and a prompt hook that shows the top warning in the session (only if they are not already there; an existing status line is kept and wrapped, see [In your status bar](#in-your-status-bar)) | `~/.claude/settings.json`, keys `statusLine` and `hooks.UserPromptSubmit` | Warnings only help if you see them while you work. After this, every Claude Code session shows them with nothing to run by hand. |
| Autopilot for Claude Code: summarize long sessions at about 200K tokens, shape and save the summary, and point a new chat at it (each part only if you haven't set it yourself; see [Autopilot](#autopilot-shorter-sessions-with-nothing-to-type)) | `~/.claude/settings.json`, keys `env.CLAUDE_CODE_AUTO_COMPACT_WINDOW` and `hooks.PreCompact`, `hooks.PostCompact`, `hooks.SessionStart` | Nobody has to remember `/clear`. |
| A `/credits` shortcut for Claude Code (only if no `credits` skill or command exists) | new file `~/.claude/skills/credits/SKILL.md` | The start-up and waste checks without a terminal: the agent runs `startup` and `watch --once` and reports in plain words. After you say yes to one fix, it runs `setup --only <id> --yes` for that step, with the same backup and undo. An older copy written by this tool is updated; a `/credits` of your own is never touched. |
| A `/brief` shortcut for Claude Code (only if no `brief` skill or command exists) | new file `~/.claude/skills/brief/SKILL.md` | The brief writer without a terminal; see [Write a brief that stays cheap](#write-a-brief-that-stays-cheap). |
| A `/handoff` shortcut for Claude Code (only if no `handoff` skill or command exists, in your home folder or the project) | new file `~/.claude/skills/handoff/SKILL.md` | `/clear` is the cheapest fix for a long session, but the agent forgets the task. `/handoff` writes a short note first; see [Start fresh without losing the task](#start-fresh-without-losing-the-task). |
| Gemini CLI usage statistics off | `~/.gemini/settings.json`, key `privacy.usageStatisticsEnabled` | It defaults to on and sends usage statistics to Google. |
| Caveman telemetry off | `~/.caveman/cloud.json`, key `telemetry.enabled` | The Caveman CLI reports usage, including your IP, by default. |
| RTK telemetry back off (only if someone turned it on) | `~/.config/rtk/config.toml`, key `telemetry` | Privacy. |
| *Optional, asked separately because it downloads:* pinned installs of RTK OSS v1.2.0 (`RTK_EDITION=oss`, commit `e288a18`) and the Caveman **skill only** v4.1.0 (commit `1e91f13`) | pinned in `tokenstretch/catalog.py` | Shell-output compression and terse replies, from pinned, hash-checked sources. |

To undo, run **`tokenstretch undo`**. It restores every file from the latest setup run, newest step first, and deletes files that setup created. If you've edited a file since then, undo stops and tells you, unless you pass `--force`. To undo a single step, pass that step's manifest from `~/.tokenstretch/manifests/`.

Shell rc files are never edited. If `ANTHROPIC_MODEL` is set in your shell, setup tells you rather than changing it.

## Watch while you work

`tokenstretch watch` re-reads your local session logs every 10 seconds (`--interval`) and prints a warning the first time a session shows one of these. It never repeats a warning for the same session.

| Warning | Why it costs credits | Fix it suggests |
|---|---|---|
| History over 150K tokens (`--max-context`); lower on a dearer model, see below | Every step re-sends the whole conversation. | `/clear` (fresh start) or `/compact` |
| Top model on the last 3 simple steps (short replies, few tool calls) | The top model costs several times more per step. | `/model sonnet` for routine steps |
| The same file read 3+ times (`--max-reads`) | Each re-read is added to history and re-sent. | Ask for line ranges or a note; start fresh |
| History grew 60K+ tokens in 5 minutes | Big reads or noisy output pile up fast. | Read narrowly, trim output |
| Helpers (subagents) re-sent 1M+ tokens and more than the main conversation | Each helper is its own conversation with its own start-up size, re-sending its history on every step. | One narrow task per helper, name the files to work from, ask for a short reply |
| Finished helpers woke the conversation 3+ times (`--wake-min`) and those steps re-sent 300K+ tokens | Each background helper that finishes starts a new step, and that step re-sends the whole conversation. | Start helpers early and together; one final report, not one per helper |
| 2+ helpers ran at `xhigh` or `max` effort and re-sent 500K+ tokens (Claude Code logs record the effort) | A higher effort lets the model think longer on every step, and thinking is billed as output. | Lower effort for routine helpers; keep the top effort for the hard step |
| The conversation itself ran 15+ steps at `xhigh` or `max` effort and they wrote 60K+ output tokens | Thinking is billed as output, and the effort applies to every step, also the routine ones. | Keep the top effort for the hard step; lower it for routine steps |
| The session started at 40K+ tokens before any work (`--max-start`) | Instruction files, tool and connector definitions, and skill listings are re-sent on every step of every session. `/clear` doesn't remove them. | `tokenstretch trim`; turn off unused connectors and skills (`doctor` W03, W25) |
| The saved-context discount (prompt cache) expired during a pause | The next message re-pays for the whole conversation instead of the discounted re-read. | `/clear` before a break; start fresh instead of resuming a big idle session |

```
⚠ [claude-code abcd1234 · -Users-me-app] history is ~200K tokens (over 150K).
   Why it costs credits: every step re-sends the whole conversation, so each reply now costs at least that much input.
   Fix: finish this step, then type /clear (fresh start) or /compact (keeps a short summary).
```

When the session's start-up size is known, the long-history warning also says about how many more steps it takes for a fresh start to pay for itself: roughly two steps at the current size, divided by what each later step saves (current size minus start-up size). It's a rule of thumb from token counts, not a price calculation.

**Limits follow the price.** Re-reading the same history costs more on a dearer model, so three limits come down with it: history size, fast growth, and helper wake-ups. The yardstick is the mid model. On a model where a re-read costs 2x, the history limit is 75K, not 150K; the floor is 60K however dear the model. The warning names the reason. Cheaper models keep the plain limits, and so do models with no published price ratio in `tokenstretch/cost.py`. A limit you set yourself (`--max-context 200000`) stays as you set it. The status bar line shows the same limit.

The cache warning comes in two forms. If the logs already show it (after a pause of 5+ minutes, the same model re-wrote most of the history to the cache and read almost none back), watch reports how often and how many tokens. If a session with 20K+ tokens of history is sitting idle past the cache lifetime right now (about an hour on a Claude subscription, about five minutes with an API key or a 5-minute cache setting), watch says the discount has probably expired, so you can `/clear` before your next message. That second form is a prediction from the clock, and it says so. Claude Code only for now.

It's read-only and only looks at token counts, model names, tool-call counts, and the paths of files the agent read (never their contents). `--once` does a single pass. `--notify` also pops a desktop notification through your OS's own `osascript` or `notify-send`, if present. Claude Code is fully supported. Codex gets the history, top-model, and growth warnings (its logs don't record file reads). Other tools say "not supported yet".

## Autopilot: shorter sessions with nothing to type

Most people will never type `/clear` or `/handoff`, so `tokenstretch setup` also sets Claude Code up to do the same job by itself:

1. **Long sessions are summarized automatically at about 200K tokens.** Setup sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000` under `env` in `~/.claude/settings.json`. Without it, a model with a large context window can carry several hundred thousand tokens on every step before anything trims it.
2. **The automatic summary is written as a note you could resume from.** A `PreCompact` hook tells the summary what to keep (goal, what's done and how it was checked, files and branch, decisions, next step, open questions) and what to drop (file contents, command output, search results).
3. **The summary is saved** to `.claude/handoff-auto.md` in the project by a `PostCompact` hook.
4. **The work restarts by itself after a summary.** The same `SessionStart` hook fires after a compaction and tells the agent to carry on with the summary's next step in the same turn, without stopping to report or asking for a go-ahead. Anything waiting on you still waits.
5. **A new chat picks the work up.** A `SessionStart` hook tells the agent where the newest note is (that file, or the one `/handoff` writes) if it's less than 72 hours old. Say anything ("continue", "what's happening?") and the agent reads the note, tells you where things stand in a few lines, and does the next step without asking you to say go. Ask for something else and the note is left unread. Things the note lists as waiting on you still wait. You do have to send that one message: Claude Code doesn't let a hook start the first turn of an interactive chat.

While autopilot is on, `watch`, the status line and the in-session warning stay quiet about long history below the automatic size: no tip to `/clear` by hand contradicts it. A session that is well past that size (it started before setup, so the setting never reached it) gets one tip instead: type `/compact` once.

The three hooks are one command, `tokenstretch auto`. It makes no model calls, reads only what Claude Code passes it, never follows a link when writing, always exits 0, and its hook command ends in `|| true`, so it can't block a session, a prompt, or a compaction. Add `.claude/handoff-auto.md` to `.gitignore` if you don't want it committed.

Setup leaves your own choices alone: if you already set `CLAUDE_CODE_AUTO_COMPACT_WINDOW`, `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`, `DISABLE_AUTO_COMPACT`, `autoCompactWindow`, or turned `autoCompactEnabled` off, the size isn't touched. To use a different size, change the number in `settings.json` (Claude Code accepts 100000 to 1000000).

What this costs: each automatic summary is one model step over the history, and a summary keeps less detail than the full history. What it saves: every step after it re-sends a short note instead of 200K or more. This is built on Claude Code's documented settings and hook events; the unit tests cover the hook and setup logic, and the hooks have been watched on one live automatic compaction: a 283K history came back as a 20K summary in the handoff shape, and the note was saved to disk. That session began before the size setting was written, so a compaction at the 200K size, in a session started after setup, has not yet been watched.

## Start fresh without losing the task

Every step re-sends the whole conversation, so the biggest saving is a fresh session. Most people don't take it, because the agent then forgets what it was doing. Setup adds a `/handoff` shortcut to Claude Code that removes that cost:

```text
/handoff           the agent writes .claude/handoff.md: goal, what's done, files changed, decisions, next step
/clear             fresh session
/handoff resume    the agent reads the note and carries on from "Next step"
```

The note is at most 40 lines, written from what the agent already knows (it's told not to read files or run commands for it), so the handoff itself is one cheap step. After `/clear` you re-send a page instead of the whole history.

The shortcut is a manual-only skill (`disable-model-invocation: true`), so it adds nothing to each turn and the agent never runs it on its own. Once it's installed, `watch` and `statusline` say `/handoff then /clear` instead of `/clear`. Setup never replaces a `handoff` skill or command you already have, and `tokenstretch undo` deletes the file. The note lives in the project's `.claude/` folder; add `.claude/handoff.md` to `.gitignore` if you don't want it committed. Claude Code only for now. The saving isn't measured yet: it depends on how often you clear.

## In your status bar

`watch` needs its own terminal. `tokenstretch statusline` puts the same checks where you're already looking: it prints one line with the current session's history size and, if watch would warn, the most urgent warning.

```text
22K/150K history · claude-sonnet-5-5
⚠ 158K/150K history · /clear at the next task break
⚠ 104K/150K history · cache expired after ~50 min idle (~99K re-sent): /clear before breaks
```

For Claude Code, `tokenstretch setup` adds it for you (shown first, backed up, undone by `tokenstretch undo`). If you already have a status line, setup keeps it and wraps it. To add it by hand instead, put this in `~/.claude/settings.json`:

```json
{ "statusLine": { "type": "command", "command": "tokenstretch statusline" } }
```

Already have a status line from another tool, such as Caveman's? A tool has one status-line slot, so share it with `--wrap`. Put your current command inside it:

```json
{ "statusLine": { "type": "command", "command": "tokenstretch statusline --wrap 'bash ~/.claude/hooks/caveman-statusline.sh'" } }
```

The wrapped command gets the same input and prints as before, and tokenstretch adds its line to the end of the first line (`[CAVEMAN] · 22K/150K history · claude-sonnet-5-5`). If the wrapped command fails or takes over 3 seconds, you get the tokenstretch line alone; if tokenstretch has nothing to say, you get the other line unchanged.

Claude Code pipes the session's log path to the command, and `statusline` reads that one file. It only opens a `.jsonl` file inside `~/.claude/projects`; any other path prints nothing. Run by hand, it uses your newest active session. It's read-only unless you have switched `pace auto` on (then it stores plan readings in tokenstretch's own `pace.json`), runs no other program unless you pass `--wrap`, and takes about a tenth of a second. If anything goes wrong it prints nothing and exits 0, so it can't break your status bar. `--max-context` and `--max-reads` work as in `watch`.

### In the session itself

Setup also adds a prompt hook, `tokenstretch nudge`. When you send a prompt and the session has a warning, Claude Code shows it in the chat and passes it to the agent, which is told to finish your request first and then mention it in one line. This is what makes the warnings reach sessions nobody is watching, such as agents working in the background.

- No warning, no output, so a clean session pays nothing.
- The same kind of warning is repeated at most every 20 minutes per session.
- It reads the one session log Claude Code names (token counts, never prompt text), always exits 0, and can't block a prompt (the hook command ends in `|| true`, so even a broken install can't).
- It keeps one small file per session in `~/.tokenstretch/nudge/` to remember what it last said. Files older than a week are removed.

## Dashboard

```bash
tokenstretch dashboard            # opens http://127.0.0.1:8765/<one-time key>/ in your browser
tokenstretch dashboard --no-open  # just print the link; --port 9000 to pick a port
tokenstretch dashboard --url      # print the link of the dashboard that is running now
tokenstretch dashboard --remind on|off   # the link reminder (see below)
```

One page, refreshed every 5 seconds while it runs:

![Full tokenstretch dashboard with doctor results, tools and telemetry, setup status, and bench results](docs/images/dashboard-full.png)

- **Overview:** tokens today and this week, cost (only if your logs report it), active sessions, and the biggest history.
- **Live sessions:** tool, project, model, and history size with a bar against the 150K mark.
- **Live warnings:** the same once-per-problem warnings as `watch`, each with its fix.
- **Models this week** and an **8-week trend**, as simple bar charts.
- **Doctor results** with plain-English fixes (re-run with a button; it's read-only).
- **Setup status:** what tokenstretch applied and whether `tokenstretch undo` can restore it.
- **Token-saving tools** found on your machine and whether their telemetry is off.
- **Since setup** (a before/after from your own logs, clearly labeled observational) and **bench results** if you've measured one.
- **Pace**, **team queue**, **cost**, **redo work**, **start-up size** and **watch** panels. Each one reads the same data as its command (`pace`, `queue list`, `model cost`, `model rework`, `startup`, `watch`) and shows a one-line reason when there is nothing to show yet. Cost figures are estimates from published price ratios, not a bill, and the redo-work and start-up numbers are heuristics from your own logs.

The page opens instantly and shows "warming up" while it reads your logs in the background; usage numbers and doctor results fill in as they're ready (seconds on a large log folder, the first time). After that it only reads what changed. `--weeks 8` sets how far back it reads.

It's read-only: it never changes your files, it only shows the commands to run. It writes only inside its own folder, `~/.tokenstretch/`: a link file, `dashboard.json`, while it runs (removed when it stops), and the read caches under `cache/` that hold times, model names and token counts so later reads are fast. It listens on 127.0.0.1 only, needs the one-time key in the link for every request, and loads nothing from the internet. Press Ctrl-C to stop it.

**Link reminder (off until you turn it on).** `tokenstretch setup` offers one extra, undoable item for Claude Code that switches on a reminder. When it is on, the session-start hook that setup already installs adds one line to a new session's starting context, and again after each automatic summary, that asks the assistant to end each finished task with "View your tokenstretch dashboard at <link>" if the dashboard is running, or with the `tokenstretch dashboard` command if it is not. It is a request to the assistant, so whether the line shows up is up to the assistant. It never asks you anything and is meant not to stop the task. The link only ever points at 127.0.0.1. The switch is the file `~/.tokenstretch/dashboard-remind.json`; `tokenstretch dashboard --remind off` (or `tokenstretch undo` after setup) turns it off at once. A link file left behind by a dashboard that crashed is ignored when its port no longer answers.

`tokenstretch report --weekly` writes the same weekly numbers to a self-contained HTML file under `~/.tokenstretch/reports/` (`--weeks 8`, `--out FILE`, or `--json`).

## See what every session starts with

Every session carries a block before you type anything: instruction files, skill and helper listings, and tool and connector definitions. It is re-sent on every step, and a fresh start doesn't remove it. `tokenstretch startup` shows what it is made of.

```text
Claude Code: what every session carries before you type
  Measured: the newest session's first step was ~77K tokens. Recent sessions: 77K, 80K, 75K.
  Price: on claude-fable-5-2, 50 steps re-read this block 50 times. That costs about as much as ~385K new input tokens on the mid model, 2x what the mid model pays for the same block. An estimate from published price ratios, not a bill.
  Sized from local files: ~2.5K tokens (approx., bytes / 4).
    ~  1.3K  instruction file: ~/work/CLAUDE.md
             `tokenstretch trim` removes duplicates and moves reference blocks out.
    ~  0.9K  instruction file: ~/work/app/CLAUDE.md
  The rest, ~75K tokens, can't be sized from local files: the tool's own system prompt and built-in tools, and connector tool lists.
  To size a connector: turn it off, start a new session, and run this again. This command remembers the last run and shows the difference as that connector's size.
```

That output is from a real machine, with paths changed. The instruction files were under 4% of the start-up size, so trimming them would not have helped much there; the connectors and plugins were the place to look. The command exists to tell you which case you are in before you spend time on the wrong fix.

The `Price` line puts the block in price terms for the model the newest session ran on: a re-read is cheap per token, but the block comes back on every step, and a dearer model multiplies it. It appears only for models with a published price ratio.

Sizes are estimates (bytes / 4). Skills marked `disable-model-invocation` are left out because they aren't listed to the model. Instruction files deeper in the project are left out because they load only when the agent works in that folder. `--all` lists every part and `--json` prints the data. Claude Code only for now.

Plugins and skills added by the desktop app are listed too, each with its listing size and the number of connectors it brings. The command reads the last 14 days of Claude Code logs on this computer and counts calls to each one. A `No use found in the last 14 days` line names the ones with no call. Read that line with care: it counts Claude Code sessions only, so a skill you use in the desktop app's chat can still be named. A plugin with hooks is never named, because hooks run without a call. The command switches nothing off. For a plugin installed from the terminal it prints the `enabledPlugins` line to set to `false` in `~/.claude/settings.json`. Plugins and skills the desktop app adds are switched off in that app's settings. The listing sizes are small next to the connectors a plugin brings, which are not sized here.

Connector tool lists aren't in any local file, so they are sized by difference. Each run remembers the unsized remainder for the current set of connectors in `~/.tokenstretch/startup-history.json` (connector names and token counts only). Turn one connector off, start a new session, and run `startup` again: it reports how much lighter sessions now start, and if you turn the connector back on it is listed with that size. The two numbers come from two different sessions, so a change under 2K tokens is ignored and the size is approximate. When the local files and connectors are the same as last time but the start moved by 2K or more, it says so: a plugin or an app connector changed. `--no-save` keeps the run read-only.

<!-- team: full-effort section -->
## Run a team at full effort

Lowering the effort of a long-running team is not the only way to make a plan's weekly limit last, but be clear about how much the other way buys. On one week of logs from a team that worked mostly on a top model (about 2,100 top-model steps, every log read in full), about 33% of the spend was history re-read, 31% was output and thinking, 30% was new content, and 6% was old history saved to the cache a second time. With no second saves and re-reads halved, that week would have used about a quarter less, which is about 1.3x more work at the same effort (`pace --what-if` shows the same sum for your own logs). So shorter history and a lighter start-up help, and they do not by themselves make a week of top effort without a stop fit in a plan. What keeps the limit from arriving early is a governor on how many agents run at once. The parts:

- One task per fresh session, with no pause. `tokenstretch queue` is a file-based queue in the project (`.tokenstretch/queue/`, one JSON file per task, each holding a ready `brief`), so agents pull work and the lead spends no steps on dispatch.
- A light start-up for workers. `tokenstretch startup --agent` sizes the parts of a session's start-up that it can see in local files (instruction files, skills, connected tool lists) and lists the ones worth switching off for a worker, with the saving over 50 steps in price units. The tool's own system prompt and built-in tools are not in local files, so it cannot size those; on a set-up that loads little of its own it says that no part is worth switching off. It prints settings to use; it writes nothing.
- Proof of what redo work costs. `tokenstretch model rework` estimates, per model and effort, how much spend went to redo steps and what a finished task cost, with four named heuristic signals.

```text
tokenstretch queue add "fix the retry" --files src/upload.py --done "tests pass"
tokenstretch queue add --from-plan plan.md      # one task per numbered step; keeps the file paths and the check each step names
tokenstretch queue next --agent w1              # gate, then claim the oldest open task and print its brief
tokenstretch queue done T0001 --note "ok"       # or: queue fail T0001 --note "what went wrong"
tokenstretch queue list                         # open, claimed, done, needs-lead
tokenstretch queue release --stale 30           # free claims with no update for 30 minutes
tokenstretch queue loop-prompt                  # the standing prompt for a worker session
```

`queue next` checks the pace gate first (`tokenstretch pace --gate`; with no reading stored the gate is open). Over the even-pace line it prints the wait and exits 3; an empty queue exits 4. A claim is a file created with an exclusive open, so two agents never get the same task, and a task whose files overlap a claimed task's files is skipped. `queue fail` sends the task back to open with the note added to its brief, so the next agent does not repeat the mistake; after two failures it is marked `needs-lead` for the lead to look at. The worker's standing prompt ends the session after each task: a fresh short session per task keeps the history small and the cache hot.

`model rework` reads local Claude Code logs, read-only. The signals are a file edited again after a failing test or build, the same file edited 3 or more times, a prompt with a correction word ("still", "again", "wrong", ...), and the steps from a failing check to the next passing one. These are heuristics: a file edited twice may be good work, and the counts are a floor (only the newest part of a long log is read, and edits made through shell commands are not seen). It compares efforts in one sentence only when both have 3 or more sessions, and otherwise says the sample is too small. Units are price-weighted estimates, not dollars and not a bill; models without a published ratio show steps only.
<!-- /team: full-effort section -->

## Trim instruction files

Your CLAUDE.md, AGENTS.md, and similar files are resent on every turn, so a bloated one quietly costs credits all day. `tokenstretch trim` looks at the project you're in (or `--project DIR`) plus your user-level files, and shows what each tool loads per turn:

```text
$ tokenstretch trim --move-blocks
Files:
  ~/.claude/CLAUDE.md  user          ~25 tokens  Claude Code
  CLAUDE.md            project      ~158 tokens  Claude Code
  AGENTS.md            project       ~17 tokens  Codex

Resent per turn:
  Claude Code: ~183 tokens per turn (~18,300 per 100 turns, approx.)
  Codex: ~17 tokens per turn (~1,700 per 100 turns, approx.)

Findings (5):
  MED  duplicate: CLAUDE.md:5 repeats ~/.claude/CLAUDE.md:3 exactly ("- Always run the full test suite before you say a task is d…").
       Why: The same text is already in ~/.claude/CLAUDE.md, which loads in the same turn, so you pay for it twice on every turn in Claude Code.
       Fix: Keep the first copy. trim removes this one.
  MED  big-block: CLAUDE.md:12 has a pasted log (10 lines, ~69 tokens approx.).
  LOW  stale-path: CLAUDE.md mentions 1 path(s) that don't exist in this project: src/legacy/main.py (line 3).
  ...
Proposed edit (safe mechanical changes only: exact duplicates, blank lines, trailing spaces, moved blocks; prose is never rewritten):
  CLAUDE.md: ~158 -> ~81 tokens (-77 approx.)
  create docs/agent-notes/claude-1.md (not loaded each turn; the agent opens it when needed)
--- a/CLAUDE.md
+++ b/CLAUDE.md
...
Dry run: nothing was changed. Add --apply to make these edits (backed up; `tokenstretch undo` restores them).
```

It checks for exact and near-duplicate paragraphs and bullets (within a file, and across files the same tool loads together), files over ~2,000 tokens (`--max-tokens`), big pasted blocks (code over 30 lines, long tables, logs and stack traces, base64 or minified blobs), relative paths that no longer exist in the project, extra blank lines and trailing spaces, and a duplicated tokenstretch rules block. Each finding says why it costs credits and how to fix it.

The proposed edit never rewrites or summarizes your prose. It only removes exact duplicate blocks (keeping the first copy), collapses blank lines, and strips trailing spaces. With `--move-blocks` it also moves big code blocks and logs from project files into `docs/agent-notes/<name>.md` and leaves a one-line pointer. Token counts are approx. (characters / 4), computed from the files themselves. `--apply` asks `[y/N]` (or `--yes`), backs up every file, and `tokenstretch undo` restores them byte for byte. `--json` prints machine-readable output, and the dashboard shows the same numbers in an "Instruction files" card.

`tokenstretch trim --to-skills` is a separate mode for Claude Code. It finds workflow-style sections in your CLAUDE.md files (a numbered list of 3+ steps, a code block of commands, or a heading like "release" or "deploy") and moves each into a skill (`.claude/skills/<name>/SKILL.md` in the project, `~/.claude/skills/<name>/SKILL.md` for your user file), leaving a one-line pointer. Sections that read like standing rules (they mention "never", "always", "private", "security", and similar) stay where they are, and the dry run lists them with the reason. It never overwrites a skill folder that already exists, and `--min-tokens` (default 150) sets the smallest section it will move. The dry run shows the diff and a net per-turn count (tokens removed minus the pointer lines and skill descriptions that Claude Code still loads), all approx. Skills load only when the assistant picks them from their one-line description, so a moved section is no longer guaranteed to be read: keep rules that must hold every turn in CLAUDE.md, and review each description. `--apply` backs up the files, and `tokenstretch undo` restores them and removes the skills it created. It cannot be combined with `--move-blocks`.

## Write a brief that stays cheap

Most wasted steps start with a prompt that leaves the agent to guess: where to work, what to read, when to stop. `tokenstretch brief` turns a one-line task into a brief that answers those, and tells you what your task text is missing.

```text
$ tokenstretch brief "write the cost page" --files site/cost.py --from research/notes.md \
      --done "the audit prints NO ISSUES" --helper --no-build
Task: write the cost page

Work only in this file: `site/cost.py`. Do not change any other file.

Work from: `research/notes.md`. Read each once. Open another file only when you need its exact lines.

Rules:
- No web search and no new research. If a fact is missing, say so in your reply; do not go looking.
- Search before you read, and read line ranges, not whole large files.
- Do not re-read a file you already read unless it changed.
- Do not run builds, tests, or installs. The caller does that.

Done when: the audit prints NO ISSUES

Reply in under 80 words: what you wrote, file paths, and anything you needed and could not find. No recap of the task.

(about 165 tokens, approx.; suggested model tier: mid, effort: medium, a rule of thumb from `tokenstretch model suggest`)

Nothing missing: files, stop condition, and limits are all stated.
```

Give it a bare task and it lists what's open, each with why it costs credits and the fix: no file named, no stop condition, open-ended wording, several tasks in one, research with no limit (`--max-searches`), no notes to work from. `--check` prints only that list and exits 1 if anything is missing, so you can use it in a script. `--file` reads the task from a file and `--json` prints machine-readable output.

It uses simple local rules you can read in `tokenstretch/brief.py`. It makes no model calls and reads only the text you pass in, never your session logs or past prompts. In one live test, helpers given briefs like this ran 6 to 7 steps and re-sent about 0.4M tokens each; open-ended research helpers in the same project ran 10 to 34 steps and re-sent 0.8M to 3.2M each. Those were different kinds of task (writing against research), so treat it as an illustration, not a measurement.

For research tasks the brief also asks for the full URL and read date of every fact, a mark for read-versus-search-summary, and nothing from memory, and it says to list a gap instead of skipping a topic. `--max-searches` and `--max-fetches` set the caps. `--brief-only` prints just the brief, ready to paste or pipe.

For a review, add `--review`: the helper reads only the named files, changes nothing, and reports one line per problem as `path:line: problem. fix.`. A reviewer keeps every file it opens in its history, so a wide review is costly; `--split 8` prints one brief per group of 8 files, each for its own helper, and `--max-reads` caps file reads.

`--preset` starts from a job shape, so you don't retype the same limits. Anything you pass yourself wins over the preset.

| Preset | For | What it adds |
|---|---|---|
| `build` | building something new from one existing example | follow the one example in `--from` and read nothing else in bulk, at most one helper, run the project's check once, 60-word reply. Suggested effort: medium. |
| `research` | a research helper | 8 searches and 6 page fetches at most, answer only the questions in the task, one output file, 60-word reply. Suggested effort: low. |
| `plan` | planning a job before any edits | write the plan to one file and change nothing else, numbered steps that each name their files and their check, steps small enough for a smaller model at a lower effort, hard steps marked, open questions listed, no builds, 12 file reads at most. Suggested tier: top. Suggested effort: high or above. |
| `design` | visual or layout work | look at the current result at phone and desktop width first, write the direction down before editing, up to 5 reference pages on the web, 200-word reply. Suggested effort: high or above. |

The lint knows the shapes too: `build` with no `--from` example and `research` with no question in the task are both flagged. The shapes come from one live project: a build briefed this way re-sent 2.1M tokens where an earlier, wider brief for the same kind of job re-sent 5.2M, and a research helper given an output file and a list of questions ran 9 steps where an open one ran 24. Two runs each, different subjects, so an illustration and not a measurement. `plan` is how the top model earns its price: it runs once, on the step where a wrong call costs the most, and the steps it writes go to a smaller model at a lower effort. A design job is the opposite case: a tight no-web, 80-word brief is the wrong shape for it, which is why that preset loosens both. With a preset, the output also reminds you to check the session ran at the effort you chose (`tokenstretch model effort --by-session`).

In Claude Code you can skip the terminal: after `tokenstretch setup`, type `/brief add a retry to the upload client`. The agent rewrites the task as a brief with the same parts and lists what is missing. It's a manual-only skill, so it adds nothing to each turn. Unlike the command, `/brief` uses the model, so it costs one short reply.

## Pick the right model

```
$ tokenstretch model suggest "Debug the race condition in the websocket reconnect logic"
Suggested tier: TOP: hard reasoning across many parts; worth the top model for this task only.
Why: +2 mentions concurrency/leak bug (score +2).
Suggested effort: HIGH: one hard signal; worth more thinking for this task.

  Claude Code: opus  (switch now: `/model opus`)
    effort: `"effortLevel": "high"` in settings.json, or start the session with `CLAUDE_CODE_EFFORT_LEVEL=high`

Pick the effort when a session starts: a change in the middle of a session re-sends the history at full price.
```

`model suggest` scores the task with simple rules you can read in `tokenstretch/models.py`: words like architecture, broad refactor, migration, race condition, security, root cause, or a visual redesign push toward **top**; rename, typo, formatting, docs, or a version bump push toward **light**; tests and small fixes lean **mid**; naming many files or a long description pushes up. The same score picks a reasoning effort: **low** for mechanical work, **medium** for everyday work, **high** for one hard signal, **xhigh** for several at once. It never suggests `max`. It makes no model calls. **It's a rule of thumb, not a measured result**, so move up a tier or an effort level if the answer comes back wrong. `--file prompt.txt` reads the task from a file and `--json` prints machine-readable output.

`tokenstretch model effort` reads your newest session logs and shows the reasoning effort they ran at: steps and output tokens per effort level, main conversations and helpers apart. It says so when every step ran at one level whatever the task, when most helper steps ran at high or above, or when `max` was used. It reads only what the tool's own log records (Claude Code today), changes nothing, and `--json` prints the same numbers. `--by-session` adds one line per conversation with the model and effort it was logged at, so you can check that a session you set to a higher (or lower) effort ran at it.

`tokenstretch model cost` answers a different question: where did the credits go, in price terms? Token counts alone hide the price. An output token costs about five times an input token, a re-read from the cache costs a small share of one, and the top model multiplies all of it. This command weighs each kind of token by published list-price ratios, per model and effort, main conversations and helpers apart.

```text
$ tokenstretch model cost
Where the credits went in the 12 newest conversations (local logs, read-only).
1 unit = one new input token on the mid model at list price. An estimate from published price ratios (read 2026-11), not a bill.

claude-fable-5-2, MEDIUM, main conversation: 162 steps, ~7.9M units (53% of all)
    history re-read      25%  (19.8M tokens)
    history saved again  17%  (265k tokens)
    new content          28%  (445k tokens)
    output and thinking  30%  (117k tokens)
    the same tokens on the mid model: ~2.5M units (3.2x less). A smaller model may need more steps, so try it on routine work first.
    biggest lever: replies are the largest part: ask for shorter replies, and for changed lines, not whole files.

claude-sonnet-5-5, HIGH, main conversation: 94 steps, ~1.3M units (9% of all)
    history re-read      49%  (12.3M tokens)
    history saved again   5%  (55k tokens)
    new content          24%  (245k tokens)
    output and thinking  22%  (55k tokens)
    biggest lever: the conversation is long and every step re-reads it: start fresh at the next task break (`tokenstretch watch` says when).
```

That output is from a real machine, shortened. The four kinds: **history re-read** is the conversation coming back from the cache on each step; **history saved again** is old history written to the cache a second time, after a pause outlasted the cache or the model changed mid-conversation; **new content** is what arrived for the first time (files read, command output, your prompts); **output and thinking** is what the model wrote, with its thinking. Each row ends with one lever, picked from its largest kind.

`--grid` prints one line per model and effort with the average cost of a step, dearest first, so a top-model, high-effort session sits next to a mid-model, low-effort one:

```text
model                       effort     steps  units/step  output/step  re-read/step  vs cheapest
claude-fable-5-2            medium       184         47k          731          124k  3.5x
claude-opus-5-5             medium        53         35k           1k          112k  2.6x
claude-sonnet-5-5           medium       119         15k          775          118k  1.1x
claude-sonnet-5-5           high          94         13k          586          131k  1.0x
```

A dearer row is not waste by itself: compare rows that did the same kind of work. Limits to know: units are relative list-price ratios, not dollars, and a subscription plan counts usage its own way. Logs don't say which cache lifetime a save used, so every save is priced at the short-lifetime rate (the one-hour kind costs more). "Saved again" is detected from the pattern (a later step that saves 20K+ tokens and more than it re-reads). Models with no ratio in `tokenstretch/cost.py` (other providers, older models) are listed with token counts only; nothing is guessed. `--sessions N` sets how many conversations are read per tool and `--json` prints the data. Read-only, no model calls.

`tokenstretch model default --tier mid` shows the change to your default model (Claude Code: `model` in `settings.json`). Add `--apply` to make it. The file is backed up first and `tokenstretch undo` restores it. `--tool claude` limits it to one tool. Gemini CLI uses its documented aliases (`flash-lite` / `flash` / `pro`, key `model.name` in `~/.gemini/settings.json`). Codex uses the model ids from its models page (`gpt-6-luna` / `gpt-6.1-sol` / `gpt-6-astra`, the top-level `model` key in `~/.codex/config.toml`; tables such as `[profiles.*]` are left alone). Which models you can use depends on your plan. Codex also lists `gpt-6.1-terra` as a fast light-tier alternative. Copilot CLI uses a recommended mapping (`claude-haiku-5.5` / `claude-sonnet-5.5` / `claude-opus-5.5`, with GPT-6 alternatives), which isn't a list GitHub publishes. If your Copilot model is `auto`, `model default` sets `autoTier` instead (`efficiency` / `balance` / `intelligence`).

## Make a week last at full effort

`queue next` runs the gate for you before it hands out a task.

Lowering the reasoning effort is one way to stretch a plan, and it can cost you in redone work. This is the other way: cut what is re-sent and re-saved (long history, lapsed caches, heavy start-up), and let a governor decide how many agents run at once, so the plan's weekly limit arrives at the reset, not before.

A subscription plan shows percent used per window (5 hours, the week, the week for the top model family) and a reset time. Local logs do not record that percent, so you type it in once in a while. `pace` then calibrates how many price-weighted units one percent is, from the logs since the window began, and tracks the percent from there. On the one account we checked this against (one reading stored, a week of logs), the estimate matched the plan's own card to the point at two later checks within half an hour: 83% then 84% for the top-model week, 61% then 62% for all models. That is one account over a short span, not a promise; add a reading now and then.

To skip the typing on Claude Code, run `tokenstretch pace auto`. Claude Code hands its status line the plan's 5-hour and 7-day limits (percent used and reset time, on a subscription plan, after a session's first reply). With `pace auto` on, the tokenstretch status line stores them as windows `auto-5h` and `auto-week` each time the percent moves a point or half an hour passes. It is off until you switch it on, it stores only the percent and the reset time in `pace.json`, and `tokenstretch pace auto --off` stops it. Claude Code reports no per-model weekly limit there, so keep a top-model window current with `pace set`. This needs a session where the status line runs, which means Claude Code in a terminal. In our check, sessions inside the desktop app stored nothing, and one terminal session stored both windows after its first reply.

```bash
tokenstretch pace set --window weekly-top --used 80 --resets 2026-03-09T09:00:00 --length 7d --models 'fable|opus'
tokenstretch pace set --window weekly --used 60 --resets 2026-03-09T09:00:00
tokenstretch pace                 # where each window stands against an even burn
tokenstretch pace --gate          # exit 0 on or under the line, 3 over it (for a script or a lead agent)
tokenstretch pace --team 8 --effort max
tokenstretch pace --what-if
```

Sample output (a temporary home with synthetic logs, three days into a week). The shares in it come from the synthetic logs; the what-if cut on one real week of logs was about a quarter, not 40%:

```text
$ tokenstretch pace
Pace (estimates): is the plan on an even burn through each window?
Units are price-weighted, 1 unit = one new input token on the mid model. Estimates from local logs and list-price ratios, not a bill.

weekly  (length 7d 0h, resets 2026-03-09T09:00:00Z)
    reading: 60% used at 2026-03-05T09:00:00Z  (1 stored)
    estimated used now: ~60%   share of window gone: 43%   pace ratio: 1.40  (over the line by 20 points; line = time share minus 3 reserve)
    calibration: ~2.4M units per 1% (units since window start / percent used; store a second reading later for a closer fit); ~97.5M units left, ~24.4M per remaining day (4.0 days left)
    about 226 steps left today on claude-fable-5-2 at effort max (~108k units per step)

weekly-top  (length 7d 0h, resets 2026-03-09T09:00:00Z, models /fable|opus/)
    reading: 80% used at 2026-03-05T09:00:00Z  (1 stored)
    estimated used now: ~80%   share of window gone: 43%   pace ratio: 1.87  (over the line by 40 points; line = time share minus 3 reserve)
    calibration: ~1.7M units per 1% (units since window start / percent used; store a second reading later for a closer fit); ~33.6M units left, ~8.4M per remaining day (4.0 days left)
    about 78 steps left today on claude-fable-5-2 at effort max (~108k units per step)

Tightest window: weekly-top (pace ratio 1.87, ~80% used with 43% of the window gone).
```

`pace --gate` prints one line and exits 3 while a window is over its line (the share of the window that has passed, minus a reserve of 3 points; change it with `--reserve`). It exits 0 when you are at or under the line, and also when no reading is stored: missing data never blocks work. `--json` prints `{ok, wait_seconds, window, used, line}`. A lead agent can run it before it starts another agent, and wait when it exits 3.

```text
$ tokenstretch pace --gate
pace gate: over. weekly-top ~80% used (estimate), line 40%; wait about 2d 19h for the line to catch up.
(exit code 3)
```

`pace --team N` measures units per step and steps per hour for the model and effort in your own logs, and says how many agents can run at once on the line and how long each of N agents can work a day. The sample below is a week that is already far over its line, so the answer is small; early in a window, or on the line, it is much larger. When the logs hold no steps at that effort it says so and uses the nearest measured effort, labelled. `--what-if` shows the same week with old history never saved again and re-reads halved, so you can see how many more agent-hours the cuts buy at the same effort:

```text
$ tokenstretch pace --team 8 --effort max --what-if
Team of 8 on claude-fable-5-2 at effort max (estimates, not a bill):
    measured: ~108k units per step, 86 steps per hour, ~9.2M units per agent-hour
    weekly: 0.11 agents at once on the line, 0.33 h a day for 8 agents
    weekly-top: 0.04 agents at once on the line, 0.11 h a day for 8 agents
    so: the line has room for less than one agent working without a stop. 8 agents can each work about 7 minutes a day (tightest window: weekly-top).
What if old history was never saved again and re-reads were halved? (same models, same effort; estimates, not a bill)
    weekly: history re-saved ~20.6M units, re-read ~76.8M units; the cuts remove ~40% of what was used. Estimated used would be ~36% instead of ~60%, 1.68x more agent-hours.
    weekly-top: history re-saved ~20.1M units, re-read ~68.2M units; the cuts remove ~40% of what was used. Estimated used would be ~48% instead of ~80%, 1.68x more agent-hours.
    8 agents: 0.11 h a day now, 0.19 h a day with the cuts (0.04 -> 0.06 agents at once; a day is capped at 24 h).
```

`watch` warns once per window when a stored window is 10 or more points over its line, and is silent with no reading or after the window has reset. Run `pace set` again after each reset.

Limits to know: every number is an estimate from local logs, published price ratios and your own readings. Units are not dollars and this is not a bill; a plan counts usage its own way, so read your plan's percent now and then and add a reading (two readings calibrate better than one). Models with no price ratio are left out of the units and listed, never guessed. For Claude Code every log in the window is read in full, helpers included, each step at its own time; the steps already read are kept in `cache/window-steps.json` under the tokenstretch home (times, model names and token counts, no prompt or reply text), so the first run is slow and later runs read only what the logs gained. For tools whose helper logs carry no times, helpers count in the same share as the main steps inside the window. Usage that is not in this machine's logs (another computer, the web or phone app) is in your plan's percent and not in the units. Your readings are stored in `pace.json` under the tokenstretch home. No network, no model calls.

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

Your mileage will differ. Run `tokenstretch bench` on your own repo (see `LIVE-BENCH.md`). `bench --from-logs` adds up what your existing logs already record, with no new runs.

## Privacy promises

- **No telemetry, no analytics, no phone-home, no update checks.** No outbound network libraries. The only socket tokenstretch itself opens is the optional dashboard, bound to 127.0.0.1 with a one-time key, and tests block every non-loopback connection.
- **No network calls**, except the pinned installs you confirm in a separate prompt.
- **Never opens credential files.** Login files and tokens are only `stat`'d (checked for existence and permissions), never read, copied, symlinked, or proxied. Tests plant canary credentials and fail if any are opened.
- **Your prompts, code, and logs never leave your machine because of this tool.** Session logs are read locally, only to show model and history size.
- **Every file change is backed up**, and `undo` restores it.
- **Checkable:** the whole test suite also runs with networking disabled (`unshare -rn`).

## Supported tools

| Tier | Tools | What works |
|---|---|---|
| 1 (full) | **Claude Code**, **Codex**, **Gemini CLI**, **Copilot CLI** | doctor, setup, status, watch, report, dashboard, model suggest, bench, Clean-profile hooks, undo (`model default`: Claude Code, Codex, Gemini CLI, Copilot CLI) |
| Doctor-only | Cursor, Aider, OpenCode, Cline | doctor checks and privacy listing |
| Planned | Goose, Devin (see `ROADMAP.md`) | |

Gemini CLI and Copilot CLI limits: Copilot's per-turn usage (for `watch`) only exists when you turn on its local OpenTelemetry file export (`COPILOT_OTEL_FILE_EXPORTER_PATH`); otherwise usage comes from session totals in `~/.copilot/session-state/*/events.jsonl`. Copilot input is read as including cache, per GitHub's `/usage` help and OTel docs. Copilot client telemetry can only be turned off with the `--disable-client-telemetry` flag, so doctor reports it as unknown unless an alias passes it. `bench --bench` reads Copilot's own `--usage-output-file`. `bench --prepare` / `--measure` cover both tools. RTK's hooks for these tools are wired by `rtk init -g --gemini` / `rtk init -g --copilot`, which you run yourself.

Every tool-specific path lives in its adapter (`tokenstretch/adapters/`). To add one, see `CONTRIBUTING.md` and `ADAPTERS.md`.

## FAQ

**Does it replace RTK or Caveman?** No. It checks, configures, and verifies them. It installs the safe editions from pinned sources (RTK's OSS edition, Caveman as a skill only), keeps their telemetry off, warns when two compressors or proxies stack up, and lets you measure whether they help on your repo. It also checks the token savers you already have (RTK, Caveman, TokenEconomizer, ccusage, Headroom, Kompressor, and similar) to confirm they're set up right and actually working. On one real machine, for example, it found Caveman's telemetry on and RTK's commands being blocked by Claude's permissions.

**Will it slow my agent down or cut work?** No. The biggest win is choosing the right model for the job and not re-reading old history. The top model is one `/model` command away whenever you need it.

**Does it change anything without asking?** No. Doctor, status, report, watch, statusline, dashboard, and model suggest are read-only. `nudge` (the prompt hook setup adds) changes none of your files; it only keeps its own small state file under `~/.tokenstretch/nudge/`. `doctor` keeps per-log totals in `~/.tokenstretch/cache/doctor-logs.json` (log paths and token counts, nothing else) so a second run takes seconds instead of re-reading every session log; `doctor --no-cache` writes nothing, and the file is safe to delete. The one exception you switch on yourself: with `pace auto` on, `statusline` stores plan readings in tokenstretch's own `pace.json`.

**Do I have to run anything after setup?** No. For Claude Code, setup puts the warnings in the status line and in the session, sets the default model, and adds the `/handoff`, `/brief`, and `/credits` shortcuts. If you never open a terminal, type `/credits` in the chat to see what a new session carries and what is wasting credits now. `watch`, `dashboard`, and `report` are there if you want more detail. Other tools get the rules block and model default; live warnings for them are on the roadmap. `trim` and `model default` are dry runs unless you pass `--apply`. Setup asks once, and it's a dry run without a terminal unless you pass `--yes`.

**Does it touch my login or API keys?** Never.

**Is it really free?** Yes. It's MIT licensed, with no paid tier, no account, and no server.

**What's the command exit code for doctor?** 0 = clean or info only, 1 = waste findings, 2 = a terms-of-service or privacy high.

## More

- All commands: `tokenstretch --help`. The others are `doctor`, `install`, `bench`, `watch`, `statusline`, `startup`, `nudge`, `auto`, `brief`, `model`, `trim`, and `undo`.
- Technical details and pins: `docs/DETAILS.md`, `ADAPTERS.md`, `tokenstretch/catalog.py`
- Contributing: `CONTRIBUTING.md` · Roadmap: `ROADMAP.md` · Security: `SECURITY.md`

## Say thanks

tokenstretch is free and stays free, with no paid tier and no tracking. If it saves you credits and you'd like to say thanks:

- [Buy me a coffee on Ko-fi](https://ko-fi.com/exjtac)
- Bitcoin (on-chain): `bc1qt892nv80l50grylqddnzf9u4203gm2gqmr22ku`

<img src="docs/images/btc-donate.png" alt="Bitcoin donation QR code" width="140">

Stars, bug reports, and pull requests help just as much.

MIT © 2026 Leverage Lab AI
