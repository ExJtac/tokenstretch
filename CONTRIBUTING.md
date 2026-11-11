# Contributing to tokenstretch

Thanks for helping people waste fewer AI coding credits. This project is small, local-only, and strict about privacy. Please read the hard rules before you open a PR.

## Hard rules (every PR must follow these)

1. **No telemetry or analytics, ever.** No crash reporting, no update checks, no "anonymous usage stats".
2. **No network calls.** The package must not import or use network libraries (`socket`, `urllib.request`, `http.client`, `requests`, and so on). The only network traffic allowed is a **pinned install the user explicitly confirms**, described in `tokenstretch/catalog.py` with pinned commits and hashes. Never use `@latest` or unpinned downloads.
3. **Never open credential files.** Login and token files (`~/.claude/.credentials.json`, `~/.codex/auth.json`, `~/.caveman/credentials`, Gemini OAuth files, and so on) may only be `stat`'d with `ctx.exists()` / `ctx.file_stat()`. Never read, copy, symlink, log, or proxy them. Tests plant canary credentials and fail if one is opened.
4. **Back up every file change.** Adapters only *plan* changes (`setup_items`, `install_hooks`, `planned_support_files`). The core writes them through `installer.apply_changes`, which backs up every file and writes a manifest so `tokenstretch undo` can restore it. Adapters never write files themselves.
5. **Dry run by default.** Nothing changes without `--apply`, `--yes`, or an interactive "y".
6. **Tool-specific paths live in adapters.** Core modules (`cli.py`, `setup.py`, `installer.py`, `bench.py`) must not hard-code a tool's paths or config keys.
7. **Honest numbers.** Savings claims come only from real `bench` runs. If there's no data, print `no measurement`. Never invent prices, hashes, or results.
8. **Stdlib only at runtime.** Python 3.11+. pytest is the only dev dependency.

## Dev setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e . pytest
.venv/bin/python -m tokenstretch --version
```

## Running tests

```bash
.venv/bin/python -m pytest -q
# The same suite with networking disabled (Linux; needs unprivileged user namespaces):
unshare -rn .venv/bin/python -m pytest -q -m 'not p00_outer'
```

Both runs must pass. CI (`.github/workflows/test.yml`) runs both on every push and pull request. Tests must never run a real install or a real agent: use `FakeRunner` / `FakeHeadlessRunner` (the live runners refuse to run under pytest).

## Adding an adapter (a new AI coding tool)

1. Read `ADAPTERS.md` (the adapter contract and current adapters and their paths).
2. Create `tokenstretch/adapters/<tool>.py` with a class that subclasses `Adapter` (or `DoctorOnlyAdapter` for a doctor-only tool). At minimum, implement `detect`, `checks`, and `privacy_checks`. Optionally add `effective_models`, `live_sessions`, `setup_items`, log parsing for `bench`, and hook planners.
3. Register it in `tokenstretch/adapters/__init__.py`.
4. Add a fixture under `tests/fixtures/<tool>/` (fake configs only, no real secrets) and tests for each check ID it emits.
5. Cite your source for every path and key (official docs or source at a commit). Mark anything you couldn't verify as **unverified** in code comments and `ADAPTERS.md`.

## Adding a doctor check

Pick the next free ID in the right family (W = waste, T = terms-of-service risk, P = privacy), add it to the adapter's `implemented_checks`, give it a plain-English `message` and `fix`, and add a fixture-backed test. Keep severity honest: `info` unless it really costs money or risks an account.

## Pull requests

- Keep PRs small and focused, with no drive-by rewrites.
- Fill in the PR template's privacy checklist.
- Update `CHANGELOG.md` under "Unreleased".

By contributing, you agree that your contributions are licensed under the MIT License and that you'll follow the [Code of Conduct](CODE_OF_CONDUCT.md).
