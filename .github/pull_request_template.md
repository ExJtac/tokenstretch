## What this changes

<!-- One or two sentences. Link the issue if there is one. -->

## How I tested it

- [ ] `.venv/bin/python -m pytest -q` passes
- [ ] `unshare -rn .venv/bin/python -m pytest -q -m 'not p00_outer'` passes (network blocked)

## Privacy checklist (required)

- [ ] No telemetry, analytics, crash reporting, or update checks were added
- [ ] No new network calls (the only exception is a pinned install the user confirms, with commit and hash)
- [ ] No credential file is opened, copied, symlinked, or logged (stat only)
- [ ] Every file change goes through the installer, so it's backed up and `undo` restores it
- [ ] Nothing changes without `--apply`, `--yes`, or an interactive "y"
- [ ] Tool-specific paths and keys live in the adapter, not in core
- [ ] Any savings claim comes from a real `bench` run
- [ ] `CHANGELOG.md` updated under "Unreleased"
