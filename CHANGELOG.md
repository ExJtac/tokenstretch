# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow semantic versioning.

## [Unreleased]

### Added
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

### Fixed
- `bench --measure` no longer labels subscription runs as "API-key mode", and it shows the after-side model.
- W17 (duplicate instruction files) is one grouped finding with a count and up to 5 example paths, instead of one finding per duplicate set.
- Hook scripts planned by `install` keep their executable mode (the installer dropped `mode` from adapter changes).
- Copilot OTel `invoke_agent` spans are no longer counted on top of their `chat` spans, and Copilot log paths moved from the host adapter into the Copilot adapter.
- Privacy findings are no longer reported twice by `doctor` for Gemini CLI, Copilot CLI, Cursor, Aider, OpenCode, and Cline.
- Top-level TOML edits (`openai_base_url` for the API-key proxy profile, Codex `model`) now go before the first `[table]`. Before, a new key was appended to the end of the file, which put it inside the last table.
- Long project paths in `status` are shortened like everywhere else (home shown as `~`, cut on a `/` boundary).

## [0.1.0] - 2026-11-10

### Added
- `doctor` (W01–W27, T01–T07, P00–P06), Clean-profile `install` / `undo`, the opt-in API-key proxy profile, and `bench` (`--from-logs`, `--bench`, `--prepare` / `--measure`).
- Doctor-only adapters for Gemini CLI, Copilot CLI, Cursor, Aider, OpenCode, and Cline.
