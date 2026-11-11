# tokenstretch: technical details

The full implementation notes that used to be the README. Start with the top-level README.md.

An open-source, local toolkit that helps people get more out of AI coding credits. It audits waste and terms-of-service risk (`doctor`), measures before/after usage (`bench`), and can apply a small, terms-safe set of local tools (`install`) with an `undo` path.

**Status (v0.1.0, v0.2 in progress):** `doctor`, Clean-profile `install` / `undo`, the opt-in API-key proxy profile (`install --profile api-proxy --proxy headroom|kompressor`; Headroom `--apply` uses a packaged hashed lock (`uv pip install --require-hashes` into `~/.tokenstretch/tools/headroom`); Kompressor apply verifies the release-binary sha256), and `bench` are implemented. Doctor covers W01–W18, T01–T07, and P00–P06. Doctor-only adapters cover Gemini CLI, Copilot CLI, Cursor, Aider, OpenCode, and Cline. `LIVE-BENCH.md` is the walkthrough: subscription mode (`bench --prepare` then you run the official CLI, then `bench --measure`) is the default, because the kit never copies, symlinks, or opens credential files. Automated `bench --bench` is API-key mode (`claude --bare` with `CLAUDE_CONFIG_DIR`, and `CODEX_HOME` created first) and only passes through `ANTHROPIC_API_KEY` / `CODEX_API_KEY` if you already exported them; values are never read, logged, or stored. Install pins and hashes are packaged in `tokenstretch/catalog.py` (none invented). `install` is dry-run unless you pass `--apply`. Savings numbers come only from measured runs; if there is no data the command prints `no measurement`. Live installs and live headless binaries have not been exercised in this tree (tests use fake runners).

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
```

`doctor` is read-only. Exit codes: 0 = clean (or info only), 1 = waste findings, 2 = a ToS or privacy high.

## Privacy guarantees

These are the product guarantees. What is actually enforced in this tree today is listed after the block.

> **Privacy guarantees**
> - **Zero network calls from the kit itself.** `doctor` and `bench` read local files only. The kit has no telemetry, analytics, crash reporting, update checks, or "phone home." The only network traffic is the install step you run yourself, which downloads pinned versions (exact tags, with checksums where upstream publishes them).
> - **Your prompts, code, and logs never leave your machine because of this kit.** Your agent still talks to your own provider (Anthropic or OpenAI), exactly as it did before.
> - **The default profile ships only tools that run fully local with telemetry off:** RTK (OSS edition; telemetry off by default), the Caveman skill (no CLI, no cloud), Microsoft TokenEconomizer, and ccusage in `--offline` mode.
> - **No credential handling.** The kit never reads, copies, or proxies your Claude/ChatGPT login, API keys, or tokens. There is no proxy on your subscription login and no MITM certificates.
> - **Opt-in only, and loud about it.** Any optional tool that has telemetry or cloud features is off unless you choose it. When you do, the installer turns its telemetry off first and tells you exactly what it would send.
> - **Checkable.** Run `doctor --privacy` to list every setting, env var, and process on your machine that sends data off-box. Run the kit under a firewall or `unshare -rn` and it still works.

**Enforced today:** `doctor` and `bench --from-logs` are local-only and the package imports no network libraries. Tests include an in-process socket guard and a suite run under `unshare -rn` (skipped if that namespace is unavailable). Credential files are stat'd, never opened. `install` is dry-run unless you pass `--apply`. License checks use packaged snapshots (nothing is fetched). Pins: RTK OSS v1.2.0 commit `e288a18c…` (rtk-oss-* assets only, install.sh and checksums.txt sha256 pinned; linux-gnu x64 and Windows arm64 are reported as unsupported oss platforms); Caveman skill-only at tag v4.1.0 / `1e91f1306d…` (copy `skills/caveman` only); TokenEconomizer v0.1.0 / `ec8ff5ec…` (commit-pinned, no assets) with Clean config `microskills=off`, `toolprune=propose`, `loopguard=off`; ccusage@20.0.48 from a pinned lockfile (`npm ci --ignore-scripts --no-audit --no-fund`, `npm_config_update_notifier=false`) plus `ccusage.json` `defaults.offline=true` and a wrapper that always passes `--offline`. Tests never run real install commands or live headless agents. Live `--bench` uses a throwaway copy and never writes the real repo or HOME. The opt-in API-key proxy profile uses packaged pins: Headroom v0.44.0 commit `b21479e0…` with wheel/sdist sha256 and a packaged universal `--require-hashes` lock (`tokenstretch/pins/headroom/requirements.txt`, includes manylinux x86_64 wheel `3b5c7fd2…`); install is `uv venv --python 3.13` then `uv pip install --require-hashes -r <lock>` into `~/.tokenstretch/tools/headroom` (`UV_NO_PROGRESS=1`). macOS below 14.0 is refused (onnxruntime only ships a macosx_14_0_arm64 wheel); the macOS lock was compiled on Linux and has not been install-tested on a real Mac. Claude is pointed at `http://127.0.0.1:8787` (no path); Codex/OpenAI at `http://127.0.0.1:8787/v1`; Kompressor 1.5.14 commit `9e4721ab…` installs the musl or gnu x86_64 release binary and verifies its sha256. Beacon env is always set off (`HEADROOM_BEACON`, `HEADROOM_TELEMETRY`, `HEADROOM_UPDATE_CHECK`, `DO_NOT_TRACK`, `HEADROOM_OFFLINE`) and the start command passes `--no-telemetry`. **Not done yet:** doctor-only adapters for Goose and Devin. See `ADAPTERS.md` for the interface and paths, and `ROADMAP.md` for unverified items.

## Tests

```bash
python -m pytest -q
```

The suite is also run under `unshare -rn` (the `p00_outer` test, or `pytest -m 'not p00_outer'` inside that namespace). Fixtures under `tests/fixtures/` are fake configs and logs. They contain no real secrets. Install and headless tests use fake runners only; they make no network calls.

## Verified vs unverified

- Claude Code settings, CLAUDE.md locations, and transcript dir `~/.claude/projects/**/*.jsonl` follow official docs. Token field names used in the parser are the usual ccusage-style names and have not been confirmed against a live session file in this repo.
- Codex rollout paths and token fields (`$CODEX_HOME/{sessions,archived_sessions}/**/rollout-*.jsonl`, `event_msg`/`token_count`, `token_usage_record`) are **verified-in-source**. Dedupe of repeated totals and forked-session replays follows ccusage (ccusage-only).
- Cline `metrics.inputTokens` already includes cache; uncached input is `inputTokens - cacheReadTokens - cacheWriteTokens` (floored at 0). Legacy Cline `ui_messages.json` `tokensIn` and Copilot input are still flagged **unverified semantics**. OpenCode input excludes cache (verified-in-source).
- The product name `tokenstretch` was available on GitHub, npm, and X at the time of the name check (2026-11-10). No trademark search has been run.

## License

MIT for this repository. Third-party tools in the Clean profile, and Headroom / Kompressor in the opt-in API-key profile, are checked against packaged license snapshots (Apache-2.0 or MIT only) before any apply step. Artifact hashes and commits are packaged in `tokenstretch/catalog.py` and `tokenstretch/license_snapshots/`; this tree does not invent checksums. Headroom and Kompressor have license snapshots (Apache-2.0 / MIT) and the artifact hashes quoted in the catalog.
