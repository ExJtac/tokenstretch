# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow semantic versioning.

## [Unreleased]

### Added
- `model suggest` and `brief` now suggest a reasoning effort (low, medium, high, xhigh) next to the model tier, from the same task score. `max` is never suggested. For Claude Code the output names the setting to change. `--json` gains `effort` and `effort_why`. A rule of thumb, not a measured result.

### Fixed
- `/credits` works when the app cannot find `tokenstretch` by name. An app started from the desktop often lacks `~/.local/bin` on its PATH, and the old fallback (`python3 -m tokenstretch`) fails when the package lives in the installer's own environment. The shortcut now carries the full command for this machine, and `setup` offers to refresh it when that path changes.

## [0.2.2] - 2026-11-12

### Added
- `setup --list`, `--only <id>` and `--skip <id>`: apply one step of the plan, or leave one out. Each step's id is shown in [brackets] in the plan. `--list` is read-only and skips the checkup; `--only` skips the checkup summary and the pinned-install offer. An unknown id stops with exit code 2 before anything changes.
- `/credits` can apply a fix: after you say yes to one change, the agent runs `setup --only <id> --yes` for that step, with the same backup and `undo`. For anything outside the plan it says where to turn it off instead of editing files. Setup updates an older `/credits` that this tool wrote and still never touches one of your own.
- `doctor` keeps per-log totals in `~/.tokenstretch/cache/doctor-logs.json`, so a repeat run doesn't re-read unchanged session logs. Measured on one machine with about 4 GB of logs: 21 s for the first run, 2 s for the next, identical output. `doctor --no-cache` writes nothing.

### Changed
- `setup` no longer starts a new `CLAUDE.md` or `AGENTS.md` in a project by default. Adding the rules block to an existing file is still in the plan; creating the file is listed as optional and applied only with `--only`.

### Fixed
- `docs/RELEASING.md` says what to do about the network-blocked test run on macOS, where `unshare` doesn't exist.

## [0.2.1] - 2026-11-12

### Added
- `doctor` W28 `helper-wakeups` (Claude Code): reads the last 30 days of session logs and reports conversations where helpers finishing one at a time woke the main conversation 3 or more times and those extra steps re-sent 300K+ tokens. The fix is to start helpers together and wait in the foreground. It is the after-the-fact form of the `watch` wake-up warning.
- `setup`: a `/credits` shortcut for Claude Code, written to `~/.claude/skills/credits/SKILL.md` (about 1 KB, `disable-model-invocation: true`). The agent runs `startup` and `watch --once` itself and reports, in under 150 words, what a new session carries, what is costing credits now, and a step for each that needs no terminal. It changes nothing without a yes. Setup skips it when a `credits` skill or command exists; `undo` deletes it.
- `startup`: connectors are sized by difference. Each run remembers the unsized remainder of the measured start for the current set of connectors in `~/.tokenstretch/startup-history.json` (names and token counts only). When an earlier run saw the same set with one connector more or fewer, the change is reported as that connector's size, and a connector turned off since then is reported as tokens saved per session. Changes under 2K tokens are ignored. A change with the same local set is reported as a plugin or app connector change. `--no-save` keeps the run read-only.
- `brief --review`: a review brief. The helper reads only the named files, changes nothing, reports problems one per line as `path:line: problem. fix.`, and replies `NO ISSUES` when there are none. `--max-reads N` caps file reads. A review of more than 8 files is flagged (`review-too-wide`), and `--split N` prints one brief per group of N files, keeping files from the same folder together, so each group goes to its own helper with a short history. Found in a live test, where one reviewer reading a whole site re-sent about 0.79M tokens.

### Changed
- `watch`, `statusline`, `nudge`: the heavy-start warning no longer assumes a terminal. With the `/credits` shortcut installed the fix says to type `/credits`; without it, it says to ask the agent to run `tokenstretch startup`.

### Fixed
- `doctor`: the search for instruction files under the home folder (W17) no longer walks app data, media, caches, or the trash, and stops after 5 seconds per folder. On one machine with a large home folder a full `doctor` run went from over 10 minutes to 37 seconds.

