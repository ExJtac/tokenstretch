# Running tokenstretch on your machine

This is a short, local-only walkthrough for someone who already uses Claude Code and Codex. Nothing here publishes or pushes the project.

**Subscription mode is the default for you.** tokenstretch never copies, symlinks, or opens credential files. Codex replaces `auth.json` with a `rename()`, which would break a symlink, and Anthropic’s terms say developers may not collect, store, or intermediate Claude.ai credentials. The kit therefore does not touch your login. You run the official `claude` and `codex` binaries yourself, unmodified, already signed in, as ordinary use. The kit only prepares sandbox *repo* copies and later reads the resulting session logs.

There is a second, automated path — API-key mode (`bench --bench`) — if you have already exported `ANTHROPIC_API_KEY` / `CODEX_API_KEY` (Gemini CLI: `GEMINI_API_KEY`; Copilot CLI: `COPILOT_GITHUB_TOKEN`). That path uses `claude --bare` (which does not use a subscription login) and a throwaway `CLAUDE_CONFIG_DIR` / `CODEX_HOME`. Use it for scripted runs; it does not measure a subscription session.

## Prerequisites

- Python 3.11 or newer
- The `claude` and `codex` CLIs installed and already signed in (the same ones you use day to day)
- A local clone of this repository

## Install

```bash
git clone https://github.com/ExJtac/tokenstretch.git
cd tokenstretch
python3 -m venv .venv
# macOS/Linux:
source .venv/bin/activate
# Windows: .venv\Scripts\activate
pip install -e .
tokenstretch --version    # should print: tokenstretch 0.9.7
```

Run the rest of the commands from inside the clone with the venv active. Defaults are: `--home` is your real home directory, `--root` is the current directory.

## Commands, in order (subscription mode)

These are read-only except for `--prepare`, which writes only under `~/.tokenstretch/benches/` (or `--prepare-dir`). Do **not** add `--apply` to `install`. Planned user-level files are never written to your real HOME.

```bash
tokenstretch doctor
tokenstretch doctor --privacy
tokenstretch bench --from-logs
tokenstretch install
```

`install` without `--apply` is a dry-run. It prints the plan and writes nothing.

Then prepare the before/after sandbox repo copies and print the official CLI commands:

```bash
tokenstretch bench --prepare
```

`--prepare` copies this folder twice (it skips `.git`, `.venv`, `__pycache__`, `node_modules`, and `*.pyc`): `…/before/repo` (no profile) and `…/after/repo` (project-level planned files only, inside that copy). It does not redirect `CLAUDE_CONFIG_DIR` or `CODEX_HOME`, does not set API keys, and does not touch credential files. It prints commands like:

```bash
cd /path/to/…/before/repo
claude -p "…" --output-format json
codex exec --json "…"

cd /path/to/…/after/repo
claude -p "…" --output-format json
codex exec --json "…"
```

Run those commands yourself, already signed in, as ordinary use. Do not add `--bare`, and do not wrap them in extra env vars to point at a sandbox login. The printed argv is the official CLI, unmodified.

When those runs have finished:

```bash
tokenstretch bench --measure
```

`--measure` (optionally with the prepare-dir path; default is the latest under `~/.tokenstretch/benches/`) reads your normal local session logs read-only, keeps records in the prepare time window whose path/cwd matches those sandbox dirs, and compares before vs after. If a side has no in-scope records it prints `no measurement`.

To also keep a machine-readable doctor report:

```bash
tokenstretch doctor --json
```

If you ever did run `install --apply` against your real home (this walkthrough does not), undo it with:

```bash
tokenstretch undo
```

That restores from the latest manifest under `~/.tokenstretch/manifests/`. You do not need this after `--prepare` / `--measure` alone.

## API-key mode (automated `--bench`)

Use this only if you have already exported an API key in your shell. tokenstretch never reads, logs, or stores the key values; it only checks whether the *name* is present and lets the official CLI inherit the existing process environment.

```bash
# already in your shell, if you use this mode — do not paste keys into tokenstretch
# export ANTHROPIC_API_KEY=…
# export CODEX_API_KEY=…
tokenstretch bench --bench --adapter claude-code --json
tokenstretch bench --bench --adapter codex --json
```

For Claude, the kit sets `CLAUDE_CONFIG_DIR` to an empty sandbox dir and runs `claude --bare -p … --output-format json` (`--bare` is API-key-only and does not use your subscription login). For Codex, it creates `CODEX_HOME` first, then runs `codex exec --json …`. The real repo and real HOME are not written to. The temp copy is deleted when the command finishes (unless you keep it for debugging).

If a key name is missing, that adapter is skipped and the output says so, with `no measurement`. Key values must never appear in the printed report, the JSON, or any manifest.

To keep a run small:

```bash
tokenstretch bench --bench --adapter claude-code --prompt "Reply with the single word: ok." --json
tokenstretch bench --bench --adapter codex --prompt "Reply with the single word: ok." --json
```

Default timeout is 120 seconds per run.

## What is read (and what is never opened)

**`doctor` and `doctor --privacy`** read local config and instruction files, for example:

