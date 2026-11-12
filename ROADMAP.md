# Roadmap

## Done
- ~~`status --watch`~~: shipped as `tokenstretch watch`, with once-per-session warnings for long history, the top model on simple steps, repeated file reads, and fast growth.
- Smart model picker: `tokenstretch model suggest` (heuristic) and `tokenstretch model default` (backed up, undoable).
- Weekly savings report: `tokenstretch report --weekly`.
- Local dashboard: `tokenstretch dashboard` (loopback only, read-only, live).
- Instruction-file trimmer: `tokenstretch trim` (dry run by default, safe mechanical edits only, backed up, undoable).
- Status-bar line: `tokenstretch statusline` (history size and the most urgent watch warning).
- `watch`: cache-expiry warning for Claude Code (logged, and predicted for idle sessions).
- Codex: session labels from the rollout's working directory, and a light/mid/top tier map for `model suggest` / `model default`.

## Next
- One-key setup for more tools: offer the Codex mid-tier default model during `setup`.
- Verify Cline and OpenCode log paths and token fields against real installs. Copilot: the OTel file line envelope, and whether every older `events.jsonl` follows the cache-inclusive convention.
- Codex: confirm whether `reasoning_output_tokens` is inside `output_tokens` (the research sample's `total_tokens` suggests it is); if so, split it like Copilot so weekly totals don't count reasoning twice.
- Gemini CLI and Copilot CLI: use `install_steps` (RTK `--gemini` / `--copilot`) from setup's pinned-install step instead of asking you to run them.
- Exercise the live pinned installs on macOS and Linux outside the test suite.

- Cache-expiry warning and `statusline` for Codex, Gemini CLI, and Copilot CLI, once each tool's cache counts and status-bar input are verified.
- `setup`: offer to add the `statusLine` entry (backed up, undoable) when none is set.

- A dashboard "copy command" button next to each fix (still read-only).

- `trim`: offer moving blocks out of user-level files once each tool's import syntax is verified, and flag headings left empty after duplicates are removed.

## Good first issues
- **Goose adapter** (doctor-only): detect the config, check the default model and telemetry. See `ADAPTERS.md` for the interface.
- **Devin / Windsurf adapter** (doctor-only), starting with instruction-file size and duplicate rules.
- **More doctor checks:** warn on TokenEconomizer `loopguard != off` or `toolprune == auto`.
- **Codex `live_sessions`**, so `status` shows Codex sessions too (rollout files under `$CODEX_HOME/sessions`).
- **A core-has-no-tool-strings test**, so core modules can't hard-code tool paths.
- **A `report --open` flag** that prints the `file://` URL to click (no browser automation).

## Not planned
- Anything that sends data off your machine (telemetry, cloud sync, hosted dashboards).
- Proxies on subscription logins, or anything that reads credential files.
