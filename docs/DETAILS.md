# tokenstretch: technical details

The full implementation notes that used to be the README. Start with the top-level README.md.

An open-source, local toolkit that helps people get more out of AI coding credits. It audits waste and terms-of-service risk (`doctor`), measures before/after usage (`bench`), and can apply a small, terms-safe set of local tools (`install`) with an `undo` path.

**Status (v0.5.0):** `doctor`, Clean-profile `install` / `undo`, the opt-in API-key proxy profile (`install --profile api-proxy --proxy headroom|kompressor`; Headroom `--apply` uses a packaged hashed lock (`uv pip install --require-hashes` into `~/.tokenstretch/tools/headroom`); Kompressor apply verifies the release-binary sha256), and `bench` are implemented. Doctor covers W01–W18, T01–T07, and P00–P06. Gemini CLI and Copilot CLI are tier 1; doctor-only adapters cover Cursor, Aider, OpenCode, and Cline. `LIVE-BENCH.md` is the walkthrough: subscription mode (`bench --prepare` then you run the official CLI, then `bench --measure`) is the default, because the kit never copies, symlinks, or opens credential files. Automated `bench --bench` is API-key mode (`claude --bare` with `CLAUDE_CONFIG_DIR`, and `CODEX_HOME` created first) and only passes through `ANTHROPIC_API_KEY` / `CODEX_API_KEY` if you already exported them; values are never read, logged, or stored. Install pins and hashes are packaged in `tokenstretch/catalog.py` (none invented). `install` is dry-run unless you pass `--apply`. Savings numbers come only from measured runs; if there is no data the command prints `no measurement`. Live installs and live headless binaries have not been exercised in this tree (tests use fake runners).

Requires Python 3.11+. Runtime is the standard library only.

```bash
python -m tokenstretch --version
python -m tokenstretch doctor --home PATH --root PATH --sys-snapshot DIR
python -m tokenstretch doctor --privacy --json
python -m tokenstretch bench --from-logs
python -m tokenstretch bench --bench --prompt '…'
python -m tokenstretch install    # dry-run (no writes); add --apply to write
python -m tokenstretch install --apply
python -m tokenstretch install --profile api-proxy --proxy headroom   # dry-run; never part of Clean
python -m tokenstretch undo       # restore the latest manifest under --home
python -m tokenstretch watch [--once] [--interval 10] [--max-context 150000] [--max-reads 3] [--notify]
python -m tokenstretch statusline [--max-context 150000] [--max-reads 3] [--wrap COMMAND]   # reads the tool's JSON on stdin
python -m tokenstretch startup [--all] [--json] [--no-save]   # what a session carries before the first prompt
python -m tokenstretch startup --agent [--json] [--no-save]   # lean profile for a worker agent: what to switch off and what it saves; writes nothing
python -m tokenstretch auto   # hook entry for PreCompact, PostCompact and SessionStart; reads the tool's JSON on stdin
python -m tokenstretch nudge [--max-context 150000] [--max-reads 3]   # prompt-hook entry; reads the tool's JSON on stdin, prints hook JSON or nothing
python -m tokenstretch brief "task" [--files P..] [--from P..] [--done TEXT] [--helper] [--review] [--max-reads N] [--split N] [--no-build] [--rule TEXT] [--preset NAME] [--max-searches N] [--max-fetches N] [--brief-only] [--check] [--json]
python -m tokenstretch model suggest "task" [--file F] [--json]
python -m tokenstretch model cost [--grid] [--sessions 12] [--json]   # where the credits went per model and effort, price-weighted; read-only
python -m tokenstretch model rework [--sessions 10] [--json]   # redo-work estimate per model and effort (heuristic); read-only
python -m tokenstretch queue add "task" [--files P..] [--done TEXT] [--preset NAME] [--hard] | --from-plan FILE   # one task, or one per plan step
python -m tokenstretch queue next [--agent NAME]       # gate, then claim oldest open task and print its brief; exit 3 over the pace line, 4 empty
python -m tokenstretch queue done ID [--note TEXT]  |  queue fail ID --note TEXT   # fail: back to open, needs-lead after 2
python -m tokenstretch queue list [--json]  |  queue release --stale MINUTES  |  queue touch ID  |  queue loop-prompt
# pace (budget governor)
python -m tokenstretch pace set --window NAME --used PCT --resets ISO [--length 7d|5h] [--models REGEX] [--at ISO]
python -m tokenstretch pace clear --window NAME
python -m tokenstretch pace [--window NAME] [--model ID] [--effort E] [--sessions 500] [--json]   # estimates per window; read-only apart from pace.json
python -m tokenstretch pace --gate [--window NAME] [--reserve 3] [--json]   # exit 0 at or under the line, 3 over; 0 when no reading
python -m tokenstretch pace --team N [--model ID] [--effort E] [--json]     # agents at once and hours a day on the line
python -m tokenstretch pace --what-if [--team N] [--json]                   # no re-saved history, re-read halved
python -m tokenstretch report --weekly [--weeks 4] [--out FILE] [--json]
python -m tokenstretch dashboard [--port 8765] [--no-open] [--interval 5] [--weeks 8]
python -m tokenstretch model default --tier light|mid|top [--tool claude|codex|gemini] [--apply]
python -m tokenstretch trim [--project DIR] [--max-tokens 2000] [--max-block-lines 30] [--move-blocks] [--apply [--yes]] [--no-diff] [--json]
```

