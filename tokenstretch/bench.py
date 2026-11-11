"""Read-only `bench --from-logs` and opt-in live `--bench` in a throwaway sandbox.

Savings numbers come only from measured runs. There are no estimates, no default
prices, and no invented token counts. Missing data is printed as "no measurement".
Live runs never write to the real repo or the real HOME.
"""
from __future__ import annotations

import fnmatch
import json
import os
import shlex
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .files import git_ls_files, is_git_work_tree

from .adapters import REGISTRY
from .adapters.base import HeadlessRefused
from .context import Context, build_context
from .findings import UsageRecord
from .identity import CLI_NAME, PRODUCT_NAME, state_dir
from .installer import apply_changes, plan_file_changes
from .usage import NO_MEASUREMENT, format_count, record_in_window

COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".venv", "__pycache__", "node_modules", ".pytest_cache", "*.pyc",
)


@dataclass
class AdapterUsage:
    adapter_id: str
    records: int = 0
    sessions: int = 0
    input_uncached: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    output: int | None = None
    reasoning: int | None = None
    cost_usd: float | None = None
    unverified_semantics: bool = False
    no_measurement: bool = False
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class FromLogsReport:
    window: str
    adapters: list[AdapterUsage] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    records: list[UsageRecord] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "window": self.window,
            "adapters": [a.to_json() for a in self.adapters],
            "notices": self.notices,
            "records": [r.to_json() for r in self.records],
        }


@dataclass
class HeadlessResult:
    argv: list[str]
    cwd: str
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    skipped: bool = False
    usage: list[UsageRecord] | None = None


class FakeHeadlessRunner:
    """Returns scripted results. Never starts a process. Used by tests."""

    def __init__(self, scripted: list[HeadlessResult] | None = None) -> None:
        self.scripted = list(scripted or [])
        self.calls: list[dict] = []

    def run(self, argv: list[str], *, cwd: Path, env: dict | None = None, timeout: int = 120) -> HeadlessResult:
        self.calls.append({"argv": list(argv), "cwd": str(cwd), "env": dict(env or {}), "timeout": timeout})
        if self.scripted:
            return self.scripted.pop(0)
        return HeadlessResult(list(argv), str(cwd), 0, "", "fake-headless: no scripted result", skipped=True, usage=[])


class LiveHeadlessRunner:
    """Executes the agent binary. Refuses to run inside the test suite."""

    def run(self, argv: list[str], *, cwd: Path, env: dict | None = None, timeout: int = 120) -> HeadlessResult:
        if "pytest" in sys.modules:
            raise HeadlessRefused("refusing to run a live headless bench under pytest")
        import os
        import subprocess
        merged = dict(os.environ)
        merged.update(env or {})
        try:
            proc = subprocess.run(
                argv, cwd=str(cwd), capture_output=True, text=True, env=merged, timeout=timeout,
            )
            return HeadlessResult(list(argv), str(cwd), proc.returncode, proc.stdout, proc.stderr, False)
        except (OSError, subprocess.SubprocessError) as exc:
            return HeadlessResult(list(argv), str(cwd), 1, "", str(exc), False)


def default_headless_runner():
    if "pytest" in sys.modules:
        return FakeHeadlessRunner()
    return LiveHeadlessRunner()


def _cutoff(since_days: int | None, since: str | None) -> tuple[float | None, str]:
    now = datetime.now(timezone.utc)
    if since:
        try:
            text = since[:-1] + "+00:00" if since.endswith("Z") else since
            start = datetime.fromisoformat(text)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            return start.timestamp(), f"since {since}"
        except ValueError:
            pass
    days = 30 if since_days is None else int(since_days)
    start = now.timestamp() - days * 86400
    return start, f"last {days} days"