- Claude Code: `~/.claude/settings.json`, `~/.claude.json`, project `.claude/settings.json` and `settings.local.json`, `CLAUDE.md`, `MEMORY.md`, `.claude/skills`, `.claude/commands`, `.claude/plugins`, project `.mcp.json`
- Codex: `~/.codex/config.toml`, `~/.codex/hooks.json`, `AGENTS.md`
- Host / other tools, if present: `~/.config/rtk/`, `~/.tokeneconomizer/config.json`, Caveman config dirs, Gemini / Copilot / Cursor / Aider settings, and similar
- Shell startup files, for environment variables only: `~/.zshrc`, `~/.bashrc`, `~/.bash_profile`, `~/.profile`, `~/.zprofile`, `~/.config/fish/config.fish`
- Local process, port, crontab, and user-timer lists (`ps`, `ss`, `crontab`, `systemctl --user`)

Credential files are **only checked with stat** (presence, mode, and permissions). They are never opened, copied, or symlinked:

- `~/.claude/.credentials.json`
- `~/.codex/auth.json` and `~/.codex/.credentials.json`
- `~/.caveman/credentials`
- `~/.cache/anthropic` (the path itself; contents are not opened)
- `~/.gemini/oauth_creds.json`, `gemini-credentials.json`, `google_accounts.json`, `mcp-oauth-tokens.json`, `a2a-oauth-tokens.json`, `.env`
- `~/.copilot/config.json` (can hold plaintext tokens) and `~/.copilot/providers.json`
- `~/.aider/oauth-keys.env`

Cursor `state.vscdb` is never opened (User Rules and Privacy Mode are account/cloud settings).

**`bench --from-logs` and `bench --measure`** read local usage logs only: `~/.claude/projects/**/*.jsonl`, `~/.codex/{sessions,archived_sessions}/**/*.jsonl`, `~/.gemini/tmp/*/chats/**`, and Copilot / Cline / OpenCode logs if those tools are present. `--measure` keeps only records in the prepare window that are scoped to the before/after sandbox dirs. Neither command opens credential files.

**`install` (dry-run)** reads the same kind of config in order to print a plan. It writes nothing unless you pass `--apply`.

**`bench --prepare`** writes the two repo copies and a `manifest.json` under the prepare dir. It does not write to your real HOME except for that benches folder (default `~/.tokenstretch/benches/`).

**`bench --bench`** copies this folder into a temp directory, isolates Claude/Codex via empty `CLAUDE_CONFIG_DIR` / created `CODEX_HOME`, applies planned files only inside that copy, and deletes the temp directory.

## Proof that tokenstretch does not talk to the network

tokenstretch itself makes no network calls and does not import network libraries. `doctor`, `doctor --privacy`, `bench --from-logs`, `bench --prepare`, `bench --measure`, and `install` (dry-run) stay on your machine.

The official `claude` and `codex` CLIs **do** talk to Anthropic and OpenAI when you run the printed subscription commands, or when API-key `--bench` runs them. That traffic is theirs, not tokenstretch adding its own.

Optional checks:

- On Linux, rerun the local-only commands with no network and confirm they still work:

  ```bash
  unshare -rn tokenstretch doctor
  unshare -rn tokenstretch doctor --privacy
  unshare -rn tokenstretch bench --from-logs
  ```

  (`unshare` is not usually available on macOS. Do not wrap the official `claude` / `codex` commands, or `--bench`, in `unshare`; the CLIs need the network to do the task.)

- While a CLI run is in progress, you can watch connections (`ss -tp` on Linux, or `lsof -iTCP -sTCP:ESTABLISHED` / Activity Monitor on macOS). You should see `claude` or `codex` talking to their vendors, and no separate tokenstretch connection.

## Credit cost (estimate only)

The code does not define a price, so this is **not a dollar figure** and is not a measurement.

The default task prompt is 13 words (75 characters): “List the files in this repository and summarize the README in one sentence.” This folder’s README is short. The copied tree is on the order of a few hundred files (`.venv` is not copied). Subscription mode does four official CLI runs if you run both Claude and Codex before and after. API-key `--bench` also does two runs per adapter. Each run may also pull extra context the CLI chooses to read; that part is not capped by tokenstretch.

To keep it small, pass a tiny `--prompt` and run one adapter at a time (see the examples above).

Default timeout for API-key `--bench` is 120 seconds per run. There is no kit-enforced timeout on the commands you run yourself in subscription mode.

## If you share a report

If you file an issue or share a report, useful attachments are:

1. `tokenstretch doctor --json`
2. The printed output (or `--json`) from `tokenstretch bench --prepare` and `tokenstretch bench --measure`
3. Optional: `tokenstretch bench --from-logs --json`
4. Optional, only if you already had API keys exported and used that mode: the JSON from `tokenstretch bench --bench --adapter claude-code --json` and from `--adapter codex`

Doctor already redacts some key- and token-shaped strings in finding evidence. Review the files before you share them anyway. Redact any leftover API keys, tokens, passwords, cookie or `Authorization` values, and anything else that looks private. Key values should never have appeared in tokenstretch output; if one did, redact it and say so. You may also want to replace your home-directory username in file paths. Do not send the contents of credential files (tokenstretch does not include them).
