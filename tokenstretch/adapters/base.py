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
    """Return new text with the block appended, or None if a block is already there."""
    if RULES_START in existing:
        return None
    if not existing:
        return block
    sep = "\n" if existing.endswith("\n") else "\n\n"
    if existing.endswith("\n\n"):
        sep = ""
    return existing + sep + block


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

    def to_json(self) -> dict:
        return {
            "id": self.id, "adapter": self.adapter_id, "title": self.title, "why": self.why,
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
        """Stub until undo lands. Raises so callers cannot assume a restore happened."""
        raise NotImplementedError("undo is not implemented")

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

    def live_sessions(self, ctx: Context, limit: int = 5) -> list[dict]:
        """Newest local sessions, read-only: [{adapter, session, project, model, history_tokens, last_write, path}]."""
        return []

    # ---- watch / model picker ------------------------------------------------------------
    def watch_sessions(self, ctx: Context, limit: int = 5) -> list[dict] | None:
        """Per-turn records for `tokenstretch watch`, read-only. None => not supported yet.

        [{adapter, session, project, path, last_write, clear_hint, compact_hint,
          turns: [{ts, model, history_tokens, output_tokens, tool_calls (int|None), reads: [path]}]}]
        `reads` holds file paths from the tool's own read calls only; never file contents.
        """
        return None

    def model_tiers(self) -> dict[str, str]:
        """{"light": model, "mid": model, "top": model} for the model picker. Empty => no tier map yet."""
        return {}

    def is_top_model(self, model: str) -> bool:
        return False

    def model_switch_hint(self, model: str) -> str:
        """How a user switches model inside the tool, e.g. `/model sonnet`."""
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
