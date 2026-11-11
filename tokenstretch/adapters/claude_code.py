"""Claude Code adapter. All Claude-specific paths, logs, and checks live here."""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..context import Context
from ..identity import STATE_DIRNAME
from ..files import count_lines_bytes, env_truthy, frontmatter, import_refs, read_json, walk_glob_names
from ..findings import Finding, UsageRecord
from ..util import is_loopback_url
from .base import Adapter, SetupItem, add_rules_block, rules_block, BaseUrl, ConfigLocation, DetectResult, HeadlessRun, HookPlan, InstallStep, InstructionFile, McpServer
CACHE_DISABLE = (
    "DISABLE_PROMPT_CACHING",
    "DISABLE_PROMPT_CACHING_HAIKU",
    "DISABLE_PROMPT_CACHING_SONNET",
    "DISABLE_PROMPT_CACHING_OPUS",
    "DISABLE_PROMPT_CACHING_FABLE",
)
CACHE_5M = ("FORCE_PROMPT_CACHING_5M", "CLAUDE_CODE_PROMPT_CACHE_TTL")
OPUS_HINTS = re.compile(r"opus|fable", re.I)
HEAVY_MODEL = re.compile(r"(opus|fable|claude-opus|claude-fable)", re.I)
MCP_TOOL = re.compile(r"^mcp__([^_]+(?:_[^_]+)*)__")


def _settings_paths(ctx: Context) -> list[Path]:
    return [
        ctx.home / ".claude" / "settings.json",
        ctx.root / ".claude" / "settings.json",
        ctx.root / ".claude" / "settings.local.json",
    ]


def _load_settings(ctx: Context) -> list[tuple[Path, dict]]:
    out = []
    for p in _settings_paths(ctx):
        data = read_json(p)
        if isinstance(data, dict) and "__error__" not in data:
            out.append((p, data))
    return out


def _merged_settings(ctx: Context) -> tuple[dict, dict[str, str]]:
    """Merge user < project < local. Return (merged, source_of_key)."""
    merged: dict = {}
    sources: dict[str, str] = {}
    order = [
        ctx.home / ".claude" / "settings.json",
        ctx.root / ".claude" / "settings.json",
        ctx.root / ".claude" / "settings.local.json",
    ]
    for p in order:
        data = read_json(p)
        if not isinstance(data, dict) or "__error__" in data:
            continue
        for k, v in data.items():
            if k == "env" and isinstance(v, dict) and isinstance(merged.get("env"), dict):
                merged["env"] = {**merged["env"], **v}
            elif k == "hooks" and isinstance(v, dict):
                hooks = dict(merged.get("hooks") or {})
                for ev, entries in v.items():
                    hooks.setdefault(ev, [])
                    if isinstance(entries, list):
                        hooks[ev] = list(hooks[ev]) + entries
                    else:
                        hooks[ev] = entries
                merged["hooks"] = hooks
            else:
                merged[k] = v
            sources[k] = str(p)
    return merged, sources


def _claude_json(ctx: Context) -> tuple[Path, dict]:
    p = ctx.home / ".claude.json"
    data = read_json(p)
    return p, data if isinstance(data, dict) and "__error__" not in data else {}


def _mcp_servers(ctx: Context) -> list[tuple[str, dict, str]]:
    """(name, entry, where) for every configured MCP server."""
    found: list[tuple[str, dict, str]] = []
    p, data = _claude_json(ctx)
    for name, entry in (data.get("mcpServers") or {}).items():
        if isinstance(entry, dict):
            found.append((name, entry, f"{p}:mcpServers.{name}"))
    for proj, pdata in (data.get("projects") or {}).items():
        if not isinstance(pdata, dict):
            continue
        for name, entry in (pdata.get("mcpServers") or {}).items():
            if isinstance(entry, dict):
                found.append((name, entry, f"{p}:projects.{proj}.mcpServers.{name}"))
    mcp = ctx.root / ".mcp.json"
    mdata = read_json(mcp)
    if isinstance(mdata, dict):
        for name, entry in (mdata.get("mcpServers") or {}).items():
            if isinstance(entry, dict):
                found.append((name, entry, f"{mcp}:{name}"))
    return found


def _collect_memory_files(ctx: Context) -> list[tuple[Path, int, int]]:
    """(path, lines, bytes) for every instruction file this tool would load."""
    seeds: list[Path] = [
        ctx.home / ".claude" / "CLAUDE.md",
        ctx.root / "CLAUDE.md",
        ctx.root / ".claude" / "CLAUDE.md",
    ]
    # Ancestors from project root toward home (nested instruction files).
    cur = ctx.root
    for _ in range(8):
        seeds.append(cur / "CLAUDE.md")
        if cur.parent == cur:
            break
        cur = cur.parent
    # Also walk a shallow tree under the project for nested copies.
    for p in walk_glob_names(ctx.root, "CLAUDE.md", max_depth=4):
        seeds.append(p)
    seen: set[Path] = set()
    out: list[tuple[Path, int, int]] = []

    def add(path: Path, remaining: int = 8):
        try:
            path = path.resolve()
        except OSError:
            return
        if path in seen or remaining < 0:
            return
        counted = count_lines_bytes(path)
        if counted is None:
            return
        seen.add(path)
        lines, size, text = counted
        out.append((path, lines, size))
        for ref in import_refs(text, path):
            add(ref, remaining - 1)

    for s in seeds:
        add(s)
    mem = ctx.root / "MEMORY.md"
    if not mem.is_file():
        mem = ctx.home / ".claude" / "MEMORY.md"
    if mem.is_file():
        counted = count_lines_bytes(mem)
        if counted:
            # Only the first 200 lines / 25 KB actually load.
            lines, size, text = counted
            loaded = "\n".join(text.splitlines()[:200])[: 25 * 1024]
            out.append((mem, loaded.count("\n") + (1 if loaded else 0), min(size, 25 * 1024)))
    return out


def _frontmatter_models(ctx: Context) -> list[tuple[Path, str]]:
    hits = []
    roots = [
        ctx.root / ".claude" / "skills",
        ctx.root / ".claude" / "commands",
        ctx.home / ".claude" / "skills",
        ctx.home / ".claude" / "commands",
        ctx.home / ".claude" / "plugins",
        ctx.root / ".claude" / "plugins",
    ]
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.md"):
            text = count_lines_bytes(path)
            if not text:
                continue
            fm = frontmatter(text[2])
            model = fm.get("model")
            if model:
                hits.append((path, model))
    return hits


def _hook_cmds(settings: dict) -> list[tuple[str, str]]:
    out = []
    hooks = settings.get("hooks") or {}
    if not isinstance(hooks, dict):
        return out
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for g in groups:
            if not isinstance(g, dict):
                continue
            for h in g.get("hooks") or []:
                if isinstance(h, dict) and h.get("command"):
                    out.append((str(h["command"]), f"hooks.{event}"))
            if g.get("command"):
                out.append((str(g["command"]), f"hooks.{event}"))
    return out


def _transcript_paths(ctx: Context, *, subagents: bool = True) -> list[Path]:
    """Main session files, plus subagent files at <project>/<session-id>/subagents/agent-*.jsonl.

    Subagent runs are real spend (the CLI's total_cost_usd includes their model usage).
    """
    root = ctx.home / ".claude" / "projects"
    if not root.is_dir():
        return []
    paths = list(root.glob("*/*.jsonl")) + list(root.glob("*.jsonl"))
    if subagents:
        paths += list(root.glob("*/*/subagents/*.jsonl"))
    return paths


def _is_subagent_file(path: Path) -> bool:
    return path.parent.name == "subagents"


def _usage_key(obj: dict) -> tuple[str, str] | None:
    """Dedupe key for one API response.

    Claude Code writes one JSONL line per content block of a single assistant message;
    every line repeats the same message.id and message.usage. Key on (message.id, requestId)
    when requestId is present, else message.id. Rows with no message.id are not deduped.
    """
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return None
    mid = msg.get("id")
    if not mid:
        return None
    return (str(mid), str(obj.get("requestId") or ""))


