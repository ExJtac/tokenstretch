# Roadmap

## Done
- ~~`status --watch`~~: shipped as `tokenstretch watch`, with once-per-session warnings for long history, the top model on simple steps, repeated file reads, and fast growth.
- Smart model picker: `tokenstretch model suggest` (heuristic) and `tokenstretch model default` (backed up, undoable).
- Weekly savings report: `tokenstretch report --weekly`.
- Local dashboard: `tokenstretch dashboard` (loopback only, read-only, live).
- Instruction-file trimmer: `tokenstretch trim` (dry run by default, safe mechanical edits only, backed up, undoable).
- Codex: session labels from the rollout's working directory, and a light/mid/top tier map for `model suggest` / `model default`.

## Next
- One-key setup for more tools: offer the Codex mid-tier default model during `setup`.
- Verify Copilot, Cline, and OpenCode log paths and token fields against real installs (they're marked unverified today). Copilot: whether `events.jsonl` input includes cache, the OTel file line envelope, and a documented usage record in `-p --output-format json`.
- A Copilot CLI tier map for `model default`, once GitHub's model list and multipliers can be pinned from docs.
- Gemini CLI and Copilot CLI: use `install_steps` (RTK `--gemini` / `--copilot`) from setup's pinned-install step instead of asking you to run them.
- Exercise the live pinned installs on macOS and Linux outside the test suite.

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
