# Roadmap

## Next
- One-key setup for more tools: a mid-tier default model for Codex (once the model names are verified from docs).
- `status --watch`: refresh the session view every few seconds, still read-only.
- Verify Copilot, Cline, and OpenCode log paths and token fields against real installs (they're marked unverified today).
- Exercise the live pinned installs on macOS and Linux outside the test suite.

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