def _keep_newer(old_output: int, new_output: int) -> bool:
    """Duplicate-row rule: keep the row with the larger output_tokens; on a tie keep the later row.

    Input and cache fields are identical across a message's rows. Output is the only field a
    streaming update can change, and it only grows, so max-output is correct even if lines are
    out of order, and "last wins on tie" matches the final write.
    """
    return new_output >= old_output


def _dedupe_usage_records(records: list[UsageRecord]) -> list[UsageRecord]:
    """Collapse records sharing a usage key (also across files), keeping first-seen order."""
    out: list[UsageRecord] = []
    index: dict[tuple, int] = {}
    for rec in records:
        key = (rec.extra or {}).get("usage_key")
        if not key:
            out.append(rec)
            continue
        key = tuple(key)
        if key in index:
            i = index[key]
            if _keep_newer(out[i].output_tokens, rec.output_tokens):
                rec.is_subagent = rec.is_subagent or out[i].is_subagent
                out[i] = rec
            continue
        index[key] = len(out)
        out.append(rec)
    return out


def _iter_jsonl(path: Path):
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return


def _usage_from_obj(obj: dict) -> dict:
    u = obj.get("usage") or (obj.get("message") or {}).get("usage") or obj.get("message", {}).get("usage")
    if not isinstance(u, dict):
        return {}
    return {
        "input": int(u.get("input_tokens") or u.get("inputTokens") or 0),
        "output": int(u.get("output_tokens") or u.get("outputTokens") or 0),
        "cache_read": int(u.get("cache_read_input_tokens") or u.get("cacheReadTokens") or 0),
        "cache_write": int(u.get("cache_creation_input_tokens") or u.get("cacheCreationTokens") or u.get("cache_creation_tokens") or 0),
        "reasoning": int(u.get("reasoning_tokens") or u.get("reasoningTokens") or 0),
        "cost": u.get("cost_usd") or obj.get("total_cost_usd") or obj.get("totalCost"),
        "model": obj.get("model") or (obj.get("message") or {}).get("model"),
        "ts": obj.get("timestamp") or obj.get("ts"),
    }