## [0.2.0] - 2026-11-12

### Added
- `watch`: a cache-expiry warning (Claude Code). Reported from the logs when, after a pause of 5+ minutes, the same model re-wrote at least half of the history to the cache and read back at most a fifth (compactions and model switches aren't counted). Predicted from the clock when a session with 20K+ tokens of history has been idle past the cache lifetime: about 60 minutes on a subscription, about 5 with an API key or a 5-minute cache setting. One warning per session. The dashboard's live warnings show it too; the weekly report shows only the logged form.
- `tokenstretch statusline`: one read-only line for a status bar, with the session's history size against `--max-context` and the most urgent watch warning in short form. It reads the session named by `transcript_path` in the JSON a tool pipes in (only a `.jsonl` inside the tool's log folder is opened), or the newest active session when run by hand. It builds a light context (no `ps`, no `crontab`), prints nothing on any error, and always exits 0.
- `setup`: a `/handoff` shortcut for Claude Code, written to `~/.claude/skills/handoff/SKILL.md` (about 1 KB, `disable-model-invocation: true`, so nothing is added per turn). `/handoff` has the agent write `.claude/handoff.md` (goal, done, state, decisions, next step; 40 lines at most, without reading files or running commands), and `/handoff resume` continues from it after `/clear`. Setup skips it when a `handoff` skill or command already exists at user or project level; `undo` deletes it. With the shortcut present, the long-history and cache-expiry fixes in `watch`, `statusline`, and the dashboard say `/handoff then /clear`.
- `statusline --wrap COMMAND`: shares the one status-line slot with another tool (for example Caveman's). The wrapped command runs through the shell with the same input, and the tokenstretch line is added after its first line. A failing or slow (over 3 seconds) wrapped command leaves the tokenstretch line alone; an empty tokenstretch line leaves the other one unchanged.
- `watch`: a heavy-start warning (Claude Code). When a session's first step already carries more than 40K tokens (`--max-start`), watch says so and points to `trim` and to doctor's W03 and W25. Found in a live test, where a new session carried about 78K before its first action. `statusline` shows it last, after every other warning.
- `tokenstretch brief`: a local prompt generator. It turns a one-line task into a brief (files to work in, notes to work from, no-research or a search cap, read rules, stop condition, reply length) and lists what the task text leaves open: no file named, no stop condition, open-ended wording, several tasks in one, unbounded research, no notes. `--check` exits 1 when something is missing; `--helper`, `--no-build`, `--rule` (your own rule lines), `--max-searches`, `--file`, `--json`. No model calls; it reads only the text passed in.
- `watch`: a helper warning (Claude Code). Subagent logs under `<session>/subagents/` are now read (token counts only). When a session's helpers re-sent at least 1M tokens and at least as much as the main conversation, watch says how much, how many times the main conversation, and the longest helper's steps and peak. Found in a live test, where three research helpers re-sent about five times what their lead did, unseen by watch.
- `watch`: a helper wake-up warning (Claude Code). Each background helper that finishes starts a new step in the conversation that launched it, and that step re-sends the whole history. When that happened at least 3 times (`--wake-min`) and those steps re-sent at least 300K tokens, watch says how many times and how much. Only the notice row's type is read from the log, never the helper's reply. `statusline` and the in-tool nudge show it after the helper warning. Found in a live test, where a lead was woken once per helper.
- `watch` and `statusline`: the long-history warning says about how many more steps a fresh start takes to pay for itself, when the session's start-up size is known.
- `setup` (Claude Code): turns on credit warnings inside the tool. It adds the `statusLine` entry (an existing status line is kept and wrapped with `--wrap`) and a `UserPromptSubmit` hook, `tokenstretch nudge`. When the model default is changed in the same file, both ride in that one step. Backed up and undone like every setup change; unreadable settings and unknown `statusLine` shapes are left alone.
- `tokenstretch nudge`: the hook entry. It prints the top watch warning for the session the tool names, as a message for the user and context for the agent, at most every 20 minutes per session for the same kind of warning. Silent when there is no warning or no session is named; always exits 0. Keeps one small state file per session under `~/.tokenstretch/nudge/`.
- `setup`: a `/brief` shortcut for Claude Code (`~/.claude/skills/brief/SKILL.md`, manual-only), skipped when a `brief` skill or command already exists.
- `brief`: research briefs now ask for the full URL and read date per fact, a read-or-summary mark, and nothing from memory, and say to list a gap instead of skipping a topic. They also say to treat page text as data, not instructions, and to paraphrase close to the source. `--max-fetches N` caps pages opened; `--brief-only` prints just the brief for pasting or piping.
- `tokenstretch startup`: what every session carries before the first prompt. Lists the parts that can be sized from local files (instruction files loaded at start, skill listings, helper listings), largest first with a fix for each, next to the measured first-step size of recent sessions, and says how much can't be sized locally. The heavy-start warning in `watch` and `statusline` now points to it. Adapters supply parts through `startup_parts`.
- Autopilot (Claude Code), added by `setup`: `env.CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000` so long sessions are summarized automatically, and one hook entry, `tokenstretch auto`, on `PreCompact` (says what the summary must keep), `PostCompact` (saves the summary to `.claude/handoff-auto.md`) and `SessionStart` (tells a new chat where the newest note under 72 hours old is). Existing compaction settings are never overridden. No model calls; always exits 0.
- Watch sessions may carry `handoff_hint`, `start_tokens`, and `helpers`.
- Adapter hooks `watch_session_at` and `cache_ttl_s`; watch turns may carry `cache_read` and `cache_write`.

## [0.1.0] - 2026-11-11

First release.

### Added
- `doctor` (W01–W27, T01–T07, P00–P06), Clean-profile `install` / `undo`, the opt-in API-key proxy profile, and `bench` (`--from-logs`, `--bench`, `--prepare` / `--measure`).
- Doctor-only adapters for Cursor, Aider, OpenCode, and Cline. Gemini CLI and Copilot CLI are tier 1 (below).
- Copilot CLI:
  - `bench --bench` reads Copilot's own `--usage-output-file` (`UsageGetMetricsResult`; per-model totals, with `agentMetrics` not added again), before falling back to OTel spans and `events.jsonl`.
  - Sessions are tied to a repo through `session.start` `data.context.cwd` or `workspace.yaml`, so `bench --prepare` / `--measure` include Copilot and `watch` labels sessions by project.
  - `autoTier` is read. Doctor W05 notes Auto on `intelligence`, and `model default` sets `autoTier` when the model is `auto`.
  - A recommended light / mid / top map (`claude-haiku-5.5` / `claude-sonnet-5.5` / `claude-opus-5.5`, GPT-6 alternatives), labeled as a recommendation and not a GitHub list.
- OpenCode doctor T01 (high): the `opencode-gemini-auth` plugin, which reuses Gemini CLI's Google OAuth (against Gemini CLI's terms). Cline and Roo are not flagged on the name alone.
- `model suggest` shows alternatives per tier (Codex light: `gpt-6-luna` or `gpt-6.1-terra`) and a note when a tier map is a recommendation.
- `scripts/install.sh`: a one-line pinned installer for macOS and Linux. It checks for Python 3.11+. It clones the pinned tag and verifies HEAD equals the pinned commit, or downloads that commit's tarball and verifies its pinned SHA-256. It installs into `~/.local/share/tokenstretch` with a private `--without-pip` venv (no pip, no PyPI) and a launcher in `~/.local/bin`. Shell startup files are edited only with `--add-to-path` (backed up first). `--dry-run` prints every step and changes nothing, and `--uninstall` removes exactly what the installer created. No telemetry, and it never runs setup or third-party installs. The pin goes in a separate commit after the release tag.
- `scripts/release-pin.sh TAG`: fills the installer pin from a local tag and commits it. It checks the tagged commit's version, refuses dirty trees and moved tags, and adds the tarball SHA-256 later with `--sha256`. It never tags, pushes, or downloads.
- `tokenstretch.__version__`.
- Codex sessions in `watch`, `status`, `report --weekly`, and `dashboard` are labeled by the working directory recorded in the rollout (`session_meta.cwd`, else the first `turn_context.cwd`), with your home folder shown as `~`. Only a session with no recorded directory falls back to its date. Codex now also appears under `status` → newest sessions.
- Codex tier map for `model suggest` and `model default --tool codex`: `gpt-6-luna` / `gpt-6.1-sol` / `gpt-6-astra` (Codex models page). `model default` sets the top-level `model` in `~/.codex/config.toml`: dry run by default, `--apply` backs up first, `undo` restores. It refuses when the file isn't valid TOML or when the edit would change any other key.
- Gemini CLI and GitHub Copilot CLI are now tier 1. Gemini CLI: usage from `~/.gemini/tmp/<project>/chats/**` session records (incremental, per-turn; `watch`, `status`, `report --weekly`, `dashboard`), `bench --bench` (`gemini -p … --output-format stream-json`, `GEMINI_API_KEY`, usage statistics off in the sandbox) and `bench --prepare`, a Clean-profile SessionStart note (a `systemMessage`, not added to the model's context) and a native `.geminiignore` read gate, setup items (usage statistics off; rules block in an existing `GEMINI.md`), `model default` (`flash-lite` / `flash` / `pro`), and new doctor checks: W14 long sessions, W16 `billing.overageStrategy: "always"`, W20 `model.compressionThreshold` ≥ 0.9, P01 OpenTelemetry export off the machine (high when prompts are logged), P03 local telemetry file with prompts, P05 auto-update. Copilot CLI: usage from `session-state/*/events.jsonl` totals and per-turn `chat` spans from the local OTel file export (spans win for the same session; `watch` needs the file export), `bench --bench` (`COPILOT_GITHUB_TOKEN`, `--disable-client-telemetry`, `--sync-session=off`, a sandbox `COPILOT_HOME`, a fixed `--session-id`, and a local OTel file with content capture off), a rules block in an existing `.github/copilot-instructions.md`, and doctor checks W05 (Opus default), W06 (`effortLevel` xhigh/max; the old adapter read undocumented keys), W13, W16 (`continueOnAutoMode`), W18 (allow-all), P01 (client telemetry unknown unless an alias passes `--disable-client-telemetry`; OTLP HTTP export), P02 (`remoteSessions`), P05 (`autoUpdate`), P06 (`storeTokenPlaintext`; `config.json` is never opened).
- `Adapter.undo(ctx, manifest)` restores a run through the core installer (it raised before).
- `tokenstretch setup`: runs doctor, prints a plain-English plan (what, why, exact file and key, how to undo), asks once (`[y/N]`, or `--yes`), then applies with backups and live progress. Without a terminal and without `--yes`, it's a dry run. Pinned installs (rtk-oss, Caveman skill only) are offered behind a separate prompt because they use the network.
- `tokenstretch status`: what's applied (with per-file state), current default models, and the newest local sessions' history size, with a `/clear` tip over ~150K tokens.
- `tokenstretch report`: a self-contained local HTML report (no external assets).
- `tokenstretch undo` now restores every step of the latest setup run, newest first.
- Adapter hooks `setup_items` and `live_sessions` (Claude Code, Codex, host).
- `tokenstretch watch`: a read-only loop over local session logs (default every 10s, `--once`, `--interval`, `--max-context`, `--max-reads`, optional `--notify` through the OS's own notifier). It warns once per session about long history, the top model on simple steps, repeated reads of the same file (paths only), and fast history growth, each with why it costs credits and a one-line fix. Claude Code fully, Codex without the re-read check.
- `tokenstretch model suggest`: a transparent keyword-and-size heuristic that recommends light / mid / top and names the model per detected tool. No model calls; not a measured claim.
- `tokenstretch model default --tier …`: dry run by default; `--apply` changes the default model (Claude Code `settings.json` `model`) with a backup, and `undo` restores it.
- Adapter hooks `watch_sessions`, `model_tiers`, `is_top_model`, `model_switch_hint`, and `plan_default_model`.
- `tokenstretch report --weekly`: per-week tokens by type and by model from local logs, cost only where the logs report it ("not reported" otherwise; nothing estimated), biggest session histories, waste spots from the watch checks, an observational before/after since setup, and bench results when a measured bench exists. `--weeks`, `--out`, `--json`.
- `tokenstretch dashboard`: a read-only local web page (127.0.0.1 only, one-time key in the URL, Host-header check, no CORS, no external assets) with overview cards, live sessions, live watch warnings, models this week, an 8-week trend, doctor results, setup status and undo, token-saving tools and their telemetry, since-setup, and bench results. Updates every 5 seconds; Ctrl-C stops it.
- Dashboard performance: state is built in a background thread and requests only return the last snapshot (with a "warming up / last updated" status); usage logs are read incrementally (per-file cache keyed by inode, size, and mtime; appended bytes only; files older than `--weeks` skipped unopened; cheap line filtering before JSON parsing); doctor runs in the background with per-file results cached. `report --weekly` and `watch` use the same reader and cache.
- Faster Codex fork-replay filtering on machines with many sessions.
- `bench --measure` saves its result as `result.json` beside the prepare manifest, so the report and dashboard can show it.
- Claude Code logs: a per-line `costUSD` is now read as a reported cost.
- Adapter hook `token_tools` (host): installed token savers and their telemetry state.
- `tokenstretch trim`: lists every instruction file each detected tool loads per turn (via `list_instruction_files`, including Claude `@file` imports) with approx. tokens (characters / 4), the per-turn total, and per 100 turns. Findings with why and fix: exact and near-duplicate blocks (within a file and across files loaded together), long files (`--max-tokens`, default 2,000), big pasted code, tables, logs, and blobs, stale relative paths (stat-only, inside the project, credential-looking names skipped), extra blank lines and trailing spaces, and a duplicated rules block. The proposed edit only removes exact duplicates, tidies whitespace, and with `--move-blocks` moves big blocks to `docs/agent-notes/` with a one-line pointer; it shows a unified diff and before/after approx. tokens. Dry run by default; `--apply` asks `[y/N]` (or `--yes`) and backs up every file for `undo`. `--json`.
- Dashboard: an "Instruction files" card from the same read-only analysis.
- Doctor: W01, W02, and W17 fixes point to `tokenstretch trim`.
- Contributor docs, issue and PR templates, and a CI workflow including the network-blocked run.

### Fixed (during development, before this release)
- Copilot CLI input is now read as including cache read and write, and reasoning as part of output, per GitHub's `/usage` help and OTel docs. The blanket "unverified semantics" flag is gone. Older `events.jsonl` files and the OTel file's line envelope keep an UNVERIFIED note.
- Gemini CLI token buckets are documented as verified: cached tokens are inside input, tool-use prompt tokens are added on top, and thoughts are separate from output. The buckets add up to the record's `total`.
- Claude Code W08 (a shortcut switches the model mid-conversation) is now one finding that lists the shortcuts, instead of one finding per file. Subagent files (`agents/*.md`) and other Markdown under skills or plugins no longer count, because their `model` field doesn't switch the main conversation.
- `bench --measure` no longer labels subscription runs as "API-key mode", and it shows the after-side model.
- W17 (duplicate instruction files) is one grouped finding with a count and up to 5 example paths, instead of one finding per duplicate set.
- Hook scripts planned by `install` keep their executable mode (the installer dropped `mode` from adapter changes).
- Copilot OTel `invoke_agent` spans are no longer counted on top of their `chat` spans, and Copilot log paths moved from the host adapter into the Copilot adapter.
- Privacy findings are no longer reported twice by `doctor` for Gemini CLI, Copilot CLI, Cursor, Aider, OpenCode, and Cline.
- Top-level TOML edits (`openai_base_url` for the API-key proxy profile, Codex `model`) now go before the first `[table]`. Before, a new key was appended to the end of the file, which put it inside the last table.
- Long project paths in `status` are shortened like everywhere else (home shown as `~`, cut on a `/` boundary).
