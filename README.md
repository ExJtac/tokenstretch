# tokenstretch

**Stop paying for wasted AI coding credits. One command finds the waste, explains it, and fixes it, with undo.**

tokenstretch looks at how Claude Code, Codex, Gemini CLI, and Copilot CLI are set up on your computer, shows you in plain English where your credits are going, and applies a small set of safe fixes, only after you say yes. It's **free and open source (MIT)** and runs **100% on your machine**: no account, no sign-up, no telemetry, no server. Every file is backed up first, and `tokenstretch undo` puts everything back.

It doesn't replace your tools. It checks, configures, and measures them, including RTK and Caveman.

## Support tokenstretch

tokenstretch is free and stays free, with no paid tier and no tracking. If it saves you credits and you'd like to say thanks:

- [Buy me a coffee on Ko-fi](https://ko-fi.com/exjtac)
- Bitcoin (on-chain): `bc1qt892nv80l50grylqddnzf9u4203gm2gqmr22ku`

<img src="docs/images/btc-donate.png" alt="Bitcoin donation QR code" width="140">

Stars, bug reports, and pull requests help just as much.

## Quick start

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

![The tokenstretch dashboard: live sessions, warnings, model usage, and weekly trend](docs/images/dashboard.png)

<sub>The local dashboard (`tokenstretch dashboard`), shown with sample data.</sub>

## What it does

| Command | What it does |
|---|---|
| `tokenstretch start` | Checks your computer, lists every change in plain words, asks once, then applies the safe ones. |
| `tokenstretch doctor` | 40+ read-only checks: wrong default model, giant sessions, duplicate instruction files, telemetry left on, proxy chains, terms-of-service risks. |
| `tokenstretch undo` | Puts back every file setup changed. |
| `tokenstretch update` | Shows what changed in a newer release and installs it after you say yes. Nothing checks in the background. |
| `tokenstretch watch` / `statusline` | Live warnings while you work, in a terminal or your tool's status bar. |
| `tokenstretch doctor --new` | Only the findings that are new since the last check. No model, no credits. `watch` does the same every 30 minutes. |
| `tokenstretch dashboard` | One live page on your own machine (127.0.0.1): sessions, warnings, weekly pace. |
| `tokenstretch trim` | Shows what your CLAUDE.md, AGENTS.md, GEMINI.md cost per turn and proposes a safe trim (dry run unless `--apply`). |
| `tokenstretch startup` / `connectors` / `outputs` | What every session carries before the first prompt, which connectors you never call, and which tool outputs fill your sessions. |
| `tokenstretch model suggest` / `model cost` | Right-sized model and effort for a task, and where your credits went per model. |
| `tokenstretch brief` / `queue` / `pace` | Cheap task briefs, a file-based task queue for agent teams, and pacing a plan's weekly limit. |
| `tokenstretch report` / `bench` | A local HTML report, and real before/after measurements on your own repo. |

For Claude Code, setup also adds `/handoff`, `/brief` and `/credits` shortcuts and a read-only `tokenstretch-advisor` helper you can ask for a credit review, and can summarize long sessions automatically so a new chat carries on from a note. Every feature in detail: [docs/DETAILS.md](docs/DETAILS.md#what-you-get).

## Ways it cuts waste

Biggest first. The order is an estimate from model price ratios and real usage logs, not a per-feature measurement.

1. **The right model for the job.** Everyday work goes to the mid-tier model, and helpers run on a cheaper one. The same tokens cost about 3x less on the mid model than on the top one (`model suggest`, `model default`).
2. **Shorter sessions.** Every step re-sends the whole conversation history, and on long chats that re-read can be a third or more of the cost. `/handoff` + `/clear`, autopilot summaries, `watch`, and in-chat warnings tell you when to start fresh without losing your place.
3. **A lighter start-up.** Connectors, plugins, and skills you never use are loaded and re-sent on every step. `startup` and `connectors` show what each one costs and switch off the unused ones.
4. **Less new content per step.** A short rules block tells the agent to search before reading, read line ranges instead of whole files, and keep output short. `outputs` shows which tool outputs fill your sessions.
5. **Disciplined helpers.** Narrow `brief`s and a note when a helper starts keep sub-agents from re-reading everything.
6. **Trimmed instruction files.** `trim` and `trim --to-skills` shrink CLAUDE.md, AGENTS.md, and GEMINI.md, which are paid for on every step.
7. **Effort and cache warnings.** It flags high reasoning effort on simple tasks, and a break long enough for the cache to expire, which makes the next step re-send the history at full price.
8. **Pacing.** `pace` and `queue` spread work across your weekly limit. They don't cut usage, but they help you avoid hitting the wall mid-week.

The telemetry and terms-of-service checks are about privacy and safety, not savings. To see what it does for you, run `tokenstretch report --weekly` a week after setup, or `tokenstretch bench` on your own repo.

## Privacy promises

- **No telemetry, no analytics, no phone-home, no update checks unless you run `tokenstretch update`.** No outbound network libraries: that one command starts `curl` to read two public files and sends nothing about you. The only socket tokenstretch itself opens is the optional dashboard, bound to 127.0.0.1 with a one-time key, and tests block every non-loopback connection.
- **No network calls**, except the pinned installs you confirm in a separate prompt and `tokenstretch update` when you run it.
- **Never opens credential files.** Login files and tokens are only `stat`'d (checked for existence and permissions), never read, copied, symlinked, or proxied. Tests plant canary credentials and fail if any are opened.
- **Your prompts, code, and logs never leave your machine because of this tool.** Session logs are read locally: for model, usage and history size, for counts of tool and connector calls, and for the memory check (`doctor` W29), which reads up to 2 MB from the end of the newest 5 session logs in a project's folder and only counts the rows that name another project.
- **Settings files that can hold a key are read for one thing at a time.** `~/.claude/settings.json` and a plugin's `.mcp.json` can hold an API key next to ordinary settings. tokenstretch reads them for what a command needs (which plugins are on, how many connectors, whether a hook is set); no value from them is kept, logged or shown. [Where a switched-off connector's entry is held](docs/DETAILS.md#see-which-connectors-you-use).
- **Every file change is backed up**, and `undo` restores it.
- **Checkable:** the whole test suite also runs with networking disabled (`unshare -rn`).

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

## Supported tools

| Tier | Tools | What works |
|---|---|---|
| 1 (full) | **Claude Code**, **Codex**, **Gemini CLI**, **Copilot CLI** | doctor, setup, status, watch, report, dashboard, model suggest, bench, Clean-profile hooks, undo (`model default`: Claude Code, Codex, Gemini CLI, Copilot CLI) |
| Doctor-only | Cursor, Aider, OpenCode, Cline | doctor checks and privacy listing |
| Planned | Goose, Devin (see `ROADMAP.md`) | |

Gemini CLI and Copilot CLI have a few limits (Copilot per-turn usage needs its local OpenTelemetry export, for example): see [the details](docs/DETAILS.md#gemini-cli-and-copilot-cli-limits).

Every tool-specific path lives in its adapter (`tokenstretch/adapters/`). To add one, see `CONTRIBUTING.md` and `ADAPTERS.md`.

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
- It adds a `tokenstretch` launcher in `~/.local/bin`. The launcher always runs the installed copy, whatever folder you are in.
- It never edits your shell startup files unless you pass `--add-to-path`, and it backs them up first when you do.
- It never runs `setup` or installs other tools. There's no telemetry and no install ping.

`sh install.sh --uninstall` removes exactly what it created: the folder, the launcher, and the PATH line. Your `~/.tokenstretch` backups stay, so run `tokenstretch undo` first if you want setup's changes reverted.

Options: `--add-to-path`, `--python PATH`, `--data-dir DIR`, `--bin-dir DIR`, `--dry-run`, `--uninstall` (`sh install.sh --help` lists them). For an install link that can never change, use the commit link in the release notes instead of `main`. The installer doesn't use pipx, because pipx would download build tools from PyPI. To install from a clone instead, see [the clone quickstart](docs/DETAILS.md#30-second-quickstart).

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

## FAQ

**Does it replace RTK or Caveman?** No. It checks, configures, and verifies them. It installs the safe editions from pinned sources (RTK's OSS edition, Caveman as a skill only), keeps their telemetry off, warns when two compressors or proxies stack up, and lets you measure whether they help on your repo. It also checks the token savers you already have (RTK, Caveman, TokenEconomizer, ccusage, Headroom, Kompressor, and similar) to confirm they're set up right and actually working. On one real machine, for example, it found Caveman's telemetry on and RTK's commands being blocked by Claude's permissions.

**Will it slow my agent down or cut work?** No. The biggest win is choosing the right model for the job and not re-reading old history. The top model is one `/model` command away whenever you need it.

**Does it change anything without asking?** No. Doctor, status, report, watch, statusline, dashboard, and model suggest are read-only. `nudge` (the prompt hook setup adds) changes none of your files; it only keeps its own small state file under `~/.tokenstretch/nudge/`. `doctor` keeps per-log totals in `~/.tokenstretch/cache/doctor-logs.json` (log paths and token counts, nothing else) so a second run takes seconds instead of re-reading every session log; `doctor --no-cache` writes nothing, and the file is safe to delete. The one exception you switch on yourself: with `pace auto` on, `statusline` stores plan readings in tokenstretch's own `pace.json`.

**Do I have to run anything after setup?** No. For Claude Code, setup puts the warnings in the status line and in the session, sets the default model, and adds the `/handoff`, `/brief`, and `/credits` shortcuts. If you never open a terminal, type `/credits` in the chat to see what a new session carries and what is wasting credits now. `watch`, `dashboard`, and `report` are there if you want more detail. Other tools get the rules block and model default; live warnings for them are on the roadmap. `trim` and `model default` are dry runs unless you pass `--apply`. Setup asks once, and it's a dry run without a terminal unless you pass `--yes`.

**Does it touch my login or API keys?** Never.

**Is it really free?** Yes. It's MIT licensed, with no paid tier, no account, and no server.

**What's the command exit code for doctor?** 0 = clean or info only, 1 = waste findings, 2 = a terms-of-service or privacy high.

## Full guide

Long reference material lives in [docs/DETAILS.md](docs/DETAILS.md):

- [What `tokenstretch start` does](docs/DETAILS.md#what-tokenstretch-start-does)
- [Install from a clone, and every command](docs/DETAILS.md#30-second-quickstart)
- [Update to a newer release](docs/DETAILS.md#update)
- [What setup changes, and how to undo it](docs/DETAILS.md#what-setup-changes-and-how-to-undo-it)
- [Watch while you work](docs/DETAILS.md#watch-while-you-work)
- [New findings only, with no model](docs/DETAILS.md#new-findings-only-with-no-model)
- [Autopilot: shorter sessions with nothing to type](docs/DETAILS.md#autopilot-shorter-sessions-with-nothing-to-type)
- [Start fresh without losing the task (`/handoff`)](docs/DETAILS.md#start-fresh-without-losing-the-task)
- [In your status bar](docs/DETAILS.md#in-your-status-bar)
- [Dashboard](docs/DETAILS.md#dashboard)
- [See what every session starts with](docs/DETAILS.md#see-what-every-session-starts-with)
- [See which connectors you use](docs/DETAILS.md#see-which-connectors-you-use)
- [See which tool outputs fill your sessions](docs/DETAILS.md#see-which-tool-outputs-fill-your-sessions)
- [Run a team at full effort](docs/DETAILS.md#run-a-team-at-full-effort)
- [Trim instruction files](docs/DETAILS.md#trim-instruction-files)
- [Write a brief that stays cheap](docs/DETAILS.md#write-a-brief-that-stays-cheap)
- [Pick the right model](docs/DETAILS.md#pick-the-right-model)
- [Make a week last at full effort](docs/DETAILS.md#make-a-week-last-at-full-effort)
- [Gemini CLI and Copilot CLI limits](docs/DETAILS.md#gemini-cli-and-copilot-cli-limits)

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