class ClaudeCodeAdapter(Adapter):
    id = "claude-code"
    display_name = "Claude Code"
    tier = 1
    vendor_terms = "Anthropic"
    implemented_checks = frozenset({
        "W01", "W03", "W04", "W05", "W06", "W07", "W08", "W09", "W10",
        "W13", "W14", "W15", "W16", "T01", "P01", "P03",
        # v0.3: gaps found comparing with rsaryev/tokendoctor.
        "W19", "W20", "W21", "W22", "W23", "W24", "W25", "W26", "W27",
    })
    config_dir_env = ("CLAUDE_CONFIG_DIR",)
    headless_patterns = (r"\bclaude\b.*\s-p\b", r"\bclaude\b.*--print")

    def detect(self, ctx: Context) -> DetectResult:
        binary = ctx.which("claude")
        roots = [str(p) for p in (
            ctx.home / ".claude",
            ctx.home / ".claude.json",
            ctx.root / ".claude",
            ctx.root / "CLAUDE.md",
        ) if p.exists()]
        installed = bool(roots or binary)
        return DetectResult(
            installed=installed,
            binary=str(binary) if binary else None,
            config_roots=roots,
            auth_mode=self._auth_mode_public(ctx) if installed else "unknown",
        )

    def read_config(self, ctx: Context) -> dict:
        settings, sources = _merged_settings(ctx)
        envmap = self.settings_env(ctx)
        env = {**{k: v for k, (v, _) in envmap.items()}, **ctx.env}
        effective: dict = {}
        models = self.effective_models(ctx)
        if models:
            effective["model"] = {"value": models[0][0], "from": models[0][1]}
        if env.get("CLAUDE_CODE_SUBAGENT_MODEL"):
            effective["subagent_model"] = {"value": env["CLAUDE_CODE_SUBAGENT_MODEL"], "from": "env"}
        effort = env.get("CLAUDE_CODE_EFFORT_LEVEL") or settings.get("effortLevel")
        if effort:
            where = "CLAUDE_CODE_EFFORT_LEVEL" if env.get("CLAUDE_CODE_EFFORT_LEVEL") else sources.get("effortLevel", "settings")
            effective["effort"] = {"value": effort, "from": where}
        think = env.get("MAX_THINKING_TOKENS")
        if think is not None or settings.get("alwaysThinkingEnabled") is not None:
            effective["thinking_budget"] = {
                "value": think if think is not None else settings.get("alwaysThinkingEnabled"),
                "from": "MAX_THINKING_TOKENS" if think is not None else sources.get("alwaysThinkingEnabled", "settings"),
            }
        if "fastMode" in settings:
            effective["fast_mode"] = {"value": settings.get("fastMode"), "from": sources.get("fastMode", "settings")}
        urls = self.base_urls(ctx)
        if urls:
            effective["base_url"] = {"value": urls[0].url, "from": urls[0].where}
        ttl = settings.get("promptCacheTtl") or env.get("CLAUDE_CODE_PROMPT_CACHE_TTL")
        if ttl:
            effective["cache_ttl"] = {"value": ttl, "from": sources.get("promptCacheTtl", "env")}
        raw = []
        for path, data in _load_settings(ctx):
            name = path.name
            if path == ctx.home / ".claude" / "settings.json":
                scope = "user"
            elif name == "settings.local.json":
                scope = "local"
            else:
                scope = "project"
            raw.append({"file": str(path), "scope": scope, "data": data})
        return {"effective": effective, "raw": raw}

    def list_instruction_files(self, ctx: Context, cwd: str | None = None) -> list[InstructionFile]:
        out = []
        home_claude = ctx.home / ".claude"
        for path, lines, size in _collect_memory_files(ctx):
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if home_claude in resolved.parents or resolved.parent == home_claude:
                scope = "user"
            else:
                scope = "project"
            out.append(InstructionFile(
                path=str(path), scope=scope, bytes=size, lines=lines, always_loaded=True,
            ))
        return out

    def list_mcp_servers(self, ctx: Context) -> list[McpServer]:
        out = []
        for name, entry, where in _mcp_servers(ctx):
            typ = str(entry.get("type") or "")
            url = entry.get("url") or entry.get("serverUrl")
            if typ in {"http", "sse"}:
                transport = typ
            elif url:
                transport = typ or "http"
            else:
                transport = "stdio"
            scope = "project" if (".mcp.json" in where or "projects." in where) else "user"
            file = where.split(":")[0]
            out.append(McpServer(
                name=name, scope=scope, transport=transport, enabled=True,
                file=file, remote_url=str(url) if url else None,
            ))
        return out

    def read_usage_logs(self, ctx: Context, since_days: int = 30) -> list[UsageRecord]:
        import time
        from ..usage import record_in_window
        cutoff = time.time() - since_days * 86400
        self.last_log_notices = []
        records = []
        for path in self.default_log_paths(ctx):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            recs = self.parse_log(path)
            # Keep records whose ts is in the window; if a file has no ts, use mtime.
            kept = []
            for r in recs:
                if r.ts:
                    if record_in_window(r, cutoff, None):
                        kept.append(r)
                elif mtime >= cutoff:
                    kept.append(r)
            records.extend(kept)
        # parse_log dedupes within a file; also dedupe across files (sidechain rows can
        # appear in both a main session and its subagents/ file).
        return _dedupe_usage_records(records)

    def headless_env(self, home: Path) -> dict[str, str]:
        return {"HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude")}


    def config_locations(self, ctx: Context) -> list[ConfigLocation]:
        cred = ctx.home / ".claude" / ".credentials.json"
        locs = [
            ConfigLocation(cred, "credential", ctx.exists(cred), sensitive=True),
            ConfigLocation(ctx.home / ".claude.json", "settings", (ctx.home / ".claude.json").is_file()),
        ]
        for p in _settings_paths(ctx):
            locs.append(ConfigLocation(p, "settings", p.is_file()))
        locs.append(ConfigLocation(ctx.root / ".mcp.json", "mcp", (ctx.root / ".mcp.json").is_file()))
        for p, _, _ in _collect_memory_files(ctx):
            locs.append(ConfigLocation(p, "memory", True))
        return locs

    def settings_env(self, ctx: Context) -> dict[str, tuple[str, str]]:
        out = {}
        settings, sources = _merged_settings(ctx)
        env = settings.get("env") or {}
        if isinstance(env, dict):
            for k, v in env.items():
                out[str(k)] = (str(v), f"{sources.get('env', 'settings')}:env.{k}")
        return out

    def auth_mode(self, ctx: Context) -> str:
        if ctx.plan_override in {"subscription", "api"}:
            return "subscription" if ctx.plan_override == "subscription" else "api"
        settings, _ = _merged_settings(ctx)
        env = {**{k: v for k, (v, _) in self.settings_env(ctx).items()}, **ctx.env}
        if env.get("ANTHROPIC_API_KEY") or settings.get("apiKeyHelper") or env.get("apiKeyHelper"):
            return "api"
        if (settings.get("forceLoginMethod") or "") in {"console", "api"}:
            return "api"
        if env.get("CLAUDE_CODE_OAUTH_TOKEN") or ctx.exists(ctx.home / ".claude" / ".credentials.json"):
            return "subscription"
        return "unknown"

    def base_urls(self, ctx: Context) -> list[BaseUrl]:
        urls = []
        v, where = ctx.env_or_rc("ANTHROPIC_BASE_URL")
        if v:
            urls.append(BaseUrl(v, where, "ANTHROPIC_BASE_URL"))
        for k, (val, src) in self.settings_env(ctx).items():
            if k == "ANTHROPIC_BASE_URL" and val:
                urls.append(BaseUrl(val, src, k))
        return urls

    def hook_commands(self, ctx: Context) -> list[tuple[str, str]]:
        settings, _ = _merged_settings(ctx)
        return _hook_cmds(settings)

    def effective_models(self, ctx: Context) -> list[tuple[str, str]]:
        models = []
        v, where = ctx.env_or_rc("ANTHROPIC_MODEL")
        if v:
            models.append((v, where))
        settings, sources = _merged_settings(ctx)
        if settings.get("model"):
            models.append((str(settings["model"]), sources.get("model", "settings")))
        return models

    # ---- checks ------------------------------------------------------------------
    def checks(self, ctx: Context) -> list[Finding]:
        out: list[Finding] = []
        out += self._w01(ctx)
        out += self._w03(ctx)
        out += self._w04(ctx)
        out += self._w05(ctx)
        out += self._w06(ctx)
        out += self._w07(ctx)
        out += self._w08(ctx)
        out += self._w09(ctx)
        out += self._w10(ctx)
        out += self._w13(ctx)
        out += self._w14(ctx)
        out += self._w15(ctx)
        out += self._w16(ctx)
        out += self._w19(ctx)
        out += self._w20(ctx)
        out += self._w21(ctx)
        out += self._w22(ctx)
        out += self._w23(ctx)
        out += self._w24(ctx)
        out += self._w25(ctx)
        out += self._w26(ctx)
        out += self._w27(ctx)
        out += self._t01(ctx)
        return out

    def privacy_checks(self, ctx: Context) -> list[Finding]:
        return self._p01_first_party(ctx) + self._p03(ctx)

    def _w01(self, ctx: Context) -> list[Finding]:
        files = _collect_memory_files(ctx)
        if not files:
            return []
        findings = []
        total = sum(b for _, _, b in files)
        for path, lines, size in files:
            if path.name == "MEMORY.md":
                continue
            if lines > 200:
                findings.append(self.finding(
                    "W01", "claude-md-size", "med", "waste",
                    f"Your instruction file is read on every single message. It's {lines} lines; "
                    "Anthropic recommends under 200. The extra text costs you on every turn.",
                    "Move part-specific rules into path-scoped rules; exclude vendored instruction files.",
                    "CC-MEM, CC-COST, CC-SET",
                    file=str(path), key="lines", value=lines,
                ))
        if total > 25 * 1024:
            findings.append(self.finding(
                "W01", "claude-md-size", "high", "waste",
                f"Your instruction files total {total} bytes (over 25 KB) and are re-read on every message.",
                "Trim standing instructions and split workflow into skills.",
                "CC-MEM, CC-COST",
                file=",".join(str(p) for p, _, _ in files), key="bytes", value=total,
            ))
        return findings

    def _w03(self, ctx: Context) -> list[Finding]:
        servers = _mcp_servers(ctx)
        if not servers:
            return []
        names = {n for n, _, _ in servers}
        used: set[str] = set()
        transcripts = _transcript_paths(ctx)
        for p in transcripts:
            for obj in _iter_jsonl(p):
                msg = obj.get("message") or obj
                content = msg.get("content") if isinstance(msg, dict) else None
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    n = block.get("name") or ""
                    if n.startswith("mcp__"):
                        # mcp__<server>__<tool>
                        parts = n.split("__")
                        if len(parts) >= 3:
                            used.add(parts[1])
        unused = sorted(names - used)
        env = {**ctx.env, **{k: v for k, (v, _) in self.settings_env(ctx).items()}}
        search_off = (env.get("ENABLE_TOOL_SEARCH") or "").lower() == "false" or bool(env.get("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"))
        if not transcripts:
            return [self.finding(
                "W03", "mcp-unused", "info", "waste",
                f"{len(servers)} tool connections are configured, but there are no local transcripts "
                "to tell whether they have been used.",
                "After you work for a while, re-run doctor to see which connections sit idle.",
                "CC-MCP, CC-COST",
                file="; ".join(w for _, _, w in servers), key="servers", value=len(servers),
            )]
        if not unused:
            return []
        sev = "med" if search_off else "low"
        return [self.finding(
            "W03", "mcp-unused", sev, "waste",
            f"{len(unused)} tool connections are loaded but haven't been used in the local transcripts. "
            "Each one adds its instructions to every request.",
            "Remove unused servers, or disable them in settings. Make the change between sessions, "
            "since connecting or removing a server with tool search off breaks the cache.",
            "CC-MCP, CC-SET, CC-CACHE, CC-COST",
            file="; ".join(w for n, _, w in servers if n in unused), key="unused", value=unused,
        )]

    def _w04(self, ctx: Context) -> list[Finding]:
        env = {**ctx.env, **{k: v for k, (v, _) in self.settings_env(ctx).items()}}
        off = (env.get("ENABLE_TOOL_SEARCH") or "").lower() == "false"
        betas = bool(env.get("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"))
        if not (off or betas) or not _mcp_servers(ctx):
            return []
        return [self.finding(
            "W04", "tool-search-off", "med", "waste",
            "A setting forces every tool description into every message. Turning it back on is free savings.",
            "Unset ENABLE_TOOL_SEARCH and CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS.",
            "CC-ENV",
            env="ENABLE_TOOL_SEARCH" if off else "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
            key="ENABLE_TOOL_SEARCH" if off else "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS",
            value=env.get("ENABLE_TOOL_SEARCH") if off else env.get("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"),
        )]

    def _w05(self, ctx: Context) -> list[Finding]:
        models = self.effective_models(ctx)
        if not models:
            return []
        model, where = models[0]  # env beats settings because we listed env first
        if not HEAVY_MODEL.search(model):
            return []
        plan = self.auth_mode(ctx)
        sev = "med" if plan in {"subscription", "unknown"} else "low"
        return [self.finding(
            "W05", "default-model-heavy", sev, "waste",
            "You're running the most expensive model for everything, including small edits. "
            "Switching the everyday default stretches your plan; keep the big model for hard problems.",
            "Set the default to the mid-tier model and switch up only for hard tasks.",
            "CC-MODEL, CC-COST, CC-SET",
            file=where, key="model", value=model,
        )]

    def _w06(self, ctx: Context) -> list[Finding]:
        settings, sources = _merged_settings(ctx)
        env = {**ctx.env, **{k: v for k, (v, _) in self.settings_env(ctx).items()}}
        effort = env.get("CLAUDE_CODE_EFFORT_LEVEL") or settings.get("effortLevel") or ""
        where = "CLAUDE_CODE_EFFORT_LEVEL" if env.get("CLAUDE_CODE_EFFORT_LEVEL") else sources.get("effortLevel", "")
        thinking = settings.get("alwaysThinkingEnabled") is True
        max_tok = env.get("MAX_THINKING_TOKENS") or settings.get("MAX_THINKING_TOKENS")
        ceiling = str(effort).lower() in {"max", "xhigh"}
        large_budget = False
        try:
            large_budget = int(max_tok) >= 20000
        except (TypeError, ValueError):
            large_budget = bool(max_tok)
        if not (ceiling or thinking or large_budget):
            return []
        return [self.finding(
            "W06", "effort-max", "med", "waste",
            "Your AI is set to think as hard as possible on every message. That burns your allowance fastest. "
            "'High' or 'medium' is plenty for everyday work.",
            "Set effort to high or medium and raise it only per task. Change it at session start, "
            "because changing effort mid-session breaks the cache.",
            "CC-SET, CC-COST, CC-CACHE",
            file=where, key="effortLevel", value=effort or f"alwaysThinkingEnabled={thinking}",
        )]

    def _w07(self, ctx: Context) -> list[Finding]:
        settings, sources = _merged_settings(ctx)
        if settings.get("fastMode") is True and settings.get("fastModePerSessionOptIn") in {None, False}:
            return [self.finding(
                "W07", "fast-mode-on", "high", "waste",
                "'Fast mode' is on by default. It's billed as extra (Claude) or uses up to 8× your allowance "
                "(other tools). Turn it on only when you're in a hurry.",
                "Set fastMode to false and fastModePerSessionOptIn to true. If you need it, turn it on at session start "
                "(the first toggle pays the full uncached context).",
                "CC-FAST, CC-SET",
                file=sources.get("fastMode", "settings"), key="fastMode", value=True,
            )]
        return []

    def _w08(self, ctx: Context) -> list[Finding]:
        findings = []
        settings, sources = _merged_settings(ctx)
        if str(settings.get("model") or "").lower() == "opusplan":
            findings.append(self.finding(
                "W08", "cache-model-frontmatter", "med", "waste",
                "A default of opusplan switches models mid-conversation in long sessions. "
                "That throws away your saved context discount, and the next message re-pays for the whole chat.",
                "Avoid opusplan as the standing default in long sessions.",
                "CC-CACHE, CC-MODEL",
                file=sources.get("model", "settings"), key="model", value="opusplan",
            ))
        used_cmds = 0
        for path, model in _frontmatter_models(ctx):
            findings.append(self.finding(
                "W08", "cache-model-frontmatter", "med", "waste",
                "A shortcut you use switches the AI model mid-conversation. That throws away your saved "
                "context discount, and the next message re-pays for the whole chat.",
                "Remove the model field from that shortcut's frontmatter (or move the work into a subagent "
                "with its own model, which has a separate context).",
                "CC-CACHE, CC-SKILL, CC-MODEL",
                file=str(path), key="model", value=model,
            ))
            used_cmds += 1
        return findings

    def _w09(self, ctx: Context) -> list[Finding]:
        env = {**ctx.env, **{k: v for k, (v, _) in self.settings_env(ctx).items()}}
        findings = []
        for name in CACHE_DISABLE:
            v, where = ctx.env_or_rc(name)
            if not v:
                v, where = env.get(name), f"settings.env.{name}" if name in env else ""
            if v:
                findings.append(self.finding(
                    "W09", "cache-disabled", "high", "waste",
                    "A setting turns off the discount for re-reading your conversation. "
                    "Every message pays full price for everything before it.",
                    f"Remove {name}. Verify in the usage view with the prompt-cache line.",
                    "CC-CACHE, CC-ENV, CC-SET",
                    env=name, key=name, value=v, file=where,
                ))
        settings, sources = _merged_settings(ctx)
        five = env.get("FORCE_PROMPT_CACHING_5M") or env.get("CLAUDE_CODE_PROMPT_CACHE_TTL") in {"5m", "5M"}
        ttl = settings.get("promptCacheTtl") or settings.get("subagentPromptCacheTtl")
        if five or (isinstance(ttl, str) and ttl.lower() in {"5m", "5min", "300", "300s"}):
            findings.append(self.finding(
                "W09", "cache-disabled", "med", "waste",
                "The conversation cache lifetime has been shortened to about five minutes. "
                "On a subscription the usual main-conversation default is one hour.",
                "Remove FORCE_PROMPT_CACHING_5M / CLAUDE_CODE_PROMPT_CACHE_TTL and any 5m promptCacheTtl.",
                "CC-CACHE, CC-ENV, CC-SET",
                key="promptCacheTtl", value=ttl or "5m",
            ))
        return findings

    def _w10(self, ctx: Context) -> list[Finding]:
        findings = []
        for b in self.base_urls(ctx):
            if is_loopback_url(b.url) or "127.0.0.1" in b.url or "localhost" in b.url:
                findings.append(self.finding(
                    "W10", "chained-proxies", "med" if self.auth_mode(ctx) == "api" else "high", "waste",
                    f"Your AI traffic is going through a program on your computer first ({b.url}). "
                    "Chained middlemen can strip the caching discount and double-process your messages.",
                    "Keep at most one proxy, and only with an API key. Remove the base URL for subscription use.",
                    "CC-ENV, CC-CACHE",
                    file=b.where, key=b.key, value=b.url,
                ))
        # Undocumented first-party assumption written by some proxy CLIs.
        env = {**ctx.env, **{k: v for k, (v, _) in self.settings_env(ctx).items()}}
        if env.get("_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL"):
            findings.append(self.finding(
                "W10", "chained-proxies", "high", "waste",
                "A setting tells Claude Code to treat a custom base URL as first-party. "
                "That is written by a third-party proxy CLI and hides the fact that traffic is being intercepted.",
                "Remove _CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL and the custom base URL.",
                "CC-ENV, tool SECURITY.md",
                key="_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL",
                value=env.get("_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL"),
            ))
        return findings

    def _w13(self, ctx: Context) -> list[Finding]:
        env = {**ctx.env, **{k: v for k, (v, _) in self.settings_env(ctx).items()}}
        findings = []
        if env.get("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS") in {"1", "true", "True"}:
            findings.append(self.finding(
                "W13", "subagent-share", "info", "waste",
                "Agent teams are enabled. Official docs say they use significantly more tokens than a standard session.",
                "Disable agent teams unless you need them. Point helper agents at a cheaper model.",
                "CC-COST, CC-TEAM",
                env="CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", key="CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", value="1",
            ))
        side = main = 0
        recs: list[UsageRecord] = []
        for p in _transcript_paths(ctx):
            recs.extend(self.parse_log(p))
        for r in _dedupe_usage_records(recs):
            tok = r.input_tokens + r.output_tokens + r.cache_read_tokens + r.cache_write_tokens
            if r.is_subagent:
                side += tok
            else:
                main += tok
        total = side + main
        if total and side / total > 0.40:
            pct = round(100 * side / total)
            findings.append(self.finding(
                "W13", "subagent-share", "med", "waste",
                f"{pct}% of your usage is helper agents working in the background. Each one loads your "
                "instructions and tools again. Point them at a cheaper model or use fewer.",
                "Set a cheaper model for helper agents, keep spawn prompts focused, and disable agent teams unless needed.",
                "CC-COST, CC-TEAM, CC-SUB, CC-CACHE",
                key="subagent_share_pct", value=pct,
            ))
        return findings

    def _w14(self, ctx: Context) -> list[Finding]:
        peaks = []
        # Peak context per main session. Repeated rows per message.id don't change a max.
        for p in _transcript_paths(ctx, subagents=False):
            peak = turns = 0
            for obj in _iter_jsonl(p):
                u = _usage_from_obj(obj)
                ctx_tok = u.get("input", 0) + u.get("cache_read", 0) + u.get("cache_write", 0)
                peak = max(peak, ctx_tok)
                if obj.get("type") in {"user", "human"} or (obj.get("message") or {}).get("role") == "user":
                    turns += 1
            if peak or turns:
                peaks.append((p, peak, turns))
        if not peaks:
            return []
        peaks.sort(key=lambda x: x[1])
        mid = peaks[len(peaks) // 2][1]
        if mid > 150_000:
            return [self.finding(
                "W14", "long-sessions", "med", "waste",
                "You're keeping one giant conversation going. Every new message re-sends all of it. "
                "Start fresh when you switch tasks.",
                "Clear the conversation between tasks (rename it first so you can find it later). "
                "Compact with focus instructions before stepping away.",
                "CC-COST, CC-SET",
                file=str(peaks[-1][0]), key="median_context_tokens", value=mid,
            )]
        return []

    def _w15(self, ctx: Context) -> list[Finding]:
        settings, sources = _merged_settings(ctx)
        cap = settings.get("bashOutputMaxChars")
        has_rtk = any("rtk" in cmd.lower() for cmd, _ in self.hook_commands(ctx))
        if has_rtk:
            return []
        big = False
        for p in _transcript_paths(ctx):
            for obj in _iter_jsonl(p):
                content = ((obj.get("message") or {}).get("content") or obj.get("content") or [])
                if not isinstance(content, list):
                    if isinstance(obj.get("tool_result"), str) and len(obj["tool_result"]) > 20_000:
                        big = True
                    continue
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("content") or block.get("text") or ""
                        if isinstance(text, str) and len(text) > 20_000:
                            big = True
        if cap is not None and not big:
            try:
                if int(cap) <= 20_000:
                    return []
            except (TypeError, ValueError):
                pass
        if cap is None or big or (isinstance(cap, int) and cap > 20_000):
            return [self.finding(
                "W15", "big-bash-output", "low", "waste",
                "Long command logs are pasted into the AI's memory in full.",
                "Install the local command-output hook from the Clean profile, or lower the command-output size cap.",
                "CC-SET",
                file=sources.get("bashOutputMaxChars", ""), key="bashOutputMaxChars", value=cap,
            )]
        return []

    def _w16(self, ctx: Context) -> list[Finding]:
        settings, sources = _merged_settings(ctx)
        if settings.get("autoContinueAtUsageLimit") is True:
            return [self.finding(
                "W16", "auto-continue", "info", "waste",
                "When you hit your limit, Claude keeps going automatically. Check that it isn't spending extra credits.",
                "Set autoContinueAtUsageLimit to false unless you want it.",
                "CC-SET",
                file=sources.get("autoContinueAtUsageLimit", "settings"), key="autoContinueAtUsageLimit", value=True,
            )]
        return []

    # ---- v0.3 checks (gaps vs rsaryev/tokendoctor; re-implemented, no code copied) ----------
    # Every key and range below was read in code.claude.com docs on 2026-11-10
    # (settings-reference.md, env-vars.md, settings.md, sub-agents.md, skills.md).
    def _env_value(self, ctx: Context, name: str) -> tuple[str | None, str]:
        v, where = ctx.env_or_rc(name)
        if v is None:
            sv = self.settings_env(ctx).get(name)
            if sv:
                v, where = sv
        return v, where

    def _w19(self, ctx: Context) -> list[Finding]:
        out = []
        for p in _settings_paths(ctx):
            data = read_json(p)
            if isinstance(data, dict) and "__error__" in data:
                out.append(self.finding(
                    "W19", "settings-unreadable", "med", "waste",
                    "A Claude Code settings file isn't valid JSON, so Claude Code skips it and none of the "
                    "savings settings in it apply. Doctor couldn't check it either.",
                    "Fix the JSON (no comments, no trailing commas). `claude doctor` shows what Claude Code rejected.",
                    "CC-SET (settings.md#fix-a-broken-settings-file)",
                    file=str(p), key="json_error", value=data["__error__"][:120],
                ))
        return out

    def _w20(self, ctx: Context) -> list[Finding]:
        hits = []
        for name in ("DISABLE_AUTO_COMPACT", "DISABLE_COMPACT"):
            v, where = self._env_value(ctx, name)
            if env_truthy(v):
                hits.append((name, v, where))
        for p, data in _load_settings(ctx):
            if data.get("autoCompactEnabled") is False:
                hits.append(("autoCompactEnabled", False, str(p)))
        if not hits:
            return []
        key, value, where = hits[0]
        return [self.finding(
            "W20", "auto-compact-off", "med", "waste",
            "Automatic compaction is off, so a long conversation keeps re-sending its whole history "
            "until the model's window fills. Idle compaction is off with it.",
            "Remove DISABLE_AUTO_COMPACT / DISABLE_COMPACT and autoCompactEnabled: false. /compact still works by hand.",
            "CC-ENV, CC-SET",
            file=where, key=key, value=value,
        )]

    def _w21(self, ctx: Context) -> list[Finding]:
        for p, data in _load_settings(ctx):
            if data.get("idleCompaction") is False:
                return [self.finding(
                    "W21", "idle-compaction-off", "info", "waste",
                    "Idle compaction is off. A long conversation you step away from isn't summarized before "
                    "its one-hour cache expires, so your next message re-reads it all uncached.",
                    "Remove idleCompaction: false unless you need it.",
                    "CC-SET (#idlecompaction)",
                    file=str(p), key="idleCompaction", value=False,
                )]
        return []

    def _w22(self, ctx: Context) -> list[Finding]:
        problems: list[tuple[str, object, str]] = []

        def window_ok(raw) -> bool:
            text = str(raw).strip().lower()
            mult = 1000 if text.endswith("k") else 1
            text = text[:-1] if mult == 1000 else text
            try:
                n = float(text) * mult
            except ValueError:
                return False
            return 100_000 <= n <= 1_000_000

        for p, data in _load_settings(ctx):
            if "autoCompactThreshold" in data:
                problems.append(("autoCompactThreshold", data["autoCompactThreshold"], str(p)))
            vals = [("autoCompactWindow", data.get("autoCompactWindow"))]
            ms = data.get("modelSettings")
            if isinstance(ms, dict):
                vals += [(f"modelSettings.{m}.autoCompactWindow", v.get("autoCompactWindow"))
                         for m, v in ms.items() if isinstance(v, dict)]
            for key, v in vals:
                if v is not None and not (isinstance(v, (int, float)) and not isinstance(v, bool)
                                          and 100_000 <= v <= 1_000_000):
                    problems.append((key, v, str(p)))
        v, where = self._env_value(ctx, "CLAUDE_CODE_AUTO_COMPACT_WINDOW")
        if v and not window_ok(v):
            problems.append(("CLAUDE_CODE_AUTO_COMPACT_WINDOW", v, where))
        v, where = self._env_value(ctx, "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE")
        if v and not (v.strip().isdigit() and 1 <= int(v.strip()) <= 100):
            problems.append(("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", v, where))
        return [self.finding(
            "W22", "compact-config-invalid", "low", "waste",
            "A compaction setting doesn't do what it looks like: the key isn't documented, or the value is "
            "outside the documented range, so Claude Code ignores or clamps it.",
            "Use autoCompactWindow (100000 to 1000000 tokens) or CLAUDE_CODE_AUTO_COMPACT_WINDOW (a bare number "
            "is tokens, not thousands); CLAUDE_AUTOCOMPACT_PCT_OVERRIDE takes a whole number 1-100.",
            "CC-SET (#autocompactwindow), CC-ENV",
            file=where_, key=key, value=value,
        ) for key, value, where_ in problems]

    def _agent_files(self, ctx: Context) -> list[Path]:
        out = []
        for d in (ctx.home / ".claude" / "agents", ctx.root / ".claude" / "agents"):
            if d.is_dir():
                out += sorted(d.rglob("*.md"))
        return out

    def _w23(self, ctx: Context) -> list[Finding]:
        models = self.effective_models(ctx)
        if not models or not HEAVY_MODEL.search(models[0][0]):
            return []
        sub, _ = self._env_value(ctx, "CLAUDE_CODE_SUBAGENT_MODEL")
        if sub and sub.strip().lower() != "inherit":
            return []
        inheriting = []
        for path in self._agent_files(ctx):
            counted = count_lines_bytes(path)
            if not counted:
                continue
            model = frontmatter(counted[2]).get("model", "").strip().lower()
            if model in {"", "inherit"}:
                inheriting.append(path)
        if not inheriting:
            return []
        return [self.finding(
            "W23", "agents-inherit-heavy-model", "info", "waste",
            f"{len(inheriting)} of your custom helper agents name no model, so they run on your expensive "
            f"main model ({models[0][0]}).",
            "Add `model: haiku` or `model: sonnet` to agents that only search, run commands or summarize; "
            "leave reasoning-heavy agents as they are. Or set CLAUDE_CODE_SUBAGENT_MODEL.",
            "CC-SUB (#choose-a-model)",
            file="; ".join(str(p) for p in inheriting[:8]), key="model", value="inherit",
        )]

    def _w24(self, ctx: Context) -> list[Finding]:
        v, where = self._env_value(ctx, "MAX_MCP_OUTPUT_TOKENS")
        try:
            n = int(str(v).strip())
        except (TypeError, ValueError):
            return []
        if n <= 25_000:
            return []
        return [self.finding(
            "W24", "mcp-output-limit-raised", "info", "waste",
            f"Tool-connection output is allowed up to {n} tokens (default 25,000). One big result stays in "
            "the conversation and is re-sent with every later message.",
            "Unset MAX_MCP_OUTPUT_TOKENS, or raise it only for the server that needs it.",
            "CC-ENV",
            env="MAX_MCP_OUTPUT_TOKENS", file=where, key="MAX_MCP_OUTPUT_TOKENS", value=n,
        )]

    def _skills(self, ctx: Context) -> dict[str, list[tuple[str, bool]]]:
        """scope -> [(skill name, listed for the model)] from <dir>/<name>/SKILL.md frontmatter."""
        out: dict[str, list[tuple[str, bool]]] = {}
        for scope, d in (("user", ctx.home / ".claude" / "skills"), ("project", ctx.root / ".claude" / "skills")):
            rows = []
            if d.is_dir():
                for sk in sorted(d.iterdir()):
                    counted = count_lines_bytes(sk / "SKILL.md") if sk.is_dir() else None
                    if not counted:
                        continue
                    off = frontmatter(counted[2]).get("disable-model-invocation", "").lower() in {"true", "yes", "on", "1"}
                    rows.append((sk.name, not off))
            out[scope] = rows
        return out

    SKILL_LISTING_MAX = 15  # [reasoning] starting threshold; bench should calibrate it.

    def _w25(self, ctx: Context) -> list[Finding]:
        listed = [n for rows in self._skills(ctx).values() for n, on in rows if on]
        if len(listed) <= self.SKILL_LISTING_MAX:
            return []
        return [self.finding(
            "W25", "skill-listing-large", "info", "waste",
            f"{len(listed)} skills put their descriptions into every message, whether or not they're used.",
            "Add `disable-model-invocation: true` to skills you only run by hand, or hide them with skillOverrides.",
            "CC-SKILL",
            key="auto_listed_skills", value=len(listed),
        )]

    def _w26(self, ctx: Context) -> list[Finding]:
        sk = self._skills(ctx)
        dup = sorted({n for n, _ in sk.get("user", [])} & {n for n, _ in sk.get("project", [])})
        if not dup:
            return []
        return [self.finding(
            "W26", "duplicate-skills", "info", "waste",
            "The same skill is installed for you and for this project. Keep one copy.",
            "Delete the copy you don't want (user ~/.claude/skills or project .claude/skills).",
            "CC-SKILL",
            key="skills", value=dup,
        )]

    LOCKFILES = ("bun.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "Cargo.lock", "poetry.lock", "uv.lock")
    LOCKFILE_BYTES = 200_000  # [reasoning] threshold

    def _w27(self, ctx: Context) -> list[Finding]:
        deny = []
        for _, data in _load_settings(ctx):
            perms = data.get("permissions")
            if isinstance(perms, dict) and isinstance(perms.get("deny"), list):
                deny += [str(r) for r in perms["deny"]]
        hits = []
        for name in self.LOCKFILES:
            st = ctx.file_stat(ctx.root / name)  # stat only; the lockfile is never opened
            if st is None:
                continue
            size = st.st_size
            if size >= self.LOCKFILE_BYTES and not any(name in r for r in deny):
                hits.append((name, size))
        if not hits:
            return []
        return [self.finding(
            "W27", "large-lockfile-readable", "info", "waste",
            "Large lockfiles in this project can be read in full by the AI, costing tens of thousands of "
            "tokens for content it could search instead.",
            "Add a permissions.deny Read rule for them (the Clean profile's read gate does this). "
            "Deny rules don't stop reads through the shell.",
            "CC-SET (permissions)",
            file=", ".join(str(ctx.root / n) for n, _ in hits), key="bytes", value={n: b for n, b in hits},
        )]

    def _t01(self, ctx: Context) -> list[Finding]:
        mode = self.auth_mode(ctx)
        proxies = [b for b in self.base_urls(ctx) if is_loopback_url(b.url) or "localhost" in b.url or "127.0.0.1" in b.url]
        env = {**ctx.env, **{k: v for k, (v, _) in self.settings_env(ctx).items()}}
        assume = bool(env.get("_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL"))
        if not proxies and not assume:
            return []
        if mode == "api":
            return []
        return [self.finding(
            "T01", "tos-oauth-proxy", "high", "tos",
            "Your Claude plan login is passing through a third-party program. Anthropic's terms say "
            "outside tools may not intermediate your Claude.ai credentials. This could get your account flagged.",
            "Remove the base URL, or switch that tool to an API key.",
            "CC-LEGAL",
            file=proxies[0].where if proxies else "settings.env",
            key="_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL" if assume else "ANTHROPIC_BASE_URL",
            value=proxies[0].url if proxies else "set",
        )]

    def _p01_first_party(self, ctx: Context) -> list[Finding]:
        flags = ("DISABLE_TELEMETRY", "DO_NOT_TRACK", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                 "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY")
        present = {n: (ctx.env_or_rc(n)[0] or self.settings_env(ctx).get(n, (None, ""))[0]) for n in flags}
        return [self.finding(
            "P01", "telemetry-on", "info", "privacy",
            "Claude Code itself may send first-party usage/feedback traffic unless you opt out. "
            "This is the vendor's own telemetry; you can choose to disable it.",
            "Set DISABLE_TELEMETRY, DO_NOT_TRACK, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC, and/or "
            "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY if you want it off.",
            "CC-DATA",
            key="first_party_opt_outs", value={k: (v or "unset") for k, v in present.items()},
        )]

    def _p03(self, ctx: Context) -> list[Finding]:
        settings, sources = _merged_settings(ctx)
        days = settings.get("cleanupPeriodDays", 30)
        proj = ctx.home / ".claude" / "projects"
        if not proj.is_dir() and days is None:
            return []
        return [self.finding(
            "P03", "local-plaintext-retention", "info", "privacy",
            f"Your AI chats are saved in plain text on this computer for {days} days.",
            "Lower cleanupPeriodDays if you want transcripts kept for less time.",
            "CC-DATA, CC-SET",
            file=sources.get("cleanupPeriodDays", str(proj)), key="cleanupPeriodDays", value=days,
        )]

    # ---- logs --------------------------------------------------------------------
    def default_log_paths(self, ctx: Context) -> list[Path]:
        return _transcript_paths(ctx)

    def sniff_log(self, path: Path, first_lines: list[str]) -> bool:
        blob = "\n".join(first_lines)
        return "cache_creation_input_tokens" in blob or '"type": "assistant"' in blob or '"type":"assistant"' in blob

    def parse_log(self, path: Path) -> list[UsageRecord]:
        """Per-assistant-message records. Claude input is already uncached (separate cache fields)."""
        from ..usage import normalize_usage, parse_ts, record_from_normalized
        sub_file = _is_subagent_file(path)
        # subagents/agent-*.jsonl lives under <project>/<session-id>/; bill it to that session.
        session_id = path.parent.parent.name if sub_file else path.stem
        records: list[UsageRecord] = []
        index: dict[tuple[str, str], int] = {}
        turns = 0
        for obj in _iter_jsonl(path):
            if obj.get("type") in {"user", "human"} or (obj.get("message") or {}).get("role") == "user":
                turns += 1
            if obj.get("type") == "result":
                # Session totals would double-count per-message usage; skip when we already have records.
                if records:
                    continue
                u = _usage_from_obj(obj)
                if not u:
                    continue
                norm = normalize_usage(
                    input_tokens=u.get("input", 0), cache_read=u.get("cache_read", 0),
                    cache_write=u.get("cache_write", 0), output=u.get("output", 0),
                    reasoning=u.get("reasoning", 0), input_includes_cache=False,
                )
                cost = obj.get("total_cost_usd") if obj.get("total_cost_usd") is not None else u.get("cost")
                records.append(record_from_normalized(
                    self.id, "log", norm, session_id=session_id, model=u.get("model"),
                    ts=parse_ts(u.get("ts")), cost_usd=float(cost) if cost is not None else None,
                    success=obj.get("subtype") != "error", turns=turns,
                    extra={"path": str(path)},
                ))
                continue
            u = _usage_from_obj(obj)
            if not u or not any(u.get(k) for k in ("input", "output", "cache_read", "cache_write", "reasoning")):
                continue
            norm = normalize_usage(
                input_tokens=u.get("input", 0), cache_read=u.get("cache_read", 0),
                cache_write=u.get("cache_write", 0), output=u.get("output", 0),
                reasoning=u.get("reasoning", 0), input_includes_cache=False,
            )
            cost = u.get("cost")
            key = _usage_key(obj)
            extra = {"path": str(path)}
            if obj.get("cwd"):
                extra["cwd"] = str(obj.get("cwd"))
            if key:
                extra["usage_key"] = list(key)
            rec = record_from_normalized(
                self.id, "log", norm, session_id=str(obj.get("sessionId") or session_id),
                task_id=str((obj.get("message") or {}).get("id") or obj.get("uuid") or obj.get("id") or ""),
                model=u.get("model"), ts=parse_ts(u.get("ts") or obj.get("timestamp")),
                is_subagent=sub_file or bool(obj.get("isSidechain") or obj.get("is_sidechain")),
                cost_usd=float(cost) if cost is not None else None,
                turns=1, extra=extra,
            )
            if key and key in index:
                i = index[key]
                if _keep_newer(records[i].output_tokens, rec.output_tokens):
                    records[i] = rec
                continue
            if key:
                index[key] = len(records)
            records.append(rec)
        return records

    def apply_model_flag(self, argv: list[str], model: str) -> list[str]:
        out = list(argv)
        out.extend(["--model", model])
        return out

    def headless_run(self, prompt: str, cwd: Path, model: str | None = None) -> HeadlessRun | None:
        argv = ["claude", "--bare", "-p", prompt, "--output-format", "json"]
        if model:
            argv = self.apply_model_flag(argv, model)
        return HeadlessRun(argv, cwd)

    def parse_headless_output(self, stdout: str, task_id: str) -> UsageRecord | None:
        try:
            obj = json.loads(stdout.strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            return None
        u = _usage_from_obj(obj)
        if not u and "usage" not in obj:
            return None
        from ..usage import normalize_usage, record_from_normalized
        norm = normalize_usage(
            input_tokens=u.get("input", 0), cache_read=u.get("cache_read", 0),
            cache_write=u.get("cache_write", 0), output=u.get("output", 0),
            reasoning=u.get("reasoning", 0), input_includes_cache=False,
        )
        cost = obj.get("total_cost_usd")
        return record_from_normalized(
            self.id, "headless", norm, task_id=task_id, model=u.get("model"),
            cost_usd=float(cost) if cost is not None else None,
        )



    def caveman_skill_dir(self, ctx: Context) -> Path:
        return ctx.home / ".claude" / "skills" / "caveman"

    # ---- setup / status (plans only; the core writes with backups) ---------------------
    MID_MODEL = "sonnet"
    TOP_MODEL = "opus"

    def setup_items(self, ctx: Context) -> list[SetupItem]:
        items: list[SetupItem] = []
        item = self._setup_model(ctx)
        if item:
            items.append(item)
        item = self._setup_rules(ctx)
        if item:
            items.append(item)
        return items

    def _setup_model(self, ctx: Context) -> SetupItem | None:
        current, where = None, None
        target = ctx.home / ".claude" / "settings.json"
        for path in _settings_paths(ctx):
            data = read_json(path)
            if isinstance(data, dict) and "__error__" not in data and data.get("model"):
                current, where, target = str(data["model"]), path, path
        if current and not HEAVY_MODEL.search(current):
            return None  # already on a mid/small model; leave it alone
        raw = read_json(target) if target.is_file() else {}
        if not isinstance(raw, dict) or "__error__" in raw:
            return None  # unreadable JSON: doctor W19 reports it; never overwrite
        data = dict(raw)
        data["model"] = self.MID_MODEL
        notes = [
            f"For hard tracing or design work, type `/model {self.TOP_MODEL}` in Claude Code "
            f"(or run `claude --model {self.TOP_MODEL}`), then `/model {self.MID_MODEL}` when done.",
        ]
        env_model, env_where = ctx.env_or_rc("ANTHROPIC_MODEL")
        if env_model and HEAVY_MODEL.search(env_model):
            notes.append(f"ANTHROPIC_MODEL={env_model} in {env_where} overrides this setting. "
                         "Remove it yourself; shell files are never edited.")
        why = (
            f"Your default is {current} (from {where}). " if current else
            "No default model is set, so Claude Code picks one by plan, which is often the top model. "
        ) + ("The top model costs several times more per step. In a real bench on a large repo, "
             "switching to the mid-tier model was nearly all of a ~90% drop in Claude-reported cost.")
        return SetupItem(
            id="claude-default-model", adapter_id=self.id,
            title=f"Make {self.MID_MODEL} the default Claude Code model",
            why=why,
            changes=[{
                "path": str(target), "kind": "modify" if target.is_file() else "create",
                "content": _dump_json(data), "key": "model",
                "reason": f"model: {current or '(unset)'} -> {self.MID_MODEL}",
            }],
            done=f"switched default model to {self.MID_MODEL}",
            notes=notes,
        )

    def _setup_rules(self, ctx: Context) -> SetupItem | None:
        if ctx.root.resolve() == ctx.home.resolve():
            return None  # no project folder; never write ~/CLAUDE.md
        path = ctx.root / "CLAUDE.md"
        existing = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        new = add_rules_block(existing, rules_block("Sonnet", "/clear (or open a new chat)"))
        if new is None:
            return None
        return SetupItem(
            id="claude-rules-block", adapter_id=self.id,
            title="Add 5 short credit-saving rules to the project CLAUDE.md",
            why="Long sessions re-read their whole history on every step, and broad file reads fill it fast. "
                "These rules keep each task fresh, reads narrow, and replies short.",
            changes=[{
                "path": str(path), "kind": "modify" if path.is_file() else "create",
                "content": new, "key": "credit-rules block",
                "reason": "append a marked rules block (about 0.7 KB)",
            }],
            done="added credit-saving rules to CLAUDE.md",
        )

    def live_sessions(self, ctx: Context, limit: int = 5) -> list[dict]:
        """Newest main sessions, read from the tail of each log. Logs only; never credentials."""
        paths = []
        for p in _transcript_paths(ctx, subagents=False):
            try:
                paths.append((p.stat().st_mtime, p))
            except OSError:
                continue
        out = []
        for mtime, path in sorted(paths, reverse=True)[:limit]:
            last = _last_assistant_usage(path)
            if not last:
                continue
            model, usage = last
            history = sum(int(usage.get(k) or 0) for k in
                          ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            out.append({
                "adapter": self.id, "session": path.stem[:8], "project": path.parent.name,
                "model": model, "history_tokens": history, "last_write": mtime,
                "size_bytes": path.stat().st_size, "path": str(path),
                "clear_hint": "/clear",
            })
        return out

    HOOK_EVENTS = {
        "pre_tool_shell_filter": ("PreToolUse", "Bash"),
        "pre_read_gate": ("PreToolUse", "Read"),
        "session_start_note": ("SessionStart", None),
    }

    def install_hooks(self, ctx: Context, plan: HookPlan | dict | None = None, *, dry_run: bool = True) -> dict:
        """Plan hook and native-deny file edits. Never writes; the installer applies changes."""
        items = _plan_items(plan)
        changes: list[dict] = []
        settings_path = ctx.home / ".claude" / "settings.json"
        project_settings = ctx.root / ".claude" / "settings.json"
        data = _load_json_file(settings_path)
        user_dirty = False
        if "pre_tool_shell_filter" in items:
            if _ensure_hook(data, "PreToolUse", "rtk", matcher="Bash"):
                user_dirty = True
        if "session_start_note" in items:
            script = ctx.home / STATE_DIRNAME / "hooks" / "session-start.sh"
            if _ensure_hook(data, "SessionStart", str(script), matcher=None):
                user_dirty = True
        if user_dirty:
            changes.append({
                "path": str(settings_path),
                "kind": "modify" if settings_path.is_file() else "create",
                "content": _dump_json(data),
                "reason": "Wire Clean-profile hooks (RTK Bash filter, session-start note). Decline any proxy.",
            })
        if "pre_read_gate" in items:
            deny = _leanreads_patterns(ctx)
            pdata = _load_json_file(project_settings)
            existing = (((pdata.get("permissions") or {}).get("deny")) or [])
            if not isinstance(existing, list):
                existing = []
            merged = list(existing)
            for pat in deny:
                if pat not in merged:
                    merged.append(pat)
            if merged != existing:
                perms = dict(pdata.get("permissions") or {})
                perms["deny"] = merged
                pdata["permissions"] = perms
                changes.append({
                    "path": str(project_settings),
                    "kind": "modify" if project_settings.is_file() else "create",
                    "content": _dump_json(pdata),
                    "reason": "Native permissions.deny (leanreads) instead of a third-party Read hook.",
                })
        return {"changes": changes, "backup_dir": ""}

    def install_steps(self, tool_id: str, ctx: Context, options: dict) -> list[InstallStep] | None:

        if tool_id == "rtk-oss":
            return [InstallStep("Wire the OSS command-output hook for this agent (decline any proxy offer).",
                                command=["rtk", "init", "-g"], manual=True)]
        if tool_id == "caveman-skill":
            return [InstallStep("Install the local skill/plugin only (never the proxy CLI).",
                                command=["claude", "plugin", "marketplace", "add", "JuliusBrussee/caveman"],
                                network=True, manual=True)]
        if tool_id == "tokeneconomizer":
            return [InstallStep("Register the local plugin marketplace from the pinned checkout, then install it.",
                                command=None, manual=True)]
        if tool_id == "native-deny":
            return self.native_fix_steps(ctx)
        return None


    def plan_api_proxy_settings(self, ctx: Context, *, proxy_id: str, base_url: str, env: dict) -> list[dict]:
        """Point Claude at a loopback proxy through settings.json env (never shell rc)."""
        if not self.detect(ctx):
            return []
        path = ctx.home / ".claude" / "settings.json"
        data = _load_json_file(path)
        current = dict(data.get("env") or {}) if isinstance(data.get("env"), dict) else {}
        # Desktop intercept keys must never stay in adapter-owned settings.
        from ..api_proxy import FORBIDDEN_HEADROOM_ENV, INTERCEPT_ENV_KEYS
        for k in INTERCEPT_ENV_KEYS:
            current.pop(k, None)
        for k in FORBIDDEN_HEADROOM_ENV:
            current.pop(k, None)
        current.pop("_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL", None)
        for k, v in (env or {}).items():
            current[str(k)] = str(v)
        current["ANTHROPIC_BASE_URL"] = base_url
        data["env"] = current
        return [{
            "path": str(path),
            "kind": "modify" if path.is_file() else "create",
            "content": _dump_json(data),
            "reason": (
                f"Point Claude at the {proxy_id} proxy via ANTHROPIC_BASE_URL in "
                "adapter-owned settings.json (telemetry/analytics off; bind 127.0.0.1)."
            ),
        }]

    def native_fix_steps(self, ctx: Context) -> list[InstallStep]:
        ignore = ctx.root / ".claudeignore"
        if not ignore.is_file():
            return []
        patterns = []
        try:
            for line in ignore.read_text(encoding="utf-8").splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    patterns.append(f"Read(./{s})")
        except OSError:
            return []
        if not patterns:
            return []
        dest = ctx.root / ".claude" / "settings.json"
        return [InstallStep(
            "Convert ignore-file rules into native permission deny rules (no third-party hook).",
            edit={"path": str(dest), "merge": {"permissions": {"deny": patterns}}},
        )]


def _last_assistant_usage(path: Path, tail_bytes: int = 2_000_000) -> tuple[str, dict] | None:
    """(model, usage) of the newest assistant message, reading only the end of a large log."""
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - tail_bytes))
            chunk = fh.read()
    except OSError:
        return None
    lines = chunk.splitlines()
    if size > tail_bytes and lines:
        lines = lines[1:]  # first line may be partial
    for raw in reversed(lines):
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        msg = obj.get("message") if isinstance(obj, dict) else None
        if not isinstance(msg, dict) or obj.get("type") != "assistant":
            continue
        usage = msg.get("usage")
        model = str(msg.get("model") or "")
        if isinstance(usage, dict) and model and not model.startswith("<"):
            return model, usage
    return None


def _plan_items(plan) -> list[str]:
    if plan is None:
        return []
    if isinstance(plan, HookPlan):
        return list(plan.items or [])
    if isinstance(plan, dict):
        return list(plan.get("items") or [])
    return list(plan)


def _load_json_file(path: Path) -> dict:
    data = read_json(path)
    return data if isinstance(data, dict) and "__error__" not in data else {}


def _dump_json(data: dict) -> str:
    return json.dumps(data, indent=2) + "\n"


def _ensure_hook(settings: dict, event: str, command: str, matcher: str | None = None) -> bool:
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        hooks = {}
        settings["hooks"] = hooks
    groups = hooks.setdefault(event, [])
    if not isinstance(groups, list):
        groups = []
        hooks[event] = groups
    for g in groups:
        if not isinstance(g, dict):
            continue
        if g.get("command") == command:
            return False
        for h in g.get("hooks") or []:
            if isinstance(h, dict) and h.get("command") == command:
                return False
    entry: dict = {"hooks": [{"type": "command", "command": command}]}
    if matcher:
        entry["matcher"] = matcher
    groups.append(entry)
    return True


_DEFAULT_DENY = (
    "Read(./node_modules/**)",
    "Read(./dist/**)",
    "Read(./build/**)",
    "Read(./.venv/**)",
    "Read(./venv/**)",
    "Read(./target/**)",
    "Read(./__pycache__/**)",
)


def _patterns_from_ignore(path: Path) -> list[str]:
    out = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("!"):
            continue
        if s.startswith("/"):
            s = s[1:]
        out.append(f"Read(./{s})")
    return out


def _leanreads_patterns(ctx: Context) -> list[str]:
    pats = list(_DEFAULT_DENY)
    for name in (".claudeignore", ".gitignore"):
        pats.extend(_patterns_from_ignore(ctx.root / name))
    # unique, stable order
    seen = set()
    uniq = []
    for p in pats:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq
