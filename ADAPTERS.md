# tokenstretch adapters

The core (`doctor`, `bench`, `install` / `undo`) talks only to the `Adapter` interface in `tokenstretch/adapters/base.py`. Tool-specific paths, log formats, hook event names, and terms wording live in the adapter modules. The core must not hard-code them.

v0.1 stays in Python and implements the Adapter interface below.

## Interface

Each adapter implements:

| Method | Role |
|---|---|
| `detect(ctx)` | Returns a `DetectResult` (truthy if the tool is present). |
| `read_config(ctx)` | `{effective: {key: {value, from}}, raw: [...]}`. |
| `list_instruction_files(ctx)` | Instruction / memory files (CLAUDE.md, AGENTS.md, skills, …). |
| `list_mcp_servers(ctx)` | Configured MCP servers. |
| `read_usage_logs(ctx, since_days=30)` | Local usage records, already normalized. |
| `install_hooks(ctx, plan, dry_run=True)` | **Planner only** — returns file changes, writes nothing. The installer applies backups and the manifest. |
| `run_headless_task(ctx, task, opts)` | Refuses unless `opts["bench"]` is true. Runs only through a runner (`FakeHeadlessRunner` in tests). |
| `undo(ctx, manifest_path)` | Adapter stub raises `NotImplementedError`. CLI undo uses `installer.undo_manifest`. |
| `watch_sessions(ctx, limit)` | Per-turn records for `watch` (model, history tokens, output tokens, tool-call count, Read paths only). `None` = not supported yet. Read-only. |
| `model_tiers()` / `is_top_model(model)` / `model_switch_hint(model)` | Model picker facts: `{light, mid, top}` names (empty = no tier map), top-tier test, and the in-tool switch command. |
| `plan_default_model(ctx, tier)` | **Planner only**: a `SetupItem` that sets the default model; the core applies it with backup + manifest. |

Normalized `UsageRecord` fields: `ts`, `session_id`, `model`, `is_subagent`, `input_tokens` (uncached), `cache_read_tokens`, `cache_write_tokens`, `output_tokens`, `reasoning_tokens`, `cost_usd` (only when the log actually has it), `source`.

HookPlan items, mapped inside the adapters (not the core): `pre_tool_shell_filter`, `pre_read_gate`, `session_start_note`.

Privacy contract: adapters never fetch, never copy or symlink credential files, never open them (stat only: presence, mode, and permissions via `ctx.exists()` / `ctx.file_stat()`), and never invent savings. Cursor `state.vscdb` is never opened.

`REGISTRY` is Host, Claude Code, Codex, then the doctor-only adapters (Gemini CLI, Copilot CLI, Cursor, Aider, OpenCode, Cline). Claude and Codex are tier 1. Host and the doctor-only adapters are tier 2. Doctor-only adapters implement `detect`, `read_config`, `list_instruction_files`, and `list_mcp_servers`, and plug existing check IDs. `install_hooks` returns no changes (`refused: doctor-only`). `run_headless_task` raises `HeadlessRefused` even with `--bench`.

## Claude Code (`adapters/claude_code.py`)

- **Home / env:** `$CLAUDE_CONFIG_DIR` or `~/.claude`.
- **Settings:** `~/.claude/settings.json`, `~/.claude.json`, project `.claude/settings.json`, `.claude/settings.local.json`.
- **Instructions:** `CLAUDE.md` (user and project), `.claude/CLAUDE.md`, `MEMORY.md`, `.claude/skills`, `.claude/commands`, `.claude/plugins`.
- **MCP:** project `.mcp.json` and settings `mcpServers`.
- **Credentials (stat only):** `~/.claude/.credentials.json` (redirected by `CLAUDE_CONFIG_DIR`, including the macOS Keychain entry).
- **Logs:** `~/.claude/projects/**/*.jsonl`.
- **Headless:** API-key `--bench` runs `claude --bare -p … --output-format json` with `CLAUDE_CONFIG_DIR` pointed at an empty sandbox dir. Subscription mode prints the official command `claude -p … --output-format json` for the user to run (no `--bare`).
- **Install paths this adapter owns:**
  - hooks on `~/.claude/settings.json` (PreToolUse/Bash `rtk`, SessionStart)
  - native `permissions.deny` on project `.claude/settings.json` (`pre_read_gate`)
  - Caveman skill: `~/.claude/skills/caveman` (skill-only copy; never `caveman-setup`)
  - `~/.claude/ccusage.json` is written by the host support-file planner (`{"defaults":{"offline":true}}`)
  - API-key proxy profile only: `~/.claude/settings.json` `env` (adapter-owned, never shell rc) sets `ANTHROPIC_BASE_URL=http://127.0.0.1:<port>` with no path (Headroom: `http://127.0.0.1:8787`) and the quoted telemetry-off variables. `_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL`, `HTTPS_PROXY`, and `NODE_EXTRA_CA_CERTS` are never set. Backed up in the install manifest so `undo` restores them.

