# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow semantic versioning.

## [Unreleased]

### Added
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

## [0.1.0] - 2026-11-10

### Added
- `doctor` (W01–W27, T01–T07, P00–P06), Clean-profile `install` / `undo`, the opt-in API-key proxy profile, and `bench` (`--from-logs`, `--bench`, `--prepare` / `--measure`).
- Doctor-only adapters for Gemini CLI, Copilot CLI, Cursor, Aider, OpenCode, and Cline.
