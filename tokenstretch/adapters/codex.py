"""Codex adapter. All Codex-specific paths, logs, and checks live here."""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..context import Context
from ..identity import STATE_DIRNAME
from ..files import count_lines_bytes, read_toml, walk_glob_names
from ..findings import Finding, UsageRecord
from ..util import is_loopback_url
from .base import Adapter, SetupItem, add_rules_block, rules_block, BaseUrl, ConfigLocation, DetectResult, HeadlessRun, HookPlan, InstallStep, InstructionFile, McpServer
HEAVY = re.compile(r"(gpt-6.*astra|gpt-5-?pro|o3|o4|gpt-6\.1-?pro)", re.I)
CEILING = {"xhigh", "max", "ultra"}
UNOFFICIAL = ("chatgpt.com/backend-api/codex", "backend-api/codex")


def _cfg_paths(ctx: Context) -> list[Path]:
    return [ctx.home / ".codex" / "config.toml", ctx.root / ".codex" / "config.toml"]


def _load_cfgs(ctx: Context) -> list[tuple[Path, dict]]:
    out = []
    for p in _cfg_paths(ctx):
        data = read_toml(p)
        if isinstance(data, dict) and "__error__" not in data:
            out.append((p, data))
    return out


def _merged(ctx: Context) -> tuple[dict, dict[str, str]]:
    merged, sources = {}, {}
    for p, data in _load_cfgs(ctx):
        for k, v in data.items():
            if k in {"mcp_servers", "hooks", "model_providers", "features", "agents"} and isinstance(v, dict):
                base = dict(merged.get(k) or {})
                base.update(v)
                merged[k] = base
            else:
                merged[k] = v
            sources[k] = str(p)
    return merged, sources


def _agents_chain(ctx: Context) -> list[Path]:
    paths = [ctx.home / ".codex" / "AGENTS.md"]
    cur, root = ctx.root, ctx.root
    # Walk from cwd/root up, collecting AGENTS.md (repo root to cwd).
    seen = set()
    chain = []
    while True:
        p = cur / "AGENTS.md"
        if p.is_file() and p.resolve() not in seen:
            seen.add(p.resolve())
            chain.append(p)
        if cur.parent == cur:
            break
        cur = cur.parent
        if len(chain) > 12:
            break
    # Prefer closer files last (they'll already be included). Also catch nested copies under root.
    for p in walk_glob_names(ctx.root, "AGENTS.md", max_depth=4):
        if p.resolve() not in seen:
            chain.append(p)
            seen.add(p.resolve())
    return [p for p in paths + chain if p.is_file()]