## Codex CLI (`adapters/codex.py`)

- **Home / env:** `$CODEX_HOME` or `~/.codex`.
- **Settings:** `~/.codex/config.toml`, project `.codex/config.toml`, `~/.codex/hooks.json`.
- **Instructions:** `~/.codex/AGENTS.md`, project `AGENTS.md`.
- **Credentials (stat only):** `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`) and `$CODEX_HOME/.credentials.json` (MCP OAuth fallback).
- **Logs:** `$CODEX_HOME/{sessions,archived_sessions}/**/*.jsonl[.zst]` (rollout format). `.jsonl.zst` is skipped with a notice when stdlib cannot decompress it.
- **Headless:** API-key `--bench` creates `CODEX_HOME` first, then runs `codex exec --json …`. Subscription mode prints the same official argv for the user to run from the sandbox repo, without redirecting `CODEX_HOME`.
- **Install paths this adapter owns:**
  - `~/.codex/hooks.json` (PreToolUse `rtk` and the local read-gate / session-start scripts)
  - Caveman skill: `~/.agents/skills/caveman` (skill-only copy; never `caveman-setup`)
  - API-key proxy profile only: top-level `openai_base_url` in `~/.codex/config.toml` (quoted key). Headroom uses `http://127.0.0.1:8787/v1` (the `/v1` suffix is required; Kompressor stays pathless at `http://127.0.0.1:8817`). `chatgpt_base_url` is not set. Backed up in the manifest so `undo` restores it.

## Host / ecosystem (`adapters/host.py`, `host_logs.py`)

No single vendor home. This adapter covers third-party tools, ports, timers, and cross-agent checks, plus the tested Copilot usage parser. Cline and OpenCode log paths and parsers now live on those doctor-only adapters (`host_logs.py` still holds the parser functions).

- **Doctor-owned paths (never opened if they are credentials):** `~/.claude/.credentials.json`, `~/.codex/auth.json`, `~/.codex/.credentials.json`, `~/.caveman/credentials`, `~/.cache/anthropic`. Caveman CLI is inferred from `~/.config/caveman` or `~/.caveman` — the installer must not create those.
- **P06 `credential-file-exposure`:** stat only. On macOS, a present `~/.claude/.credentials.json` means the Keychain fallback wrote a plaintext file. On any OS, group- or world-readable credential files (mode shown as e.g. `0644`) are flagged. Linux existence of `~/.claude/.credentials.json` at `0600` is the normal store and is not flagged.
- **RTK:** `~/.config/rtk/config.toml` (`telemetry = false`; proxy never enabled), `~/.config/rtk/pipeline.toml`.
- **TokenEconomizer:** `$TOKENECONOMIZER_HOME/config.json`, default `~/.tokeneconomizer/config.json`. Clean values: `{"microskills":"off","toolprune":"propose","loopguard":"off"}`.
- **ccusage:** wrapper `~/.tokenstretch/bin/ccusage` (always `--offline`); pin dir `~/.tokenstretch/ccusage/` (package.json + lockfile + `npm ci`); `~/.claude/ccusage.json`.
- **Logs (host parsers):**
  - Copilot: `$COPILOT_HOME` or `~/.copilot/session-state/*/events.jsonl`, OTel file exporter / `~/.copilot/otel/*.jsonl`

Kit-owned (not vendor) paths used by install/bench: `~/.tokenstretch/manifests/`, `~/.tokenstretch/backups/`, `~/.tokenstretch/hooks/`.



## API-key proxy profile (`install --profile api-proxy --proxy headroom|kompressor`)