def _summarize(adapter_id: str, recs: list[UsageRecord], notices: list[str]) -> AdapterUsage:
    if not recs:
        return AdapterUsage(
            adapter_id=adapter_id,
            no_measurement=True,
            notes=list(notices),
        )
    sessions = {r.session_id for r in recs if r.session_id}
    costs = [r.cost_usd for r in recs]
    cost = sum(c for c in costs if c is not None) if costs and all(c is not None for c in costs) else None
    return AdapterUsage(
        adapter_id=adapter_id,
        records=len(recs),
        sessions=len(sessions),
        input_uncached=sum(r.input_tokens for r in recs),
        cache_read=sum(r.cache_read_tokens for r in recs),
        cache_write=sum(r.cache_write_tokens for r in recs),
        output=sum(r.output_tokens for r in recs),
        reasoning=sum(r.reasoning_tokens for r in recs),
        cost_usd=cost,
        unverified_semantics=any(r.unverified_semantics for r in recs),
        no_measurement=False,
        notes=list(notices) + [n for r in recs for n in (r.notes or [])],
    )


def from_logs(
    ctx: Context,
    *,
    since_days: int | None = 30,
    since: str | None = None,
    until: str | None = None,
    collect_notices: Callable[[], list[str]] | None = None,
) -> FromLogsReport:
    """Read local logs only. Never starts a network call or a live agent."""
    cutoff, window = _cutoff(since_days, since)
    until_epoch = None
    if until:
        try:
            text = until[:-1] + "+00:00" if until.endswith("Z") else until
            end = datetime.fromisoformat(text)
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            until_epoch = end.timestamp()
        except ValueError:
            pass

    notices: list[str] = []
    all_recs: list[UsageRecord] = []
    summaries: list[AdapterUsage] = []

    for ad in REGISTRY:
        recs = []
        try:
            recs = list(ad.read_usage_logs(ctx, since_days=since_days if since_days is not None else 30) or [])
        except OSError as exc:
            notices.append(f"{ad.id}: {exc}")
            summaries.append(AdapterUsage(adapter_id=ad.id, no_measurement=True, notes=[str(exc)]))
            continue
        # Adapters may stash skip notices on the last call via _last_log_notices.
        extra_notices = list(getattr(ad, "last_log_notices", []) or [])
        notices.extend(extra_notices)
        filtered = [r for r in recs if record_in_window(r, cutoff, until_epoch)]
        summaries.append(_summarize(ad.id, filtered, extra_notices))
        all_recs.extend(filtered)

    if collect_notices:
        notices.extend(collect_notices() or [])

    return FromLogsReport(window=window, adapters=summaries, notices=notices, records=all_recs)


def format_from_logs(report: FromLogsReport) -> str:
    lines = [f"{PRODUCT_NAME} bench --from-logs (window: {report.window})"]
    if not report.adapters:
        lines.append(NO_MEASUREMENT)
        return "\n".join(lines) + "\n"
    any_data = False
    for s in report.adapters:
        if s.no_measurement or s.records == 0:
            lines.append(f"{s.adapter_id}: {NO_MEASUREMENT}")
            continue
        any_data = True
        extra = f", {s.sessions} session(s)" if s.sessions else ""
        flag = "  [unverified semantics]" if s.unverified_semantics else ""
        lines.append(f"{s.adapter_id}: {s.records} record(s){extra}{flag}")
        lines.append(f"  input_uncached: {format_count(s.input_uncached)}")
        lines.append(f"  cache_read: {format_count(s.cache_read)}")
        lines.append(f"  cache_write: {format_count(s.cache_write)}")
        lines.append(f"  output: {format_count(s.output)}")
        lines.append(f"  reasoning: {format_count(s.reasoning)}")
        lines.append(f"  cost_usd: {NO_MEASUREMENT if s.cost_usd is None else f'{s.cost_usd:.6f}'}")
    if not any_data:
        lines.append(f"(no adapters produced usage records — {NO_MEASUREMENT})")
    if report.notices:
        lines.append("Notices:")
        for n in report.notices:
            lines.append(f"  - {n}")
    return "\n".join(lines) + "\n"


def _sum_usage(recs: list[UsageRecord] | None) -> dict[str, int | None]:
    if not recs:
        return {
            "input_uncached": None,
            "cache_read": None,
            "cache_write": None,
            "output": None,
            "reasoning": None,
            "cost_usd": None,
        }
    costs = [r.cost_usd for r in recs]
    cost = sum(c for c in costs if c is not None) if costs and all(c is not None for c in costs) else None
    return {
        "input_uncached": sum(r.input_tokens for r in recs),
        "cache_read": sum(r.cache_read_tokens for r in recs),
        "cache_write": sum(r.cache_write_tokens for r in recs),
        "output": sum(r.output_tokens for r in recs),
        "reasoning": sum(r.reasoning_tokens for r in recs),
        "cost_usd": cost,
    }


def _savings(before: dict, after: dict) -> dict[str, Any]:
    out = {}
    for key in ("input_uncached", "cache_read", "cache_write", "output", "reasoning", "cost_usd"):
        b, a = before.get(key), after.get(key)
        if b is None or a is None:
            out[key] = NO_MEASUREMENT
            continue
        delta = a - b
        out[key] = {"before": b, "after": a, "delta": delta}
    return out


_COPY_IGNORE_NAMES = {".git", ".venv", "__pycache__", "node_modules", ".pytest_cache"}
_COPY_IGNORE_GLOBS = ("*.pyc",)
_ENV_KEEP = {".env.example"}


def _is_env_secret(name: str) -> bool:
    return (name == ".env" or name.startswith(".env.")) and name not in _ENV_KEEP


def _copy_ignore(directory: str, names: list[str]) -> set[str]:
    ignored = set(COPY_IGNORE(directory, names))
    for name in names:
        if _is_env_secret(name):
            ignored.add(name)
    return ignored


def _rel_is_excluded(rel: str) -> bool:
    parts = Path(rel).parts
    if not parts:
        return True
    if any(part in _COPY_IGNORE_NAMES for part in parts):
        return True
    name = parts[-1]
    if any(fnmatch.fnmatch(name, pat) for pat in _COPY_IGNORE_GLOBS):
        return True
    if _is_env_secret(name):
        return True
    return False


def _symlink_parent(src: Path, rel: str) -> bool:
    cur = src
    for part in Path(rel).parts[:-1]:
        cur = cur / part
        if cur.is_symlink():
            return True
    return False


def _copy_one(src: Path, dest: Path) -> bool:
    """Copy a file or a symlink (as a symlink). Does not follow symlinked dirs. Returns True if copied."""
    try:
        if not src.exists() and not src.is_symlink():
            return False
    except OSError:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() or dest.is_symlink():
        if dest.is_dir() and not dest.is_symlink():
            return False
        try:
            dest.unlink()
        except OSError:
            return False
    try:
        if src.is_symlink():
            dest.symlink_to(os.readlink(src))
            return True
        if src.is_file():
            shutil.copy2(src, dest, follow_symlinks=False)
            return True
    except OSError:
        return False
    return False


def _count_copied(root: Path) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        total += len(filenames)
        for d in dirnames:
            if (Path(dirpath) / d).is_symlink():
                total += 1
    return total


