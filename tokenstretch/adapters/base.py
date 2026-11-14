"""The adapter interface. Every AI coding tool is one module implementing `Adapter`.

The core (doctor, bench, installer) only talks to this interface. All tool-specific
paths, config formats, log formats, terms-of-service wording, and checks live in the
adapter. See ADAPTERS.md for the method list.

Privacy contract for every adapter (enforced by tests):
  * no network access in any method except none: adapters never fetch; the installer
    core runs the commands an adapter *returns*, and only on `install --apply`;
  * credential files may be stat()'d via `ctx.exists()` but never opened;
  * any value placed in Finding.evidence is masked by the core before output.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..context import Context
from ..findings import Finding, UsageRecord
from ..identity import CLI_NAME, PRODUCT_NAME


@dataclass
class DetectResult:
    """Return value of `detect`. Truthy when the tool is installed / present."""
    installed: bool
    version: str | None = None
    binary: str | None = None
    config_roots: list[str] = field(default_factory=list)
    auth_mode: str = "unknown"  # subscription | api-key | byok | local | unknown

    def __bool__(self) -> bool:
        return self.installed


@dataclass
class InstructionFile:
    path: str
    scope: str  # user | project | local | managed
    bytes: int
    lines: int
    always_loaded: bool
    imported_from: str | None = None


@dataclass
class McpServer:
    name: str
    scope: str
    transport: str  # stdio | http | sse
    enabled: bool
    file: str
    remote_url: str | None = None


@dataclass
class HookPlan:
    """Shared hook intents, mapped to each agent's event names by the adapter."""
    items: list[str] = field(default_factory=list)  # pre_tool_shell_filter, pre_read_gate, session_start_note



@dataclass
class ConfigLocation:
    path: Path
    kind: str            # "settings" | "mcp" | "memory" | "hooks" | "logs" | "credential" ...
    exists: bool
    sensitive: bool = False   # True => stat only, never read


@dataclass
class BaseUrl:
    url: str
    where: str           # file path or "env"/"rc"
    key: str


@dataclass
class InstallStep:
    description: str
    command: list[str] | None = None   # argv run on --apply; None => file edit / manual
    env: dict[str, str] = field(default_factory=dict)
    network: bool = False              # fetches from the internet when applied
    manual: bool = False               # needs a human (interactive prompt)
    edit: dict | None = None           # {"path": str, "merge": {...}} local JSON edit, backed up first


@dataclass
class HeadlessRun:
    argv: list[str]
    cwd: Path


class HeadlessRefused(PermissionError):
    """Raised when run_headless_task is called without an explicit --bench flag."""


RULES_START = f"<!-- {PRODUCT_NAME}:credit-rules start -->"
RULES_END = f"<!-- {PRODUCT_NAME}:credit-rules end -->"


def rules_block(mid_model: str, clear_hint: str) -> str:
    """Short, tool-neutral credit-saving rules for a project instruction file.

    The adapter supplies its mid-tier model name and how its users start fresh.
    """
    return (
        f"{RULES_START}\n"
        f"## Credit-saving rules (added by {PRODUCT_NAME}; `{CLI_NAME} undo` removes them)\n"
        f"- Start each task fresh. When a task is done, say so, so I can {clear_hint} before the next one.\n"
        "- Read narrowly: search first, then read only the lines you need. Skip whole large files, "
        "build output, logs, and snapshot folders.\n"
        "- Don't re-read a file you already read this task unless it changed.\n"
        f"- Run subagents on {mid_model} unless the job truly needs the top model, and give each one a narrow task.\n"
        "- Keep replies short: what changed, file paths, and blockers. No long recaps.\n"
        f"{RULES_END}\n"
    )


def add_rules_block(existing: str, block: str) -> str | None:
    """Return new text with the block appended, or None if a block is already there.

    A file that already has a standalone compact block keeps one block: the compact rules move
    inside the new rules block, in their short form, instead of sitting beside it.
    """
    if RULES_START in existing:
        return None
    if COMPACT_START in existing:
        bare = remove_compact_block(existing)
        if bare is not None and COMPACT_START not in bare:
            with_rules = add_rules_block(bare, block)
            return add_compact_block(with_rules) or with_rules
    if not existing:
        return block
    sep = "\n" if existing.endswith("\n") else "\n\n"
    if existing.endswith("\n\n"):
        sep = ""
    return existing + sep + block