### watch

`tokenstretch/watch.py` is core only. Adapters supply `watch_sessions(ctx, limit)` (per-turn records: timestamp, model, history tokens, output tokens, tool-call count or `None`, and Read-call file paths), `is_top_model`, `model_tiers`, and `model_switch_hint`. Claude Code reads the tail (4 MB) of the newest `~/.claude/projects/*/*.jsonl` main sessions, merges lines on `message.id`, sums `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` as history, and keeps only `file_path` from `Read` tool calls. Codex uses the rollout parser (`token_count` last usage) and labels each session by its recorded working directory (`session_meta.cwd`, else the first `turn_context.cwd`; the date folder only when neither exists); rollouts give no read paths, so the re-read check doesn't apply. Gemini CLI uses its chat records (history = `tokens.input` + `tokens.tool`, which already include cached tokens; tool-call count from `toolCalls`; no read paths). Copilot CLI uses `chat` spans from the local OTel file export only (`gen_ai.conversation.id` groups a session); without it, watch says it is not supported. Only sessions written in the last 2 hours are checked. Each (session file, condition) pair is reported once per run. The growth check measures against the session's own newest timestamp, not the wall clock. `--notify` calls `osascript` (macOS) or `notify-send` (Linux) through `subprocess` with no shell, if present; no network. Defaults: context 150K, top model on the last 3 steps each under 400 output tokens and at most 1 tool call, a file read 3+ times, history growth of 60K in 5 minutes.

Limits left at their defaults scale with price: `WatchConfig.limits(model)` divides the history, growth and helper wake-up limits by `cost.reread_weight(model)` (never below `min_context`, `min_growth`, `min_wake_sent`; never raised for a cheaper model). A limit passed on the command line is used as passed.

### model