Never part of Clean. Selected only with the explicit flags above. Dry-run by default; `--apply` writes. Headroom `--apply` is allowed after a hash-checked install: a dedicated Python 3.13 venv at `~/.tokenstretch/tools/headroom` and `uv pip install --require-hashes -r` the packaged universal lock (`tokenstretch/pins/headroom/requirements.txt`, which includes the manylinux x86_64 wheel sha256 `3b5c7fd289e7d227dcd9b896615541ea9d3d37388bd97b3257fc8769c4b7ef70`). macOS below 14.0 is refused (onnxruntime only ships a macosx_14_0_arm64 wheel). The macOS-arm64 lock variant was compiled on Linux and has not been install-tested on a real Mac. Kompressor apply is allowed once the musl or gnu x86_64 release binary's sha256 is verified (tag `1.5.14` → `9e4721ab…`). Choose at most one proxy. Subscription/OAuth is refused (stat of credential files and env-var *names* only; files are never opened), including Kompressor in front of a Codex `auth.json` subscription login. Bind is `127.0.0.1` only; `0.0.0.0` is refused. Ports 8787 (Caveman/Headroom) and 8788 (Headroom MCP HTTP) are checked. Telemetry/analytics options quoted in the pins are written off. Agents are pointed at the proxy only through the adapter-owned settings above. After a successful apply, doctor flags **W10** (loopback base URL) and **P02** (info: a local compression proxy is in the request path). Host P01 flags Headroom unless the beacon and the update check are both explicitly off. Doctor also warns if Kompressor intercept is on, `websocket_relay` is on, or the listen address is not loopback. Host P02 still treats non-loopback third-party gateways as high.

Quoted start commands (manual; the installer does not launch the proxy): `headroom proxy --host 127.0.0.1 --port 8787 --no-telemetry` (avoid `headroom deploy` / `headroom install apply`; never set `HEADROOM_ALLOW_UNAUTHENTICATED_BIND`). Settings always include `HEADROOM_BEACON=off HEADROOM_TELEMETRY=off HEADROOM_UPDATE_CHECK=off DO_NOT_TRACK=1 HEADROOM_OFFLINE=1`. Optional container path: `ghcr.io/headroomlabs-ai/headroom:0.44.0@sha256:10ef5f71c4cae3a02ca28107dfcf379aa73314ff26544c1f9988140e71f732f4` (do not pin `:latest`). `kompressor serve --listen 127.0.0.1:8817` (never `kompressor wrap`, never Desktop intercept, never `/etc/kompressor/config.toml` which turns intercept ON, never the in-tree Dockerfile which binds `0.0.0.0:8817`). User config writes `[server] listen = "127.0.0.1:8817"` (`KOMPRESSOR__SERVER__LISTEN`) and `[proxy] websocket_relay = false`. Keep-warm is left unset (default off). A cargo fallback, if needed, is `cargo install --locked --git … --tag 1.5.14 --rev 9e4721abb1e11547a63205d60db870fde25f0c61 kompressor-cli`; the crate is not on crates.io. Headroom v0.44.0 source sets Claude `ANTHROPIC_BASE_URL=http://127.0.0.1:8787` (no path) and Codex/OpenAI `http://127.0.0.1:8787/v1`. Doctor W10/P02 still recognize the `/v1` form. Kompressor URLs stay pathless.

## Gemini CLI (`adapters/gemini_cli.py`) — doctor-only

- **Detect:** `gemini` on PATH, `~/.gemini/` (relocated by `GEMINI_CLI_HOME`), or project `.gemini/settings.json`.
- **Settings:** system-defaults < user < project < system. Paths: `GEMINI_CLI_SYSTEM_DEFAULTS_PATH` (else `<system-dir>/system-defaults.json`), `~/.gemini/settings.json`, project `.gemini/settings.json`, `GEMINI_CLI_SYSTEM_SETTINGS_PATH` (else `/etc/gemini-cli/settings.json`, macOS `/Library/Application Support/GeminiCli/settings.json`, Windows `%PROGRAMDATA%/gemini-cli/settings.json`). `$VAR` / `${VAR:-default}` in settings are expanded except secret-named vars (`API_KEY`, `TOKEN`, …), which are left literal and never logged.
- **Instructions:** `context.fileName` (default `GEMINI.md`): global `~/.gemini/<name>`, upward walk from cwd until `memoryBoundaryMarkers` (default `.git`) or home, downward scan capped by `discoveryMaxDirs` (default 200), and `@path` imports to depth 5 (counts toward W01 size).
- **Auth (stat / env names only):** `GEMINI_API_KEY` / `GOOGLE_API_KEY` / `GOOGLE_GENAI_USE_VERTEXAI` ⇒ api-key. Else presence of `~/.gemini/oauth_creds.json`, `gemini-credentials.json`, or `google_accounts.json` ⇒ subscription. Those files are never opened.
- **MCP:** `mcpServers` in the merged settings files.
- **Checks:** W01 (GEMINI.md size including @imports), W02 (AGENTS.md if listed), W05 (Pro-tier or `auto` model IDs; prices not hard-coded), W06 (`thinkingBudget` at the documented ceiling, `thinkingLevel` HIGH, or both set), W09 (OAuth/subscription without an API-key env name: token caching unavailable), P01 (`privacy.usageStatisticsEnabled` defaults to true; info), P02 (remote MCP), P03 (`~/.gemini/tmp/…/chats/` if present).
- **Not flagged:** T01 OAuth-reuse by a third-party tool (would need a quoted reader of these files, not just presence).