COMPACT_START = f"<!-- {PRODUCT_NAME}:compact start -->"
COMPACT_END = f"<!-- {PRODUCT_NAME}:compact end -->"


def compact_block(merged: bool = False) -> str:
    """Compact mode: shorter, cleaner reports. Optional, off by default. No saving is claimed.

    `merged` is the form that sits inside the credit-rules block: no heading of its own, and no
    read-narrowly rule, because that block already has one. Paid on every step, so keep it short.
    """
    head = "" if merged else f"## Compact mode (added by {PRODUCT_NAME}; `{CLI_NAME} compact off` removes it)\n"
    read = "" if merged else "- Search first, then read only the line ranges you need.\n"
    return (
        f"{COMPACT_START}\n"
        f"{head}"
        "- Change files with targeted edits or diffs. Never reprint a whole file to change a few lines.\n"
        "- Point to code (`auth.py:40-60`). Quote only the lines under discussion.\n"
        "- Don't restate the question, the plan, or earlier messages. No intros, no recaps.\n"
        "- Fewer words, never fewer facts: keep the full conclusion, caveats, and next steps.\n"
        "- End each finished task with one line: what changed, where, what is next.\n"
        "- Hand work to another agent as: goal / done / files / next / open questions.\n"
        f"{read}"
        f"{COMPACT_END}\n"
    )


def _eol(text: str) -> str:
    """The line ending most lines of `text` use: CRLF or LF. LF for a tie or an empty text."""
    crlf = text.count("\r\n")
    return "\r\n" if crlf > text.count("\n") - crlf else "\n"


def _cut_eol(text: str) -> str:
    """`text` without one trailing line ending, of either kind."""
    return text[:-2] if text.endswith("\r\n") else text[:-1] if text.endswith("\n") else text


def add_compact_block(existing: str) -> str | None:
    """Return new text with compact mode on, or None if it is already on.

    A file that has the credit-rules block gets the short form inside that block, so there is one
    block and no rule twice. Any other file gets the full form appended. No existing character is
    changed: the block is written with the file's own line ending, and the lines already there
    keep theirs, also when the file mixes CRLF and LF.
    """
    if COMPACT_START in existing:
        return None
    nl = _eol(existing)
    start = existing.find(RULES_START)
    end = existing.find(RULES_END, start) if start >= 0 else -1
    if end > 0 and existing[end - 1] == "\n":
        return existing[:end] + compact_block(merged=True).replace("\n", nl) + existing[end:]
    block = compact_block().replace("\n", nl)
    if not existing:
        return block
    one = _cut_eol(existing)
    sep = nl + nl if one == existing else nl if _cut_eol(one) == one else ""
    return existing + sep + block


def remove_compact_block(existing: str) -> str | None:
    """Return the text with the compact block taken out, or None if there is none."""
    start = existing.find(COMPACT_START)
    if start < 0:
        return None
    end = existing.find(COMPACT_END, start)
    if end < 0:
        return None
    end += len(COMPACT_END)
    if existing[end:end + 2] == "\r\n":
        end += 2
    elif existing[end:end + 1] == "\n":
        end += 1
    head, tail = existing[:start], existing[end:]
    if not tail and _cut_eol(head) != head and _cut_eol(_cut_eol(head)) != _cut_eol(head):
        head = _cut_eol(head)     # the blank line that was put in front of an appended block
    return head + tail


@dataclass
class SetupItem:
    """One plain-English step in `tokenstretch setup`, planned by an adapter (never written by it).

    changes: [{path, kind, content, reason, key}] applied by the core with backups.
    """
    id: str
    adapter_id: str
    title: str          # what it is
    why: str            # why it wastes credits / what the risk is
    changes: list[dict] = field(default_factory=list)
    done: str = ""      # past-tense progress line, e.g. "switched default model to sonnet"
    undo: str = f"{CLI_NAME} undo"
    notes: list[str] = field(default_factory=list)  # e.g. how to use the top model for hard work
    optional: bool = False  # left out of the default plan; applied only when asked for by id

    def to_json(self) -> dict:
        return {
            "id": self.id, "adapter": self.adapter_id, "title": self.title, "why": self.why,
            "optional": self.optional,
            "changes": [{k: v for k, v in c.items() if k != "content"} for c in self.changes],
            "done": self.done, "undo": self.undo, "notes": list(self.notes),
        }


