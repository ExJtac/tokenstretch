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
- Contributor docs, issue and PR templates, and a CI workflow including the network-blocked run.

### Fixed
- `bench --measure` no longer labels subscription runs as "API-key mode", and it shows the after-side model.
- W17 (duplicate instruction files) is one grouped finding with a count and up to 5 example paths, instead of one finding per duplicate set.

## [0.1.0] - 2026-11-10

### Added
- `doctor` (W01–W27, T01–T07, P00–P06), Clean-profile `install` / `undo`, the opt-in API-key proxy profile, and `bench` (`--from-logs`, `--bench`, `--prepare` / `--measure`).
- Doctor-only adapters for Gemini CLI, Copilot CLI, Cursor, Aider, OpenCode, and Cline.