## GitHub Copilot CLI (`adapters/copilot_cli.py`) — doctor-only

- **Home / env:** `$COPILOT_HOME` or `~/.copilot`.
- **Settings:** `~/.copilot/settings.json`, plus project `.github/copilot/settings.json` and `settings.local.json` when present. BYOK if `COPILOT_PROVIDERS_CONFIG` is set.
- **Instructions:** `.github/copilot-instructions.md`, `.github/instructions/*.instructions.md`, project `AGENTS.md`, or root `CLAUDE.md` / `GEMINI.md`.
- **MCP:** `~/.copilot/mcp-config.json`.
- **Checks:** W01 / W02 (instruction size), W06 if `effort` or `reasoning-effort` is `max` (CLI flags are documented; the JSON key names are read only when present), P02 (`remoteExport` defaults to true — session history may sync to the account — and remote MCP), P03 (`logs/` or `session-state/` if present).
- **Not flagged (unverified):** settings.json key for `--disable-client-telemetry`; `--fleet` key. Usage logs remain on the host parser (ccusage-only events.jsonl).

## Cursor (`adapters/cursor.py`) — doctor-only

- **Detect:** `~/.cursor/cli-config.json` (relocated only by `CURSOR_CONFIG_DIR`; a generic XDG subpath is not fully quoted and is not honored), `hooks.json`, `mcp.json`, or project `.cursor/` (not the generic `agent` binary name alone).
- **Settings:** `~/.cursor/cli-config.json` (`model`, `maxMode`).
- **Instructions:** `.cursor/rules/*.mdc` only (plain `.md` is ignored and reported), plus `AGENTS.md`. User Rules are account/cloud settings (legacy cache in `state.vscdb` is undocumented and credential-adjacent).
- **MCP:** `.cursor/mcp.json` and `~/.cursor/mcp.json`.
- **Checks:** W01 (a `.mdc` over 500 lines; also an info finding when `.md` rules are ignored), W02 (AGENTS.md), W07 (`maxMode: true`, high), P02 (info: User Rules and Privacy Mode are account settings — check Cursor Settings; `state.vscdb` is never opened — plus remote MCP).
- **Not flagged:** no official local Privacy Mode key, so the doctor does not read one. No documented local token log, so no P03.

## Aider (`adapters/aider.py`) — doctor-only

- **Detect:** project `.aider.conf.yml`, `.aider.chat.history.md`, or `aider` on PATH. `CONVENTIONS.md` alone is not enough.
- **Settings:** candidates are cwd, git root, and `~/.aider.conf.yml`. Files load home → git root → cwd; later wins. `AIDER_*` env / shell rc override YAML. `~/.aider/oauth-keys.env` is a key file (stat only, never opened).
- **Instructions:** `CONVENTIONS.md` via the `read:` key (or that filename at the repo root).
- **MCP:** none documented; `list_mcp_servers` returns empty.
- **Checks:** W01 (CONVENTIONS.md, 200-line reasoning threshold), W06 (`reasoning-effort` max), W09 (`cache-prompts` explicitly off), T04 (`cache-keepalive-pings` > 0, info; the kit never enables it), P01 (analytics enabled; opt-in, so only when on), P03 (`.aider.chat.history.md` if present), P05 (`check-update` true or unset; default true — a PyPI GET on launch, no user content; info).

## OpenCode (`adapters/opencode.py`) — doctor-only