class Adapter:
    id: str = "base"
    display_name: str = "Base"
    tier: int = 3                       # 1 = full, 2 = doctor-only, 3 = instruction advice
    vendor_terms: str = ""              # human-readable name of the vendor terms that apply
    config_dir_env: tuple[str, ...] = ()  # env vars that relocate this tool's home (multi-account detection)
    headless_patterns: tuple[str, ...] = ()  # regexes matching scripted/headless invocations (warm-up timers)

    # ---- agents.md interface -----------------------------------------------------
    def detect(self, ctx: Context) -> DetectResult:
        raise NotImplementedError

    def read_config(self, ctx: Context) -> dict:
        """Return {effective: {key: {value, from}}, raw: [{file, scope, data}]}."""
        return {"effective": {}, "raw": []}

    def list_instruction_files(self, ctx: Context, cwd: str | None = None) -> list[InstructionFile]:
        return []

    def list_mcp_servers(self, ctx: Context) -> list[McpServer]:
        return []

    def read_usage_logs(self, ctx: Context, since_days: int = 30) -> list[UsageRecord]:
        return []

    def install_hooks(self, ctx: Context, plan: HookPlan | dict | None = None, *, dry_run: bool = True) -> dict:
        """Stub until the installer lands. Returns {changes, backup_dir} and writes nothing."""
        return {"changes": [], "backup_dir": ""}

    # last_log_notices is set per read_usage_logs call on the instance

    def headless_env(self, home: Path) -> dict[str, str]:
        return {"HOME": str(home)}


    def run_headless_task(self, ctx: Context, task: dict | None = None, opts: dict | None = None) -> dict:
        """Refuses unless opts['bench'] is true. Never runs against the real HOME/repo."""
        opts = opts or {}
        if not opts.get("bench"):
            raise HeadlessRefused("run_headless_task refused without --bench")
        from ..bench import default_headless_runner
        runner = opts.get("runner") or default_headless_runner()
        sandbox = Path(opts.get("sandboxDir") or opts.get("sandbox_dir") or ctx.root)
        home = Path(opts.get("home") or ctx.home)
        task = task or {}
        prompt = task.get("prompt") or ""
        model = task.get("model")
        spec = self.headless_run(prompt, sandbox, model)
        if spec is None:
            raise NotImplementedError(f"{self.id} has no headless runner")
        env = {"TERM": "dumb", **self.headless_env(home)}
        ran = runner.run(spec.argv, cwd=spec.cwd, env=env, timeout=int(opts.get("timeoutS") or 120))
        if ran.usage is not None:
            usage = list(ran.usage)
        else:
            rec = self.parse_headless_output(ran.stdout or "", task.get("id") or task.get("task_id") or "task")
            usage = [rec] if rec else []
        return {"exitCode": ran.exit_code, "usage": usage, "rawOutput": ran.stdout or "", "stderr": ran.stderr or ""}

    def undo(self, ctx: Context, manifest_path: str | None = None) -> dict:
        """Restore one install/setup run from its manifest (default: the newest one) via the core.

        Raises FileNotFoundError if there is no such manifest, so callers cannot assume a restore happened.
        """
        from ..installer import latest_manifest, undo_manifest
        path = Path(manifest_path) if manifest_path else latest_manifest(ctx)
        if path is None or not path.is_file():
            raise FileNotFoundError(f"no install manifest to undo: {path or 'none recorded'}")
        return undo_manifest(path)

    # ---- discovery (compat helpers used by doctor) --------------------------------
    def config_locations(self, ctx: Context) -> list[ConfigLocation]:
        return []

    # ---- facts the core's cross-tool checks need -------------------------------------
    def auth_mode(self, ctx: Context) -> str:
        """'subscription' | 'api' | 'unknown'."""
        return "unknown"

    def base_urls(self, ctx: Context) -> list[BaseUrl]:
        return []

    def hook_commands(self, ctx: Context) -> list[tuple[str, str]]:
        """[(command, where)] for every configured hook."""
        return []

    def effective_models(self, ctx: Context) -> list[tuple[str, str]]:
        """[(model, where)] configured as defaults."""
        return []

    def settings_env(self, ctx: Context) -> dict[str, tuple[str, str]]:
        """Env vars set inside the tool's own config: {NAME: (value, where)}."""
        return {}

    # ---- checks ------------------------------------------------------------------
    def checks(self, ctx: Context) -> list[Finding]:
        """Tool-specific waste / ToS checks."""
        return []

    def privacy_checks(self, ctx: Context) -> list[Finding]:
        """Tool-specific telemetry / cloud / retention checks."""
        return []

    # ---- usage logs (bench) -------------------------------------------------------------
    def default_log_paths(self, ctx: Context) -> list[Path]:
        return []

    def sniff_log(self, path: Path, first_lines: list[str]) -> bool:
        return False

    def parse_log(self, path: Path) -> list[UsageRecord]:
        return []

    # Incremental log reading (core `logcache.LogCache`). When True, `usage_log_parser(path)` returns
    # an object with `feed(lines: list[bytes])` and a `records` list (or None for a file that must be
    # parsed whole with `parse_log`), and `finish_usage_records` does any cross-file dedupe.
    incremental_logs: bool = False

    def usage_log_parser(self, path: Path):
        return None

    def finish_usage_records(self, records: list[UsageRecord]) -> list[UsageRecord]:
        return records

    def headless_run(self, prompt: str, cwd: Path, model: str | None = None) -> HeadlessRun | None:
        return None

    def apply_model_flag(self, argv: list[str], model: str) -> list[str]:
        """Return argv with this adapter's model flag added. Official commands stay otherwise unchanged."""
        return list(argv)

    def parse_headless_output(self, stdout: str, task_id: str) -> UsageRecord | None:
        return None

    # ---- installer -------------------------------------------------------------------
    def install_steps(self, tool_id: str, ctx: Context, options: dict) -> list[InstallStep] | None:
        """Steps to wire a catalog tool into this agent. None => tool not supported here."""
        return None

    def native_fix_steps(self, ctx: Context) -> list[InstallStep]:
        """Local-only config edits (e.g. converting ignore files to native deny rules)."""
        return []

    # ---- setup / status ----------------------------------------------------------------
    def setup_items(self, ctx: Context) -> list[SetupItem]:
        """Safe, local-only fixes for `tokenstretch setup`. Plans only; the core writes with backups."""
        return []

    # ---- compact mode (optional rules block) ---------------------------------------------
    COMPACT_CREATE = False   # may `compact on` start this tool's instruction file when there is none

    def compact_file(self, ctx: Context) -> Path | None:
        """The project instruction file this tool's compact block goes in. None: not supported."""
        return None

    def compact_also_loads(self, ctx: Context) -> list[Path]:
        """Other project files this tool loads every step. A compact block in one of them is enough."""
        return []

    def compact_item(self, ctx: Context, on: bool, planned: frozenset[Path] = frozenset()) -> SetupItem | None:
        """Plan turning compact mode on or off in this tool's file. Plans only; the core writes with backups.

        `planned` holds the files other tools' steps in the same run will add the block to.
        """
        path = self.compact_file(ctx)
        if path is None or ctx.root.resolve() == ctx.home.resolve():
            return None      # no project folder; never write an instruction file in the home folder
        there = path.is_file()
        # Read the bytes as they are and write them back the same way: text mode would turn CRLF
        # into LF, and a byte that is not UTF-8 must come back as it was, not as a replacement mark.
        existing = path.read_bytes().decode("utf-8", errors="surrogateescape") if there else ""
        if on:
            if not there and not self.COMPACT_CREATE:
                return None
            for other in self.compact_also_loads(ctx):
                if other in planned or (other.is_file() and COMPACT_START in other.read_text(encoding="utf-8", errors="replace")):
                    return None
            new = add_compact_block(existing)
        else:
            new = remove_compact_block(existing)
        if new is None:
            return None
        name = path.name
        return SetupItem(
            id=f"{self.id}-compact-block", adapter_id=self.id,
            title=f"Add the compact-mode rules to {name}" if on else f"Take the compact-mode rules out of {name}",
            why="Whole-file reprints, quoted code and recaps are paid for as output and again as history on "
                "every later step. These rules cut the words and keep the facts.",
            changes=[{
                "path": str(path), "kind": "modify" if there else "create",
                "content": new.encode("utf-8", errors="surrogateescape"), "key": "compact block",
                "reason": "add a marked compact-mode block" if on else "remove the marked compact-mode block",
            }],
            done=f"added compact mode to {name}" if on else f"took compact mode out of {name}",
        )

    def live_sessions(self, ctx: Context, limit: int = 5, max_age_s: float | None = None) -> list[dict]:
        """Newest local sessions (written within max_age_s if given), read-only: [{adapter, session, project, model, history_tokens, last_write, path}]."""
        return []

    def token_tools(self, ctx: Context) -> list[dict]:
        """Installed token-saving helpers and their telemetry, read-only:
        [{name, installed: True, telemetry: "off" | "on" | "not checked", note}]."""
        return []

    # ---- watch / model picker ------------------------------------------------------------
    def window_sessions(self, ctx: Context, limit: int = 500, max_age_s: float | None = None) -> list[dict] | None:
        """The sessions `pace` sums over a plan window: every step of each log, not only its end.
        A helper may carry `turns` (its own timed steps). Default: the same read as watch_sessions."""
        return self.watch_sessions(ctx, limit=limit, max_age_s=max_age_s)

    def watch_sessions(self, ctx: Context, limit: int = 5, max_age_s: float | None = None) -> list[dict] | None:
        """Per-turn records for `tokenstretch watch`, read-only. None => not supported yet.
        With max_age_s, files not written within that many seconds are skipped before opening.

        [{adapter, session, project, path, last_write, clear_hint, compact_hint, handoff_hint (optional), credits_hint (optional),
          start_tokens (optional: history size on the session's first step),
          auto_compact (optional: history size at which the tool summarizes by itself; watch then
           stays quiet about long history below it),
          helpers (optional: [{steps, sent, peak}] per subagent of this session, token counts only),
          turns: [{ts, model, history_tokens, output_tokens, tool_calls (int|None), reads: [path]}]}]
        `reads` holds file paths from the tool's own read calls only; never file contents.
        A turn may also carry `cache_read` and `cache_write` (tokens); watch's cache-expiry check
        only runs on turns that have both.
        """
        return None

    def watch_session_at(self, ctx: Context, path: Path) -> dict | None:
        """The one `watch_sessions` record for this session log, or None when the path is not one of
        this tool's own session logs. Used by `statusline`, which is handed a path by the tool.
        Must refuse anything outside the tool's log folder (never a credential file)."""
        return None

    def startup_parts(self, ctx: Context) -> list[dict] | None:
        """What every session loads before the first prompt, for `startup`: [{kind, name, tokens, fix}].

        `tokens` is an estimate (bytes / 4) or None when the size can't be read locally. Return None
        when the tool isn't supported, so `startup` skips it.
        """
        return None

    def cache_ttl_s(self, ctx: Context) -> int | None:
        """How long the tool's prompt cache lives without activity, in seconds. None => unknown,
        and watch then makes no idle prediction for this tool."""
        return None

    def model_tiers(self) -> dict[str, str]:
        """{"light": model, "mid": model, "top": model} for the model picker. Empty => no tier map yet."""
        return {}

    def model_tier_alternatives(self) -> dict[str, list[str]]:
        """Other good picks per tier, shown beside the main one (e.g. {"light": ["gpt-6.1-terra"]})."""
        return {}

    def model_tier_note(self) -> str:
        """How solid the tier map is, when it isn't vendor-published (shown with the picks)."""
        return ""

    def is_top_model(self, model: str) -> bool:
        return False

    def model_switch_hint(self, model: str) -> str:
        """How a user switches model inside the tool, e.g. `/model sonnet`."""
        return ""

    def effort_hint(self, effort: str) -> str:
        """Where a user sets reasoning effort in this tool. Empty when the tool has no such setting we know."""
        return ""

    def plan_default_model(self, ctx: Context, tier: str) -> SetupItem | None:
        """Plan setting the tool's default model to `tier`. Plans only; the core writes with backups."""
        return None

    # ---- helpers --------------------------------------------------------------------
    def finding(self, id: str, slug: str, severity: str, category: str, message: str, fix: str,
                source: str, **evidence) -> Finding:
        return Finding(id=id, slug=slug, severity=severity, category=category, message=message,
                       fix=fix, source=source, evidence=evidence, adapter_id=self.id)

    @staticmethod
    def iter_existing(paths: Iterable[Path]) -> list[Path]:
        return [p for p in paths if p.is_file()]

    def _auth_mode_public(self, ctx: Context) -> str:
        """Map the internal auth_mode() value to the agents.md vocabulary."""
        mode = self.auth_mode(ctx)
        return "api-key" if mode == "api" else mode