def _copy_repo(src: Path, dest: Path) -> int:
    """Copy a project tree into dest. Returns the number of files copied.

    Inside a git work tree, copy only `git ls-files --cached --others --exclude-standard`.
    Otherwise fall back to copytree (still excluding COPY_IGNORE and .env secrets).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not src.is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        return 0
    listing = git_ls_files(src) if is_git_work_tree(src) else None
    if listing is not None:
        dest.mkdir(parents=True, exist_ok=True)
        copied = 0
        for rel in listing:
            if _rel_is_excluded(rel) or _symlink_parent(src, rel):
                continue
            source = src / rel
            try:
                missing = not source.exists() and not source.is_symlink()
            except OSError:
                missing = True
            if missing:
                continue
            if _copy_one(source, dest / rel):
                copied += 1
        return copied
    shutil.copytree(src, dest, ignore=_copy_ignore, symlinks=True, dirs_exist_ok=True)
    return _count_copied(dest)


@dataclass
class LiveBenchReport:
    ok: bool
    sandbox_root: str
    sandbox_home: str
    before: dict[str, Any] = field(default_factory=dict)
    after: dict[str, Any] = field(default_factory=dict)
    savings: dict[str, Any] = field(default_factory=dict)
    notices: list[str] = field(default_factory=list)
    applied_files: list[str] = field(default_factory=list)
    mode: str = "api-key"  # "api-key" (bench --bench) or "subscription" (bench --measure)
    after_model: str | None = None

    def to_json(self) -> dict:
        return asdict(self)




# Env var *names* used for the API-key bench. Values are never read, stored, or logged.
API_KEY_ENV = {
    "claude-code": "ANTHROPIC_API_KEY",
    "codex": "CODEX_API_KEY",
}

# Official, unmodified subscription commands (no --bare, no env overrides).
OFFICIAL_COMMANDS = {
    "claude-code": lambda prompt: ["claude", "-p", prompt, "--output-format", "json"],
    "codex": lambda prompt: ["codex", "exec", "--json", prompt],
}


def _key_name_present(name: str) -> bool:
    """True if the env var is set. Does not read or copy its value."""
    return name in os.environ


def _official_argv(adapter, prompt: str) -> list[str] | None:
    """Official headless command. Adapters may supply their own via `official_command(prompt)`."""
    adapter_id = adapter if isinstance(adapter, str) else adapter.id
    fn = OFFICIAL_COMMANDS.get(adapter_id)
    if fn:
        return fn(prompt)
    own = getattr(adapter, "official_command", None)
    return own(prompt) if callable(own) else None


def _bench_key_env(adapter) -> str | None:
    return API_KEY_ENV.get(adapter.id) or getattr(adapter, "bench_key_env", None)


def _prepare_agent(adapter) -> bool:
    if adapter.id in OFFICIAL_COMMANDS:
        return True
    return adapter.tier == 1 and getattr(adapter, "supports_prepare", True) and callable(getattr(adapter, "official_command", None))


def _format_cmd(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)


def live_bench(
    ctx: Context,
    task: dict | None = None,
    *,
    runner=None,
    adapters=None,
    timeout: int = 120,
    keep_sandbox: bool = False,
) -> LiveBenchReport:
    """API-key automated before/after in a throwaway copy.

    Isolation is by CLAUDE_CONFIG_DIR / CODEX_HOME pointing at empty sandbox
    dirs (created first). ANTHROPIC_API_KEY and CODEX_API_KEY pass through the
    process environment only if the user already exported them; this function
    never reads, logs, or stores their values. Missing keys yield no measurement.
    The real repo and real HOME are never written to. No credential files are
    copied, symlinked, or opened.
    """
    task = task or {"prompt": "List the files in this repository and summarize the README in one sentence."}
    runner = runner if runner is not None else default_headless_runner()
    agents = [a for a in (adapters or REGISTRY)
              if a.id in {"claude-code", "codex"} or (a.tier == 1 and a.detect(ctx))]
    if adapters:
        agents = list(adapters)

    tmp = tempfile.mkdtemp(prefix=f"{CLI_NAME}-bench-")
    sandbox_root = Path(tmp) / "repo"
    sandbox_home = Path(tmp) / "home"
    claude_dir = sandbox_home / ".claude"
    codex_dir = sandbox_home / ".codex"
    sandbox_home.mkdir(parents=True, exist_ok=True)
    claude_dir.mkdir(parents=True, exist_ok=True)
    codex_dir.mkdir(parents=True, exist_ok=True)
    copied = _copy_repo(ctx.root, sandbox_root)

    if sandbox_root.resolve() == ctx.root.resolve() or sandbox_home.resolve() == ctx.home.resolve():
        raise RuntimeError("sandbox path collided with the real repo or HOME; refusing to run")

    sandbox_ctx = Context(
        home=sandbox_home,
        root=sandbox_root,
        env={
            "HOME": str(sandbox_home),
            "CLAUDE_CONFIG_DIR": str(claude_dir),
            "CODEX_HOME": str(codex_dir),
        },
        snapshot=True,
        plan_override=ctx.plan_override,
    )
    notices: list[str] = [f"Copied {copied} files into the sandbox repo."]
    applied: list[str] = []

    def run_once(label: str, c: Context) -> dict[str, Any]:
        recs: list[UsageRecord] = []
        for ad in agents:
            key_name = _bench_key_env(ad)
            if key_name and not _key_name_present(key_name):
                notices.append(
                    f"{ad.id} ({label}): {key_name} is not set in the environment, "
                    f"so the API-key bench was skipped. {NO_MEASUREMENT}"
                )
                continue
            opts = {"bench": True, "sandboxDir": str(c.root), "timeoutS": timeout, "runner": runner, "home": str(c.home)}
            try:
                result = ad.run_headless_task(c, task, opts)
            except HeadlessRefused:
                raise
            except NotImplementedError as exc:
                notices.append(f"{ad.id} ({label}): {exc}")
                continue
            except OSError as exc:
                notices.append(f"{ad.id} ({label}): {exc}")
                continue
            usage = (result or {}).get("usage") or []
            recs.extend(usage)
            if not usage:
                notices.append(f"{ad.id} ({label}): {NO_MEASUREMENT}")
        return _sum_usage(recs)

    try:
        before = run_once("before", sandbox_ctx)
        changes = plan_file_changes(sandbox_ctx)
        if changes:
            applied_info = apply_changes(sandbox_ctx, changes, stamp="bench")
            applied = [r["path"] for r in applied_info["manifest"]["files"]]
            for path in applied:
                resolved = Path(path).resolve()
                if sandbox_home.resolve() not in resolved.parents and resolved != sandbox_home.resolve() \
                        and sandbox_root.resolve() not in resolved.parents and resolved != sandbox_root.resolve():
                    raise RuntimeError(f"bench write escaped the sandbox: {path}")
        after = run_once("after", sandbox_ctx)
        savings = _savings(before, after)
        return LiveBenchReport(
            ok=True,
            sandbox_root=str(sandbox_root),
            sandbox_home=str(sandbox_home),
            before=before,
            after=after,
            savings=savings,
            notices=notices,
            applied_files=applied,
        )
    finally:
        if not keep_sandbox:
            shutil.rmtree(tmp, ignore_errors=True)


def format_live_bench(report: LiveBenchReport) -> str:
    if report.mode == "subscription":
        lines = [f"{PRODUCT_NAME} bench --measure (subscription mode; read-only log comparison of a --prepare dir; real HOME/repo untouched)"]
        before_label = "Before (original copy)"
        after_label = "After (planned project files" + (f", --model {report.after_model}" if report.after_model else "") + ")"
    else:
        lines = [f"{PRODUCT_NAME} bench --bench (API-key mode; throwaway sandbox; real HOME/repo untouched)"]
        before_label = "Before (no profile)"
        after_label = "After (planned profile in sandbox HOME)"
    def fmt_side(name: str, side: dict) -> None:
        lines.append(f"{name}:")
        if all(v is None for v in side.values()):
            lines.append(f"  {NO_MEASUREMENT}")
            return
        for key in ("input_uncached", "cache_read", "cache_write", "output", "reasoning"):
            lines.append(f"  {key}: {format_count(side.get(key))}")
        cost = side.get("cost_usd")
        lines.append(f"  {cost_usd_line(cost)}")

    fmt_side(before_label, report.before)
    fmt_side(after_label, report.after)
    lines.append("Savings (measured only):")
    for key in ("input_uncached", "cache_read", "cache_write", "output", "reasoning", "cost_usd"):
        val = report.savings.get(key, NO_MEASUREMENT)
        if val == NO_MEASUREMENT or val is None:
            lines.append(f"  {key}: {NO_MEASUREMENT}")
        else:
            b, a, d = val["before"], val["after"], val["delta"]
            lines.append(f"  {key}: {b} → {a} ({d:+})")
    if report.applied_files:
        lines.append(f"Applied {len(report.applied_files)} planned file(s) inside the sandbox.")
    if report.notices:
        lines.append("Notices:")
        for n in report.notices:
            lines.append(f"  - {n}")
    return "\n".join(lines) + "\n"


def cost_usd_line(cost) -> str:
    return f"  cost_usd: {NO_MEASUREMENT if cost is None else f'{cost:.6f}'}"


@dataclass
class PrepareReport:
    ok: bool
    dest: str
    manifest: str
    before_repo: str
    after_repo: str
    prompt: str
    commands: dict
    notices: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)


def prepare_bench(ctx: Context, task: dict | None = None, *, adapters=None, dest: Path | None = None, after_model: str | None = None) -> PrepareReport:
    """Build before/after sandbox *repo* copies and print official CLI commands.

    The user runs those commands, signed in as usual. The kit does not
    set login env vars, redirect config dirs, or touch credential files.
    Project-level planned files are applied only inside the after repo copy.
    User-level planned files are not written to the real HOME.
    """
    task = task or {"prompt": "List the files in this repository and summarize the README in one sentence."}
    prompt = task.get("prompt") or ""
    agents = [a for a in (adapters or REGISTRY)
              if _prepare_agent(a) and (adapters or a.id in OFFICIAL_COMMANDS or a.detect(ctx))]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = Path(dest) if dest is not None else state_dir(ctx.home) / "benches" / stamp
    before_repo = dest / "before" / "repo"
    after_repo = dest / "after" / "repo"
    after_home = dest / "after" / "home"
    before_repo.parent.mkdir(parents=True, exist_ok=True)
    after_home.mkdir(parents=True, exist_ok=True)
    before_copied = _copy_repo(ctx.root, before_repo)
    after_copied = _copy_repo(ctx.root, after_repo)
    if before_repo.resolve() == ctx.root.resolve() or after_repo.resolve() == ctx.root.resolve():
        raise RuntimeError("prepare sandbox collided with the real repo; refusing")

    after_ctx = Context(
        home=after_home,
        root=after_repo,
        env={"HOME": str(after_home)},
        snapshot=True,
        plan_override=ctx.plan_override,
    )
    notices: list[str] = []
    applied: list[str] = []
    changes = [ch for ch in plan_file_changes(after_ctx) if _path_is_under(ch.get("path", ""), after_repo, after_home)]
    if changes:
        applied_info = apply_changes(after_ctx, changes, stamp="prepare")
        applied = [r["path"] for r in applied_info["manifest"]["files"]]
        for path in applied:
            if not _path_is_under(path, after_repo, after_home):
                raise RuntimeError(f"prepare write escaped the sandbox: {path}")
        notices.append(f"Applied {len(applied)} planned file(s) inside the after sandbox only (not your real HOME).")

    notices.append(
        f"Copied {before_copied} files to the before repo and {after_copied} files to the after repo."
    )

    commands = {"before": [], "after": []}
    for label, repo in (("before", before_repo), ("after", after_repo)):
        for ad in agents:
            argv = _official_argv(ad, prompt)
            if not argv:
                continue
            if label == "after" and after_model:
                argv = ad.apply_model_flag(argv, after_model)
            commands[label].append({"adapter": ad.id, "cwd": str(repo), "argv": argv, "cmd": _format_cmd(argv)})

    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = {
        "id": dest.name,
        "created": created,
        "prompt": prompt,
        "before_repo": str(before_repo),
        "after_repo": str(after_repo),
        "adapters": [a.id for a in agents],
        "commands": commands,
        "applied_files": applied,
        "after_model": after_model,
    }
    man_path = dest / "manifest.json"
    man_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return PrepareReport(
        ok=True,
        dest=str(dest),
        manifest=str(man_path),
        before_repo=str(before_repo),
        after_repo=str(after_repo),
        prompt=prompt,
        commands=commands,
        notices=notices,
    )


def _path_is_under(path: str, *roots: Path) -> bool:
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    for root in roots:
        try:
            r = root.resolve()
        except OSError:
            continue
        if resolved == r or r in resolved.parents:
            return True
    return False


def format_prepare(report: PrepareReport) -> str:
    lines = [
        f"{PRODUCT_NAME} bench --prepare (subscription mode)",
        "Run the official CLI commands below yourself, already signed in, as ordinary use.",
        "The kit does not set API keys, does not redirect your login, and does not touch credential files.",
        f"When you have run them, measure with: {CLI_NAME} bench --measure {shlex.quote(report.dest)}",
        f"Prepare dir: {report.dest}",
    ]
    for label, title in (("before", "Before (no profile) — run from this directory"), ("after", "After (project-level planned files in this copy)")):
        lines.append(f"{title}:")
        for item in report.commands.get(label) or []:
            lines.append(f"  cd {shlex.quote(item['cwd'])}")
            lines.append(f"  {item['cmd']}")
    if report.notices:
        lines.append("Notices:")
        for n in report.notices:
            lines.append(f"  - {n}")
    return "\n".join(lines) + "\n"


def _in_scope(rec: UsageRecord, repo: str, bench_id: str) -> bool:
    extra = rec.extra or {}
    hay = " ".join([
        rec.source or "",
        rec.session_id or "",
        rec.task_id or "",
        str(extra.get("path") or ""),
        str(extra.get("cwd") or ""),
    ]).lower()
    repo_l = repo.lower()
    stamp = bench_id.lower()
    if repo_l and repo_l in hay:
        return True
    # Claude project dirs often encode the path; the bench stamp and before/after segment remain.
    if stamp and stamp in hay:
        # Distinguish before vs after by the path segment if present.
        if "/before/" in repo_l.replace("\\", "/") or repo_l.endswith("/before/repo") or repo_l.endswith("/before/repo/"):
            return "before" in hay
        if "/after/" in repo_l.replace("\\", "/") or repo_l.endswith("/after/repo") or repo_l.endswith("/after/repo/"):
            return "after" in hay
        return True
    return False


def measure_bench(ctx: Context, manifest_path: Path, *, until: str | None = None) -> LiveBenchReport:
    """Read session logs for a prepared bench, scoped to the sandbox repos and time window."""
    path = Path(manifest_path)
    if path.is_dir():
        path = path / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    created = data.get("created")
    before_repo = data.get("before_repo") or ""
    after_repo = data.get("after_repo") or ""
    bench_id = data.get("id") or path.parent.name
    report = from_logs(ctx, since=created, until=until)
    before_recs = [r for r in report.records if _in_scope(r, before_repo, bench_id)]
    after_recs = [r for r in report.records if _in_scope(r, after_repo, bench_id)]
    notices = list(report.notices)
    if not before_recs:
        notices.append(f"before: {NO_MEASUREMENT}")
    if not after_recs:
        notices.append(f"after: {NO_MEASUREMENT}")
    before = _sum_usage(before_recs)
    after = _sum_usage(after_recs)
    return LiveBenchReport(
        ok=True,
        sandbox_root=str(path.parent),
        sandbox_home="",
        before=before,
        after=after,
        savings=_savings(before, after),
        notices=notices,
        applied_files=list(data.get("applied_files") or []),
        mode="subscription",
        after_model=data.get("after_model"),
    )


def latest_prepare_dir(ctx: Context) -> Path | None:
    root = state_dir(ctx.home) / "benches"
    if not root.is_dir():
        return None
    found = sorted((p for p in root.iterdir() if p.is_dir() and (p / "manifest.json").is_file()), key=lambda p: p.name)
    return found[-1] if found else None

def cmd_from_logs_json(report: FromLogsReport) -> str:
    return json.dumps(report.to_json(), indent=2, default=str) + "\n"