- **Detect:** `opencode` on PATH, project `opencode.json` / `opencode.jsonc`, `~/.config/opencode/AGENTS.md` or `plugins/`, or `opencode.db` / `log/opencode.log` under the data dir.
- **Settings:** `$OPENCODE_CONFIG_DIR` or `$XDG_CONFIG_HOME/opencode` or `~/.config/opencode`. Eligible names are `opencode.json` then `opencode.jsonc`; if both exist in the same directory, **`.jsonc` wins**. Project `opencode.json(c)` / `.opencode/` overrides the user file. `OPENCODE_CONFIG` is an explicit file between user and project. `config.json` is not a current user-config name. `"model": "provider/model"`, `compaction.auto` (defaults true), `warming` (defaults off; bool or object). JSONC comments and trailing commas are stripped without touching `//` inside strings.
- **Instructions:** project `AGENTS.md` and `~/.config/opencode/AGENTS.md`; fallbacks `CLAUDE.md` and `~/.claude/CLAUDE.md`; skills under `~/.claude/skills/*.md` are listed but not counted as always-loaded.
- **MCP:** the `mcp` key in those config files.
- **Logs:** `$XDG_DATA_HOME/opencode` or `~/.local/share/opencode` (same path on macOS/Windows). `OPENCODE_DB` override (absolute path; `:memory:` is ignored). `opencode.db` is opened `file:…?mode=ro`. Legacy `storage/message/**/*.json` is also parsed (ccusage-only writer).
- **Checks:** W01 (non-AGENTS instruction size; info when both AGENTS.md and CLAUDE.md are present — fallback double-loading), W02 (AGENTS.md size), T04 (`warming` on; med; never enabled by the kit), P02 (remote MCP), P03 (local session data).
- **Not flagged:** P05 for unpinned plugin startup update checks (on-disk pin format is not quoted). Official OpenCode does not document `OPENCODE_DATA_DIR` (that is a ccusage convention and is not honored here). Channel-specific `opencode-<channel>.db` names are not enumerated.

## Cline (`adapters/cline.py`) — doctor-only

- **Detect:** `cline` on PATH, `~/.cline/`, `.clinerules` / `.cline/rules/`, or the global Rules dirs (`~/.cline/rules`, `~/Cline/Rules`, `~/Documents/Cline/Rules`, Windows OneDrive variants). `.cursorrules` / `AGENTS.md` alone are not enough.
- **Settings:** `~/.cline/data/settings/global-settings.json`, relocated by `$CLINE_GLOBAL_SETTINGS_PATH`, `$CLINE_DATA_DIR`, or `$CLINE_DIR/data`. CLI flag names (`model`, `provider`, `thinking`, `compaction`) are read when present. Auto-approve uses **`toolAutoApprove`** only (unset means **true**, so W18 is info). `--auto-approve` on the CLI wins and does not have to rewrite the file.
- **Instructions:** `.clinerules/` (or a file of that name; `hooks/` skipped), `.cline/rules/`, global Rules dirs on every OS (`~/.cline/rules`, `~/Cline/Rules`, `~/Documents/Cline/Rules`, Windows OneDrive variants), `.cursorrules`, `.windsurfrules`, project `AGENTS.md`, `~/.agents/AGENTS.md`. Leftover `.roo/` and `.roorules` are listed and reported.
- **MCP:** `~/.cline/data/settings/cline_mcp_settings.json`.
- **Logs:** `$CLINE_SESSION_DATA_DIR` or `<data>/sessions/*/*.messages.json`; `<data>/tasks/*/ui_messages.json`; VS Code `globalStorage/saoudrizwan.claude-dev/tasks/*/ui_messages.json` under the quoted Code roots (Linux `~/.config/Code/…`, macOS `~/Library/Application Support/Code/…`, Windows `%APPDATA%\Code\…`). Those transcript files are read-only. `db/sessions.db` is stat'd only (no token columns; never opened). `metrics.inputTokens` includes cache; uncached input is `inputTokens - cacheReadTokens - cacheWriteTokens` (floored at 0). Legacy `ui_messages.json` `tokensIn` is still flagged unverified.
- **Checks:** W01 (rule-file size; info for leftover Roo files), W02 (AGENTS.md), W06 (`thinking` high/xhigh), W18 (`toolAutoApprove` true or unset; info), P02 (remote MCP), P03 (local session transcripts).
- **Not flagged:** T01 for Cline's `claude-code` / `openai-codex` provider IDs in `providers.json` (credential-adjacent; not opened). VS Code fork `globalStorage` directory names are not quoted in Cline's source and are not searched. Quoted secret stores (`providers.json`, `secrets.json`, `globalState.json`) are not opened.