def _hooks(ctx: Context, cfg: dict) -> list[tuple[str, str]]:
    out = []
    hooks = cfg.get("hooks") or {}
    if isinstance(hooks, dict):
        for event, groups in hooks.items():
            if not isinstance(groups, list):
                continue
            for g in groups:
                for h in (g.get("hooks") if isinstance(g, dict) else None) or []:
                    if isinstance(h, dict) and h.get("command"):
                        out.append((str(h["command"]), f"hooks.{event}"))
    hooks_json = ctx.home / ".codex" / "hooks.json"
    data = None
    try:
        import json as _json
        data = _json.loads(hooks_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        for event, groups in data.items():
            if not isinstance(groups, list):
                continue
            for g in groups:
                for h in (g.get("hooks") if isinstance(g, dict) else None) or []:
                    if isinstance(h, dict) and h.get("command"):
                        out.append((str(h["command"]), f"{hooks_json}:{event}"))
    return out


class CodexAdapter(Adapter):
    id = "codex"
    display_name = "Codex"
    tier = 1
    vendor_terms = "OpenAI"
    implemented_checks = frozenset({
        "W02", "W03", "W05", "W06", "W07", "W10", "W13", "W14", "T01", "T06",
    })
    config_dir_env = ("CODEX_HOME",)
    headless_patterns = (r"\bcodex\b.*\bexec\b",)

    def detect(self, ctx: Context) -> DetectResult:
        binary = ctx.which("codex")
        roots = [str(p) for p in (
            ctx.home / ".codex",
            ctx.root / ".codex",
            ctx.root / "AGENTS.md",
        ) if p.exists()]
        installed = bool(roots or binary)
        return DetectResult(
            installed=installed,
            binary=str(binary) if binary else None,
            config_roots=roots,
            auth_mode=self._auth_mode_public(ctx) if installed else "unknown",
        )

    def read_config(self, ctx: Context) -> dict:
        cfg, sources = _merged(ctx)
        effective: dict = {}
        if cfg.get("model"):
            effective["model"] = {"value": cfg["model"], "from": sources.get("model", "config.toml")}
        for key, norm in (("model_reasoning_effort", "effort"), ("service_tier", "fast_mode")):
            if cfg.get(key) is not None:
                effective[norm] = {"value": cfg[key], "from": sources.get(key, "config.toml")}
        urls = self.base_urls(ctx)
        if urls:
            effective["base_url"] = {"value": urls[0].url, "from": urls[0].where}
        if cfg.get("model_auto_compact_token_limit") is not None:
            effective["compaction"] = {"value": cfg["model_auto_compact_token_limit"], "from": sources.get("model_auto_compact_token_limit", "config.toml")}
        analytics = (cfg.get("analytics") or {}) if isinstance(cfg.get("analytics"), dict) else {}
        if "enabled" in analytics:
            effective["telemetry"] = {"value": analytics.get("enabled"), "from": "analytics.enabled"}
        raw = []
        for path, data in _load_cfgs(ctx):
            scope = "user" if path == ctx.home / ".codex" / "config.toml" else "project"
            raw.append({"file": str(path), "scope": scope, "data": data})
        return {"effective": effective, "raw": raw}

    def list_instruction_files(self, ctx: Context, cwd: str | None = None) -> list[InstructionFile]:
        out = []
        home_codex = ctx.home / ".codex"
        for path in _agents_chain(ctx):
            counted = count_lines_bytes(path)
            if not counted:
                continue
            lines, size, _ = counted
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if home_codex in resolved.parents or resolved.parent == home_codex:
                scope = "user"
            else:
                scope = "project"
            out.append(InstructionFile(
                path=str(path), scope=scope, bytes=size, lines=lines, always_loaded=True,
            ))
        return out

    def list_mcp_servers(self, ctx: Context) -> list[McpServer]:
        cfg, sources = _merged(ctx)
        servers = cfg.get("mcp_servers") or {}
        out = []
        if not isinstance(servers, dict):
            return out
        file = sources.get("mcp_servers", str(ctx.home / ".codex" / "config.toml"))
        for name, entry in servers.items():
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            typ = str(entry.get("type") or "")
            if typ in {"http", "sse"}:
                transport = typ
            elif url:
                transport = typ or "http"
            else:
                transport = "stdio"
            out.append(McpServer(
                name=name,
                scope="user" if file.startswith(str(ctx.home)) else "project",
                transport=transport,
                enabled=entry.get("enabled") is not False,
                file=file,
                remote_url=str(url) if url else None,
            ))
        return out

    def read_usage_logs(self, ctx: Context, since_days: int = 30) -> list[UsageRecord]:
        import time
        from ..usage import drop_forked_replays, record_in_window
        cutoff = time.time() - since_days * 86400
        self.last_log_notices = []
        records = []
        for path in self.default_log_paths(ctx):
            recs, notices = parse_codex_rollout(path, self.id)
            self.last_log_notices.extend(notices)
            if not recs and notices:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = cutoff
            for r in recs:
                if r.ts:
                    if record_in_window(r, cutoff, None):
                        records.append(r)
                elif mtime >= cutoff:
                    records.append(r)
        return drop_forked_replays(records)

    def headless_env(self, home: Path) -> dict[str, str]:
        return {"HOME": str(home), "CODEX_HOME": str(home / ".codex")}


    def config_locations(self, ctx: Context) -> list[ConfigLocation]:
        cred = ctx.home / ".codex" / "auth.json"
        mcp_cred = ctx.home / ".codex" / ".credentials.json"
        locs = [
            ConfigLocation(cred, "credential", ctx.exists(cred), sensitive=True),
            ConfigLocation(mcp_cred, "credential", ctx.exists(mcp_cred), sensitive=True),
        ]
        for p in _cfg_paths(ctx):
            locs.append(ConfigLocation(p, "settings", p.is_file()))
        hooks = ctx.home / ".codex" / "hooks.json"
        locs.append(ConfigLocation(hooks, "hooks", hooks.is_file()))
        for p in _agents_chain(ctx):
            locs.append(ConfigLocation(p, "memory", True))
        return locs

    def auth_mode(self, ctx: Context) -> str:
        if ctx.plan_override in {"subscription", "api"}:
            return "subscription" if ctx.plan_override == "subscription" else "api"
        cfg, _ = _merged(ctx)
        if cfg.get("forced_login_method") == "api":
            return "api"
        # Never open auth.json; presence plus no forced API login => treat as subscription-unknown.
        if ctx.exists(ctx.home / ".codex" / "auth.json"):
            return "subscription"
        return "unknown"

    def base_urls(self, ctx: Context) -> list[BaseUrl]:
        cfg, sources = _merged(ctx)
        urls = []
        for key in ("openai_base_url", "chatgpt_base_url"):
            if cfg.get(key):
                urls.append(BaseUrl(str(cfg[key]), sources.get(key, key), key))
        providers = cfg.get("model_providers") or {}
        if isinstance(providers, dict):
            for pid, entry in providers.items():
                if isinstance(entry, dict) and entry.get("base_url"):
                    urls.append(BaseUrl(str(entry["base_url"]), f"model_providers.{pid}.base_url", "base_url"))
        return urls

    def hook_commands(self, ctx: Context) -> list[tuple[str, str]]:
        cfg, _ = _merged(ctx)
        return _hooks(ctx, cfg)

    def effective_models(self, ctx: Context) -> list[tuple[str, str]]:
        cfg, sources = _merged(ctx)
        if cfg.get("model"):
            return [(str(cfg["model"]), sources.get("model", "config.toml"))]
        return []

    def checks(self, ctx: Context) -> list[Finding]:
        return (self._w02(ctx) + self._w03(ctx) + self._w05(ctx) + self._w06(ctx)
                + self._w07(ctx) + self._w10(ctx) + self._w13(ctx) + self._w14(ctx)
                + self._t01(ctx) + self._t06(ctx))

    def privacy_checks(self, ctx: Context) -> list[Finding]:
        return []

    def _w02(self, ctx: Context) -> list[Finding]:
        files = _agents_chain(ctx)
        if not files:
            return []
        cfg, _ = _merged(ctx)
        cap = int(cfg.get("project_doc_max_bytes") or 32768)
        total = 0
        details = []
        for p in files:
            c = count_lines_bytes(p)
            if not c:
                continue
            _, size, _ = c
            total += size
            details.append((p, size))
        if not details:
            return []
        findings = []
        if total > cap:
            findings.append(self.finding(
                "W02", "agents-md-size", "info", "waste",
                f"Instruction files total {total} bytes, over the {cap}-byte cap; content past the cap is silently dropped.",
                "Trim the files and split them into per-directory instruction files.",
                "CX-CFG, CX-PRICE",
                file=str(details[-1][0]), key="bytes", value=total,
            ))
        if total > 16 * 1024:
            findings.append(self.finding(
                "W02", "agents-md-size", "med", "waste",
                f"Codex re-reads its instruction files every session. Yours total {round(total/1024, 1)} KB; "
                "trim them to the rules that matter.",
                "Trim and split into per-directory instruction files.",
                "CX-CFG, CX-PRICE",
                file=str(details[-1][0]), key="bytes", value=total,
            ))
        return findings

    def _w03(self, ctx: Context) -> list[Finding]:
        cfg, sources = _merged(ctx)
        servers = cfg.get("mcp_servers") or {}
        enabled = []
        if isinstance(servers, dict):
            for name, entry in servers.items():
                if isinstance(entry, dict) and entry.get("enabled") is False:
                    continue
                enabled.append(name)
        if not enabled:
            return []
        return [self.finding(
            "W03", "mcp-unused", "med", "waste",
            f"{len(enabled)} tool connections are configured. Official guidance is to disable unused ones, "
            "because each server adds context to your messages.",
            "Set mcp_servers.<id>.enabled = false or restrict enabled_tools. Make the change between sessions.",
            "CX-CFG, CX-PRICE",
            file=sources.get("mcp_servers", "config.toml"), key="mcp_servers", value=enabled,
        )]

    def _w05(self, ctx: Context) -> list[Finding]:
        models = self.effective_models(ctx)
        if not models:
            return []
        model, where = models[0]
        if not HEAVY.search(model) and "astra" not in model.lower():
            # Flag only clearly top-tier names; leave mid-tier defaults alone.
            return []
        return [self.finding(
            "W05", "default-model-heavy", "med", "waste",
            "You're running a top-tier model as the default for everything, including small edits.",
            "Set a cheaper default and switch up only for hard tasks.",
            "CX-CFG, CX-PRICE",
            file=where, key="model", value=model,
        )]

    def _w06(self, ctx: Context) -> list[Finding]:
        cfg, sources = _merged(ctx)
        findings = []
        for key in ("model_reasoning_effort", "plan_mode_reasoning_effort"):
            val = str(cfg.get(key) or "").lower()
            if val in CEILING:
                findings.append(self.finding(
                    "W06", "effort-max", "med", "waste",
                    "Your AI is set to think as hard as possible on every message. That burns your allowance fastest. "
                    "'High' or 'medium' is plenty for everyday work.",
                    f"Lower {key} to high or medium and raise it only per task.",
                    "CX-CFG",
                    file=sources.get(key, "config.toml"), key=key, value=val,
                ))
        return findings

    def _w07(self, ctx: Context) -> list[Finding]:
        cfg, sources = _merged(ctx)
        tier = str(cfg.get("service_tier") or "").lower()
        features = cfg.get("features") or {}
        ultra = isinstance(features, dict) and (features.get("ultrafast_mode") is True or str(features.get("ultrafast_mode")).lower() == "true")
        if tier in {"fast", "priority", "ultrafast"} or ultra:
            return [self.finding(
                "W07", "fast-mode-on", "high", "waste",
                "'Fast mode' is on by default. Fast uses 2.5× included usage and Ultrafast uses 8×. "
                "Turn it on only when you're in a hurry.",
                "Remove service_tier and disable ultrafast mode. If you need it, enable it at session start.",
                "CX-CFG, CX-PRICE",
                file=sources.get("service_tier", "config.toml"), key="service_tier", value=tier or "ultrafast",
            )]
        return []

    def _w10(self, ctx: Context) -> list[Finding]:
        findings = []
        for b in self.base_urls(ctx):
            if is_loopback_url(b.url) or "127.0.0.1" in b.url or "localhost" in b.url:
                findings.append(self.finding(
                    "W10", "chained-proxies", "med" if self.auth_mode(ctx) == "api" else "high", "waste",
                    f"Your AI traffic is going through a program on your computer first ({b.url}). "
                    "Chained middlemen can strip the caching discount and double-process your messages.",
                    "Keep at most one proxy, and only with an API key. Remove the base URL for subscription use.",
                    "CX-CFG, CC-CACHE",
                    file=b.where, key=b.key, value=b.url,
                ))
        return findings

    def _w13(self, ctx: Context) -> list[Finding]:
        cfg, sources = _merged(ctx)
        features = cfg.get("features") or {}
        agents = cfg.get("agents") or {}
        multi = features.get("multi_agent") if isinstance(features, dict) else None
        max_t = None
        if isinstance(agents, dict):
            max_t = agents.get("max_concurrent_threads_per_session") or agents.get("max_threads")
        if multi is True or (isinstance(max_t, int) and max_t and max_t > 2):
            return [self.finding(
                "W13", "subagent-share", "info", "waste",
                "Multiple helper agents can run at once. Each one loads instructions and tools again.",
                "Lower agents.max_concurrent_threads_per_session, or disable multi-agent features if you don't need them.",
                "CX-CFG, CC-COST",
                file=sources.get("agents", "config.toml"), key="max_threads", value=max_t or multi,
            )]
        return []

    def _w14(self, ctx: Context) -> list[Finding]:
        # Session logs: ~/.codex/sessions/** (path is documented as [reasoning] in the brief).
        sessions = list((ctx.home / ".codex" / "sessions").glob("**/*")) if (ctx.home / ".codex" / "sessions").is_dir() else []
        jsonl = [p for p in sessions if p.name.endswith(".jsonl") or p.name.endswith(".jsonl.zst")]
        peaks = []
        from ..logcache import memo_by_stat
        for p in jsonl:
            session_total = memo_by_stat(p, "codex-w14", lambda p=p: sum(r.total_input() for r in self.parse_log(p)))
            if session_total:
                peaks.append(session_total)
        if not peaks:
            return []
        peaks.sort()
        mid = peaks[len(peaks) // 2]
        if mid > 150_000:
            return [self.finding(
                "W14", "long-sessions", "med", "waste",
                "You're keeping one giant conversation going. Every new message re-sends all of it. "
                "Start fresh when you switch tasks.",
                "Start a new session between tasks. Consider model_auto_compact_token_limit.",
                "CX-CFG",
                key="median_context_tokens", value=mid,
            )]
        return []

    def _t01(self, ctx: Context) -> list[Finding]:
        if self.auth_mode(ctx) == "api":
            return []
        proxies = [b for b in self.base_urls(ctx) if is_loopback_url(b.url) or "localhost" in b.url or "127.0.0.1" in b.url]
        if not proxies:
            return []
        return [self.finding(
            "T01", "tos-oauth-proxy", "high", "tos",
            "Your ChatGPT plan login is passing through a third-party program. OpenAI's terms bar circumventing "
            "usage limits and sharing account credentials. This could get your account flagged.",
            "Remove the base URL, or switch that tool to an API key (forced_login_method = \"api\").",
            "OAI-TOS, OAI-SA, CX-CFG",
            file=proxies[0].where, key=proxies[0].key, value=proxies[0].url,
        )]

    def _t06(self, ctx: Context) -> list[Finding]:
        findings = []
        for b in self.base_urls(ctx):
            low = b.url.lower()
            if any(s in low for s in UNOFFICIAL) or (b.key == "chatgpt_base_url" and "chatgpt.com" in low and "backend-api" in low):
                findings.append(self.finding(
                    "T06", "tos-unofficial-endpoint", "high", "tos",
                    "A tool talks to the app's private back door instead of the official API. "
                    "That breaks the terms and can stop working any day.",
                    "Remove the tool and any unofficial endpoint override.",
                    "CX-CFG, OAI-TOS",
                    file=b.where, key=b.key, value=b.url,
                ))
        return findings

    def default_log_paths(self, ctx: Context) -> list[Path]:
        return _codex_log_paths(ctx)

    def sniff_log(self, path: Path, first_lines: list[str]) -> bool:
        blob = "\n".join(first_lines)
        return "session_meta" in blob or "event_msg" in blob or "openai.codex" in blob or '"provider": "openai"' in blob

    def parse_log(self, path: Path) -> list[UsageRecord]:
        recs, notices = parse_codex_rollout(path, self.id)
        existing = getattr(self, "last_log_notices", None)
        if existing is None:
            self.last_log_notices = list(notices)
        else:
            existing.extend(notices)
        return recs

    def apply_model_flag(self, argv: list[str], model: str) -> list[str]:
        out = list(argv)
        out.extend(["--model", model])
        return out

    def headless_run(self, prompt: str, cwd: Path, model: str | None = None) -> HeadlessRun | None:
        argv = ["codex", "exec", "--json", prompt]
        if model:
            argv = self.apply_model_flag(argv, model)
        return HeadlessRun(argv, cwd)

    def parse_headless_output(self, stdout: str, task_id: str) -> UsageRecord | None:
        from ..usage import normalize_usage, record_from_normalized
        for line in stdout.splitlines()[::-1]:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            u = obj.get("usage") or {}
            if u or obj.get("cost_usd") is not None:
                info = u.get("total_token_usage") or u
                last = u.get("last_token_usage") or info
                src = last if last else info
                norm = normalize_usage(
                    input_tokens=int(src.get("input_tokens") or src.get("input") or 0),
                    cache_read=int(src.get("cached_input_tokens") or src.get("cache_read_tokens") or 0),
                    cache_write=int(src.get("cache_write_input_tokens") or src.get("cache_write_tokens") or 0),
                    output=int(src.get("output_tokens") or src.get("output") or 0),
                    reasoning=int(src.get("reasoning_output_tokens") or src.get("reasoning_tokens") or 0),
                    input_includes_cache=True,
                )
                cost = obj.get("cost_usd")
                return record_from_normalized(
                    self.id, "headless", norm, task_id=task_id,
                    model=obj.get("model") or (obj.get("payload") or {}).get("model"),
                    cost_usd=float(cost) if cost is not None else None,
                )
        return None



    def caveman_skill_dir(self, ctx: Context) -> Path:
        return ctx.home / ".agents" / "skills" / "caveman"

    HOOK_EVENTS = {
        "pre_tool_shell_filter": "PreToolUse",
        "pre_read_gate": "PreToolUse",
        "session_start_note": "SessionStart",
    }

    # ---- watch / model picker -----------------------------------------------------------
    # No tier map: Codex model names change often, so the picker names a tier and `/model` only.
    def is_top_model(self, model: str) -> bool:
        return bool(HEAVY.search(model or ""))

    def model_switch_hint(self, model: str) -> str:
        return "/model"

    incremental_logs = True

    def usage_log_parser(self, path: Path):
        from ..usage import is_zst_jsonl
        return None if is_zst_jsonl(path) else CodexRolloutParser(path, self.id)

    def finish_usage_records(self, records: list) -> list:
        from ..usage import drop_forked_replays
        return drop_forked_replays(records)

    def watch_sessions(self, ctx: Context, limit: int = 5, max_age_s: float | None = None) -> list[dict] | None:
        """Per-turn usage from the newest rollout logs. Rollouts carry no Read-call paths we rely on."""
        import time
        from ..logcache import memo_by_stat
        cut = time.time() - max_age_s if max_age_s else None
        paths = []
        for p in _codex_log_paths(ctx):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if cut is None or m >= cut:
                paths.append((m, p))
        out = []
        for mtime, path in sorted(paths, reverse=True)[:limit]:
            recs, _ = memo_by_stat(path, "codex-rollout", lambda path=path: parse_codex_rollout(path, self.id))
            turns = [{
                "ts": r.ts, "model": r.model or "", "history_tokens": r.total_input(),
                "output_tokens": r.output_tokens, "tool_calls": None, "reads": [],
            } for r in recs]
            if not turns:
                continue
            out.append({
                "adapter": self.id, "session": _session_id_from_name(path)[-8:], "project": path.parent.name,
                "path": str(path), "last_write": mtime, "turns": turns,
                "clear_hint": "/new", "compact_hint": "/compact",
            })
        return out

    def setup_items(self, ctx: Context) -> list[SetupItem]:
        """Credit-saving rules in the project AGENTS.md. Plans only; the core writes with backups."""
        if ctx.root.resolve() == ctx.home.resolve():
            return []
        path = ctx.root / "AGENTS.md"
        existing = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        new = add_rules_block(existing, rules_block("a smaller model", "/new"))
        if new is None:
            return []
        return [SetupItem(
            id="codex-rules-block", adapter_id=self.id,
            title="Add 5 short credit-saving rules to the project AGENTS.md",
            why="Codex re-sends the conversation each step. Fresh tasks, narrow reads, and short replies keep that small.",
            changes=[{
                "path": str(path), "kind": "modify" if path.is_file() else "create",
                "content": new, "key": "credit-rules block",
                "reason": "append a marked rules block (about 0.7 KB)",
            }],
            done="added credit-saving rules to AGENTS.md",
        )]

    def install_hooks(self, ctx: Context, plan: HookPlan | dict | None = None, *, dry_run: bool = True) -> dict:
        """Plan hooks.json edits. Never writes; the installer applies changes."""
        items = plan.items if isinstance(plan, HookPlan) else list((plan or {}).get("items") or [])
        path = ctx.home / ".codex" / "hooks.json"
        data = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, json.JSONDecodeError):
                data = {}
        dirty = False
        if "pre_tool_shell_filter" in items:
            if _ensure_codex_hook(data, "PreToolUse", "rtk"):
                dirty = True
        if "pre_read_gate" in items:
            # Codex has no native permissions.deny; map the intent to a PreToolUse hook
            # that points at the kit's local read-gate script (created by the host adapter).
            script = ctx.home / STATE_DIRNAME / "hooks" / "read-gate.sh"
            if _ensure_codex_hook(data, "PreToolUse", str(script)):
                dirty = True
        if "session_start_note" in items:
            script = ctx.home / STATE_DIRNAME / "hooks" / "session-start.sh"
            if _ensure_codex_hook(data, "SessionStart", str(script)):
                dirty = True
        changes = []
        if dirty:
            changes.append({
                "path": str(path),
                "kind": "modify" if path.is_file() else "create",
                "content": json.dumps(data, indent=2) + "\n",
                "reason": "Wire Clean-profile Codex hooks (RTK filter, read gate, session-start note).",
            })
        return {"changes": changes, "backup_dir": ""}

    def plan_api_proxy_settings(self, ctx: Context, *, proxy_id: str, base_url: str, env: dict) -> list[dict]:
        """Point Codex at a loopback proxy through openai_base_url in config.toml (never shell rc)."""
        if not self.detect(ctx):
            return []
        path = ctx.home / ".codex" / "config.toml"
        try:
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
        except OSError:
            text = ""
        from ..api_proxy import upsert_toml_key
        content = upsert_toml_key(text, "openai_base_url", base_url)
        # chatgpt_base_url is unofficial here; do not invent or set it.
        return [{
            "path": str(path),
            "kind": "modify" if path.is_file() else "create",
            "content": content,
            "reason": (
                f"Point Codex at the {proxy_id} proxy via openai_base_url in "
                "adapter-owned config.toml (loopback only)."
            ),
        }]

    def install_steps(self, tool_id: str, ctx: Context, options: dict) -> list[InstallStep] | None:

        if tool_id == "rtk-oss":
            return [InstallStep("Wire the OSS command-output hook for this agent.",
                                command=["rtk", "init", "-g", "--codex"], manual=True)]
        if tool_id == "tokeneconomizer":
            return [InstallStep("Register the local plugin marketplace from the pinned checkout, then add it.",
                                command=None, manual=True)]
        return None


def _ensure_codex_hook(data: dict, event: str, command: str) -> bool:
    groups = data.setdefault(event, [])
    if not isinstance(groups, list):
        groups = []
        data[event] = groups
    for g in groups:
        if not isinstance(g, dict):
            continue
        if g.get("command") == command:
            return False
        for h in g.get("hooks") or []:
            if isinstance(h, dict) and h.get("command") == command:
                return False
    groups.append({"hooks": [{"type": "command", "command": command}]})
    return True


def _codex_homes(ctx: Context) -> list[Path]:
    val, _ = ctx.env_or_rc("CODEX_HOME")
    if val:
        # Codex itself takes one path; a comma list is tolerated for fixtures.
        return [Path(p.strip()).expanduser() for p in val.split(",") if p.strip()]
    return [ctx.home / ".codex"]


def _codex_log_paths(ctx: Context) -> list[Path]:
    out: list[Path] = []
    seen: set[Path] = set()
    for home in _codex_homes(ctx):
        for sub in ("sessions", "archived_sessions"):
            root = home / sub
            if not root.is_dir():
                continue
            for pat in ("**/*.jsonl", "**/*.jsonl.zst"):
                for p in root.glob(pat):
                    if p not in seen:
                        seen.add(p)
                        out.append(p)
    return out


def _usage_tuple(u: dict) -> tuple:
    if not isinstance(u, dict):
        return (0, 0, 0, 0, 0, 0)
    return (
        int(u.get("input_tokens") or u.get("input") or 0),
        int(u.get("cached_input_tokens") or u.get("cache_read_tokens") or 0),
        int(u.get("cache_write_input_tokens") or u.get("cache_write_tokens") or 0),
        int(u.get("output_tokens") or u.get("output") or 0),
        int(u.get("reasoning_output_tokens") or u.get("reasoning_tokens") or 0),
        int(u.get("total_tokens") or 0),
    )


def _codex_rec(adapter_id: str, usage: dict, *, ts=None, model=None, session_id=None, extra=None, task_id=None) -> "UsageRecord":
    from ..usage import normalize_usage, parse_ts, record_from_normalized
    if not isinstance(usage, dict):
        usage = {}
    norm = normalize_usage(
        input_tokens=int(usage.get("input_tokens") or usage.get("input") or 0),
        cache_read=int(usage.get("cached_input_tokens") or usage.get("cache_read_tokens") or 0),
        cache_write=int(usage.get("cache_write_input_tokens") or usage.get("cache_write_tokens") or 0),
        output=int(usage.get("output_tokens") or usage.get("output") or 0),
        reasoning=int(usage.get("reasoning_output_tokens") or usage.get("reasoning_tokens") or 0),
        input_includes_cache=True,
    )
    return record_from_normalized(
        adapter_id, "log", norm,
        ts=parse_ts(ts), model=model, session_id=session_id, task_id=task_id,
        extra=extra or {},
    )


def _session_id_from_name(path: Path) -> str:
    name = path.name
    for suffix in (".jsonl.zst", ".jsonl"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    if name.startswith("rollout-"):
        name = name[len("rollout-"):]
    return name


_CODEX_MARKS = (b"usage", b"session_meta", b"turn_context", b"token_count", b"thread_settings_applied")


class CodexRolloutParser:
    """Incremental rollout parser: feed() complete lines in order. Dedupes unchanged totals.

    Only lines that mention a usage / session / model marker are JSON-parsed; the large
    response_item lines (prompts, code, tool output) are skipped by a substring check.
    """

    def __init__(self, path: Path, adapter_id: str) -> None:
        self.path = path
        self.adapter_id = adapter_id
        self.records: list = []
        self.session_id = _session_id_from_name(path)
        self.model = None
        self.service_tier = None
        self.prev_total = None
        self.pending_fp = None

    def feed(self, lines: list[bytes]) -> None:
        from ..logcache import loads
        for raw in lines:
            if not any(m in raw for m in _CODEX_MARKS):
                continue
            obj = loads(raw.strip())
            if isinstance(obj, dict):
                self.feed_obj(obj)

    def feed_obj(self, obj: dict) -> None:
        from ..usage import fingerprint
        path, records, adapter_id = self.path, self.records, self.adapter_id
        typ = obj.get("type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        ts = obj.get("timestamp")

        # Legacy one-line fixture: {model, usage: {...}} with no rollout envelope.
        if typ is None and (obj.get("usage") or obj.get("token_usage")):
            u = obj.get("usage") or obj.get("token_usage")
            records.append(_codex_rec(
                adapter_id, u, ts=ts or obj.get("ts"), model=obj.get("model"),
                session_id=self.session_id, extra={"path": str(path), "legacy": True},
            ))
            return
        if typ == "session_meta":
            self.session_id = (
                payload.get("id") or payload.get("session_id") or payload.get("thread_id") or self.session_id
            )
            return
        if typ == "turn_context":
            self.model = payload.get("model") or self.model
            return

        ev_type = typ
        ev_payload = payload
        if typ == "event_msg":
            ev_type = payload.get("type")

        if ev_type == "thread_settings_applied":
            settings = ev_payload.get("thread_settings") or ev_payload
            if isinstance(settings, dict):
                self.service_tier = settings.get("service_tier") or self.service_tier
            return
        tier = {"service_tier": self.service_tier} if self.service_tier else {}
        if ev_type == "token_count":
            info = ev_payload.get("info") if isinstance(ev_payload, dict) else None
            if not info:
                return
            total = info.get("total_token_usage") or {}
            last = info.get("last_token_usage")
            total_fp = _usage_tuple(total) if total else None
            if self.prev_total is not None and total_fp == self.prev_total:
                return  # unchanged totals (rate-limit-only update)
            src = last if last else total
            rec = _codex_rec(adapter_id, src, ts=ts, model=self.model, session_id=self.session_id,
                             extra={"path": str(path), **tier})
            self.prev_total = total_fp if total_fp is not None else self.prev_total
            records.append(rec)
            self.pending_fp = fingerprint(rec)
            return
        if typ == "token_usage_record" or ev_type == "token_usage_record":
            u = ev_payload.get("usage") or payload.get("usage") or {}
            rec = _codex_rec(
                adapter_id, u, ts=ts, model=self.model,
                session_id=ev_payload.get("session_id") or ev_payload.get("thread_id") or self.session_id,
                task_id=ev_payload.get("response_id") or ev_payload.get("turn_id"),
                extra={"path": str(path), **tier},
            )
            if self.pending_fp is not None and fingerprint(rec) == self.pending_fp:
                self.pending_fp = None
                return  # already counted from last_token_usage
            records.append(rec)
            self.pending_fp = None


def parse_codex_rollout(path: Path, adapter_id: str) -> tuple[list, list[str]]:
    """Parse a Codex rollout .jsonl / .jsonl.zst. Dedupes unchanged totals and usage records."""
    from ..usage import is_zst_jsonl, iter_jsonl
    parser = CodexRolloutParser(path, adapter_id)
    notices: list[str] = []
    if is_zst_jsonl(path):
        for obj, notice in iter_jsonl(path):
            if notice and obj is None:
                notices.append(notice)
                continue
            if isinstance(obj, dict):
                parser.feed_obj(obj)
        return parser.records, notices
    from ..logcache import read_lines_from
    try:
        read_lines_from(path, 0, parser.feed)
    except OSError as exc:
        return [], [f"skipped {path}: {exc}"]
    return parser.records, notices