`tokenstretch/models.py`: `suggest_tier` adds +2 per top rule hit (architecture/design, broad refactor, migration/rewrite, concurrency/leak, security, hard debugging, performance, distributed/scale), -2 per light rule hit (rename, typo, formatting/lint, docs/comments, version bump, lookup/summary), -1 per mid hint (tests, small fix), +1 for 5+ file names, +1 for 150+ words, and -1 for a short single-target ask. Score ≥ 2 is top, ≤ -2 is light, else mid. It's a heuristic, not a measured claim. `model default` asks each adapter's `plan_default_model(ctx, tier)` for a `SetupItem` and applies it with `setup.apply_plan` (backup + manifest), so `undo` restores it. Claude Code's tiers are haiku / sonnet / opus and it edits `model` in the last settings file that sets it (else `~/.claude/settings.json`). Gemini CLI's tiers are `flash-lite` / `flash` / `pro` (documented aliases) and it edits `model.name` in `~/.gemini/settings.json` (it skips files that aren't plain JSON). Codex and Copilot CLI have no tier map until model names are verified.

### pace

`tokenstretch/pace.py`. State is `pace.json` under the tokenstretch home: per window the length, reset time, optional model regex and up to 20 readings (percent used, time read). A reset time that moves by under an hour is the same period; a later one, a different length, or a lower percent starts the readings over. Units come from `cost.cost_view` over the steps logged inside the window and on matching models; helpers carry no timestamps, so their counts are scaled by the share of the main steps inside the range. Calibration: units per 1% is the slope between the first and newest readings when they differ in both percent and units, otherwise units since the window started over the newest percent; the estimate adds the units since the newest reading. The even-pace line is the share of the window gone times 100 minus the reserve. `--gate` exits 0 when every checked window is at or under its line (or none can be calibrated), 3 otherwise; the wait is the time until the share of the window gone, minus the reserve, reaches the estimate, capped at the reset. `--team` uses units per step (main plus helpers) and steps per hour (main steps that followed another within 10 minutes) of the chosen model and effort, and divides units left by the hours left; a window whose `--models` pattern does not match the model is skipped. `--what-if` removes the "saved again" bucket and halves the "re-read" bucket. `watch` runs the check at most once a minute and only when `pace.json` exists. Estimates from price ratios, not a bill; unpriced models are listed, never guessed.

### trim

`tokenstretch/trim.py` is core only. The file list comes from each detected adapter's `list_instruction_files` (only `always_loaded` files), so tool paths stay in the adapters: Claude Code (user and project CLAUDE.md, `.claude/CLAUDE.md`, ancestors, nested copies, `@file` imports, MEMORY.md), Codex (`~/.codex/AGENTS.md` and the AGENTS.md chain), Gemini CLI (GEMINI.md or the configured `context.fileName`), Copilot CLI (`.github/copilot-instructions.md`, `.github/instructions/*.instructions.md`, AGENTS.md, CLAUDE.md, GEMINI.md), Cursor (`.cursor/rules/*.mdc`, AGENTS.md), and the rest. Files are de-duplicated by resolved path, and each keeps the list of tools that load it. Only files under your home or the project folder are opened; others are counted from their size. Files with credential-looking names are never opened.

Tokens are approximated as `ceil(characters / 4)` and always labeled approx. Per tool, "per turn" is the sum over the files that tool loads, and "per 100 turns" is that times 100. There are no other savings claims.

Each file is split into blocks: headings, paragraphs, bullets (with their indented continuation lines), fenced code, tables, and the tokenstretch rules block (between its markers). Checks:

- **duplicate** (med): a block of 20+ characters whose text (trailing spaces ignored) already appeared earlier in the same file or in a file loaded by at least one of the same tools. User files come first, then project files.
- **near-duplicate** (low, report only): the same words after lowercasing and dropping punctuation and bullet markers, or a `difflib` ratio of at least 0.9 for paragraphs and bullets of 60+ characters (with a length and word-bigram prefilter and a fixed comparison budget).
- **long-file** (med, high over 2×): over `--max-tokens` (default 2,000).
- **big-block** (med): a fenced block over `--max-block-lines` (default 30), a table over 20 rows, a paragraph or fence of 8+ lines where 60% look like log or stack-trace lines, or a line with a 200+ character base64-like run or 500+ characters with almost no spaces.
- **stale-path** (low, report only): `dir/name.ext` mentions in project files that don't exist relative to the project root or the file's folder. URLs, absolute and `~` paths, `..` segments, domain-like first segments, placeholders like `path/to/`, and credential-looking names are skipped without being touched. Existence is checked with `os.path.lexists` only, never by opening, and only for paths inside the project.
- **whitespace** (info): extra blank lines and trailing spaces outside fenced code.
- **rules-duplicate** (med): a second tokenstretch rules block loaded in the same turn.

The proposal only (1) removes exact duplicate blocks, keeping the first copy, and across files only when every tool that loads the later file also loads the file with the kept copy (otherwise it's reported and left alone); (2) collapses blank-line runs and strips trailing spaces outside fences, and drops blank lines at the end of the file; (3) with `--move-blocks`, replaces big fenced code, log paragraphs, and blob paragraphs in project files with `See docs/agent-notes/<stem>-<n>.md when needed (...)` and writes the block verbatim to that file under the project root (`<n>` skips names that already exist). User-level files never get blocks moved, and non-UTF-8 files are never edited. CRLF line endings are kept. Apply goes through `installer.apply_changes` (backup + one manifest with profile `trim`), so `tokenstretch undo` (latest) or `tokenstretch undo MANIFEST` restores the edited files byte for byte, deletes the created notes, and removes the folders it created. The dashboard's "Instruction files" card calls the same analysis (read-only) in its background builder, alongside doctor (every 5 minutes or on request).

### report --weekly

`tokenstretch/weekly.py` is core only. Records come from every adapter's `read_usage_logs`; weeks start Monday in local time; empty weeks are kept; records without a timestamp are counted as undated and left out of every week. Per week it sums uncached input, cache read, cache write, output, and reasoning tokens, overall and per model. Cost is the sum of `cost_usd` from records whose logs report one (Claude `costUSD` / `total_cost_usd` / `cost_usd`, Codex `cost_usd`, host logs), labeled "reported for N of M steps" when only some do, and "not reported" when none do. There is no price table, so nothing is estimated. Top sessions and waste spots reuse `watch_sessions` and `watch.session_warnings` over the newest sessions (not only active ones). "Since setup" compares equal windows (up to 28 days) before and after the earliest recorded setup/install manifest: steps, average input and output per step, and the share of top-model steps. It's labeled observational, not a bench claim. Bench results are shown only from `~/.tokenstretch/benches/*/result.json`, which `bench --measure` now saves beside its prepare manifest.

### dashboard

`tokenstretch/dashboard.py` serves one self-contained HTML page (inline CSS and JS, no fonts, images, or external URLs) and JSON from `http.server` in the standard library. It never writes a file.

Requests never compute anything. A background thread builds the state in three parts and publishes each as soon as it's done: live sessions and watch warnings (every pass, `--interval`, default 5 s; only sessions written in the last 2 hours), usage / setup / bench (at most every 30 s), and doctor (every 5 minutes, or when you press the button, which only sets a flag). The page is static HTML, and `api/state` returns the last published JSON, with `warming_up`, `pending`, and `updated_local` fields the page shows. GC is paused while a part builds (then the survivors are frozen) so a long collection can't stall the HTTP thread.

Log reading is incremental (`tokenstretch/logcache.py`, core only). Each file is cached by (path, inode, size, mtime): unchanged files are skipped, files that only grew are parsed from the saved byte offset (a half-written last line waits for the next pass), and a file that shrank or was replaced is parsed again. Files whose mtime is older than the window (`--weeks`, default 8) are skipped before they're opened, which covers old Codex archived sessions. Lines are filtered with a substring check before `json.loads` (Claude: lines with `"usage"`; Codex: usage, session, model, and token-count markers), so prompts, code, and tool output are never parsed. Adapters opt in with `incremental_logs`, `usage_log_parser(path)`, and `finish_usage_records`. Session tails for watch/status and doctor's per-file log checks (W13–W15, Codex W14) are memoized by the same stat key, so a doctor re-run only reads changed files. `report --weekly` uses the same reader (fresh each run). The Codex fork-replay filter now groups sessions by their first two events and compares prefixes with hashes, instead of comparing every pair step by step.

On a synthetic 1 GB Claude + 2.7 GB Codex home (8-core Linux box): the page and first `api/state` answer in a few ms; usage numbers land after about 9–10 s on the first pass (about 2 GB inside the 8-week window); doctor after about 18 s; later polls take a few ms (max 28 ms while a build runs), and an unchanged usage rebuild takes about 0.5 s.

Security model:

- **Loopback only.** It binds `127.0.0.1` and refuses any other `--host` (exit 2). If the port is busy it binds a free one and says so. `SO_REUSEADDR` is off so it can't share a port with another listener.
- **Per-run token.** Each run makes a random 32-character key (`secrets.token_urlsafe(24)`) and puts it in the URL path: `http://127.0.0.1:PORT/KEY/`. Every request without the exact key (constant-time compare) gets 403, so other local programs' pages and websites you visit can't read it.
- **Host header check.** The `Host` header must be `127.0.0.1` or `localhost` (with or without the port), else 403. That blocks DNS-rebinding pages that resolve their own name to 127.0.0.1.
- **No cross-origin access.** No CORS headers are ever sent, preflight `OPTIONS` is refused, and a request with a foreign `Origin` header gets 403.
- **Headers.** `Content-Security-Policy: default-src 'none'` with only inline script/style and same-origin fetch allowed, `Referrer-Policy: no-referrer` (the key never leaks in a Referer), `X-Frame-Options: DENY`, `Cache-Control: no-store`, `nosniff`. Request logging is off so the key isn't printed per request.
- **What it shows.** Paths under your home are shown as `~/…`, finding text goes through the same secret masking as doctor, and credential files are only ever stat'd.

Tests run the server on 127.0.0.1 with the socket guard narrowed to loopback only. Under `unshare -rn` the `p00_outer` run brings up `lo` (`ip link set lo up`) so they run there too; in a namespace without loopback they skip with the reason `LOOPBACK-UNAVAILABLE` instead of passing silently.

`doctor` changes none of your files. Its only write is its own cache, `~/.tokenstretch/cache/doctor-logs.json`: per-log totals keyed by each session log's inode, size and modified time, so an unchanged log isn't parsed again. A log modified in the last minute is never cached, the file is dropped when the version changes, and `doctor --no-cache` neither reads nor writes it. Exit codes: 0 = clean (or info only), 1 = waste findings, 2 = a ToS or privacy high.

## Privacy guarantees

These are the product guarantees. What is actually enforced in this tree today is listed after the block.

> **Privacy guarantees**
> - **Zero network calls from the kit itself.** `doctor` and `bench` read local files only. The kit has no telemetry, analytics, crash reporting, update checks, or "phone home." The only network traffic is the install step you run yourself, which downloads pinned versions (exact tags, with checksums where upstream publishes them).
> - **Your prompts, code, and logs never leave your machine because of this kit.** Your agent still talks to your own provider (Anthropic or OpenAI), exactly as it did before.
> - **The default profile ships only tools that run fully local with telemetry off:** RTK (OSS edition; telemetry off by default), the Caveman skill (no CLI, no cloud), Microsoft TokenEconomizer, and ccusage in `--offline` mode.
> - **No credential handling.** The kit never reads, copies, or proxies your Claude/ChatGPT login, API keys, or tokens. There is no proxy on your subscription login and no MITM certificates.
> - **Opt-in only, and loud about it.** Any optional tool that has telemetry or cloud features is off unless you choose it. When you do, the installer turns its telemetry off first and tells you exactly what it would send.
> - **Checkable.** Run `doctor --privacy` to list every setting, env var, and process on your machine that sends data off-box. Run the kit under a firewall or `unshare -rn` and it still works.

**Enforced today:** `doctor`, `trim`, and `bench --from-logs` are local-only and the package imports no outbound network libraries (the dashboard's loopback-only `http.server` is the only socket the kit opens). Tests include an in-process socket guard and a suite run under `unshare -rn` (skipped if that namespace is unavailable). Credential files are stat'd, never opened. `install` is dry-run unless you pass `--apply`. License checks use packaged snapshots (nothing is fetched). Pins: RTK OSS v1.2.0 commit `e288a18c…` (rtk-oss-* assets only, install.sh and checksums.txt sha256 pinned; linux-gnu x64 and Windows arm64 are reported as unsupported oss platforms); Caveman skill-only at tag v4.1.0 / `1e91f1306d…` (copy `skills/caveman` only); TokenEconomizer v0.1.0 / `ec8ff5ec…` (commit-pinned, no assets) with Clean config `microskills=off`, `toolprune=propose`, `loopguard=off`; ccusage@20.0.48 from a pinned lockfile (`npm ci --ignore-scripts --no-audit --no-fund`, `npm_config_update_notifier=false`) plus `ccusage.json` `defaults.offline=true` and a wrapper that always passes `--offline`. Tests never run real install commands or live headless agents. Live `--bench` uses a throwaway copy and never writes the real repo or HOME. The opt-in API-key proxy profile uses packaged pins: Headroom v0.44.0 commit `b21479e0…` with wheel/sdist sha256 and a packaged universal `--require-hashes` lock (`tokenstretch/pins/headroom/requirements.txt`, includes manylinux x86_64 wheel `3b5c7fd2…`); install is `uv venv --python 3.13` then `uv pip install --require-hashes -r <lock>` into `~/.tokenstretch/tools/headroom` (`UV_NO_PROGRESS=1`). macOS below 14.0 is refused (onnxruntime only ships a macosx_14_0_arm64 wheel); the macOS lock was compiled on Linux and has not been install-tested on a real Mac. Claude is pointed at `http://127.0.0.1:8787` (no path); Codex/OpenAI at `http://127.0.0.1:8787/v1`; Kompressor 1.5.14 commit `9e4721ab…` installs the musl or gnu x86_64 release binary and verifies its sha256. Beacon env is always set off (`HEADROOM_BEACON`, `HEADROOM_TELEMETRY`, `HEADROOM_UPDATE_CHECK`, `DO_NOT_TRACK`, `HEADROOM_OFFLINE`) and the start command passes `--no-telemetry`. **Not done yet:** doctor-only adapters for Goose and Devin. See `ADAPTERS.md` for the interface and paths, and `ROADMAP.md` for unverified items.

## Tests

```bash
python -m pytest -q
```

The suite is also run under `unshare -rn` (the `p00_outer` test, or `pytest -m 'not p00_outer'` inside that namespace). Fixtures under `tests/fixtures/` are fake configs and logs. They contain no real secrets. Install and headless tests use fake runners only; they make no network calls.

## Verified vs unverified

- Claude Code settings, CLAUDE.md locations, and transcript dir `~/.claude/projects/**/*.jsonl` follow official docs. Token field names used in the parser are the usual ccusage-style names and have not been confirmed against a live session file in this repo.
- Codex rollout paths and token fields (`$CODEX_HOME/{sessions,archived_sessions}/**/rollout-*.jsonl`, `event_msg`/`token_count`, `token_usage_record`) are **verified-in-source**. Dedupe of repeated totals and forked-session replays follows ccusage (ccusage-only).
- Cline `metrics.inputTokens` already includes cache; uncached input is `inputTokens - cacheReadTokens - cacheWriteTokens` (floored at 0). Legacy Cline `ui_messages.json` `tokensIn` is still flagged **unverified semantics**. A normalized `output` never includes `reasoning`; sources whose output already contains reasoning (Copilot) are split, so the five buckets add up to the total.
- Gemini CLI chat records (`~/.gemini/tmp/<project>/chats/**/*.jsonl`, legacy `*.json`; project path from `.project_root`) and the `stream-json` `result` stats are **verified-in-source** (gemini-cli `chatRecordingService.ts`, `config/storage.ts`, `config/projectRegistry.ts`, `output/types.ts`). `tokens.input` is the API's `promptTokenCount`, which includes `cachedContentTokenCount`; `tokens.tool` (`toolUsePromptTokenCount`) is added on top of input, and `tokens.thoughts` is kept separate from output as reasoning. This is **verified-in-docs** (Vertex `UsageMetadata`: total = prompt + candidates + tool-use prompt + thoughts) and in the pinned gemini-cli source, which copies the fields 1:1.
- Copilot CLI `events.jsonl` `session.shutdown` and `--usage-output-file` field names are **verified-in-source** (CLI 1.0.109 typings). Input including cache read and write, and reasoning being inside output, are **verified-in-docs** for `/usage` and the OTel metrics. The `inputTokens` field comment doesn't say so, so older `events.jsonl` files carry an UNVERIFIED note. Session cwd comes from `session.start` `data.context` or `workspace.yaml` (verified-in-help). OTel `chat` span attribute names are **verified-in-docs**; the file's line envelope is **UNVERIFIED**, so both a plain span and the OTLP JSON envelope are accepted. OpenCode input excludes cache (verified-in-source).
- The product name `tokenstretch` was available on GitHub, npm, and X at the time of the name check (2026-11-10). No trademark search has been run.

## License

MIT for this repository. Third-party tools in the Clean profile, and Headroom / Kompressor in the opt-in API-key profile, are checked against packaged license snapshots (Apache-2.0 or MIT only) before any apply step. Artifact hashes and commits are packaged in `tokenstretch/catalog.py` and `tokenstretch/license_snapshots/`; this tree does not invent checksums. Headroom and Kompressor have license snapshots (Apache-2.0 / MIT) and the artifact hashes quoted in the catalog.
