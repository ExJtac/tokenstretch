"""Doctor-only adapter for Aider."""
from __future__ import annotations

from pathlib import Path

from ..context import Context
from ..yamlutil import load as load_yaml
from .base import ConfigLocation, DetectResult, InstructionFile, McpServer
from .doctor_only import DoctorOnlyAdapter, finding, instruction_size_findings, stat_instruction


def _aider_env(ctx: Context, flag: str) -> tuple[str | None, str]:
    # Documented as AIDER_* env vars matching the CLI flags (agents.md).
    name = "AIDER_" + flag.upper().replace("-", "_")
    val, where = ctx.env_or_rc(name)
    return (val, where or name) if val else (None, name)


def _conf_value(conf: dict, flag: str):
    """Read the hyphenated CLI name from .aider.conf.yml if present. No underscore alias invented."""
    if flag in conf:
        return conf[flag], flag
    return None, flag


class AiderAdapter(DoctorOnlyAdapter):
    id = "aider"
    display_name = "Aider"
    vendor_terms = "Aider (API keys)"
    implemented_checks = frozenset({"W01", "W06", "W09", "T04", "P01", "P03", "P05"})
    def _git_root(self, start: Path) -> Path | None:
        cur = start
        for _ in range(64):
            if (cur / ".git").exists():
                return cur
            if cur.parent == cur:
                return None
            cur = cur.parent
        return None

    def _conf_candidates(self, ctx: Context) -> list[tuple[Path, str]]:
        # Search cwd, git root, ~/.aider.conf.yml. Load home → git root → cwd; later wins.
        cwd = (ctx.root / ".aider.conf.yml").resolve() if (ctx.root / ".aider.conf.yml").is_file() else None
        git = self._git_root(ctx.root)
        git_p = (git / ".aider.conf.yml") if git is not None else None
        if git_p is not None and not git_p.is_file():
            git_p = None
        home_p = ctx.home / ".aider.conf.yml"
        if not home_p.is_file():
            home_p = None
        out: list[tuple[Path, str]] = []
        seen: set[str] = set()
        for path, scope in ((home_p, "user"), (git_p, "git"), (cwd, "project")):
            if path is None:
                continue
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key in seen:
                continue
            seen.add(key)
            out.append((path, scope))
        return out

    def _conf_path(self, ctx: Context) -> Path:
        cands = self._conf_candidates(ctx)
        return cands[-1][0] if cands else ctx.root / ".aider.conf.yml"

    def _conf(self, ctx: Context) -> dict:
        merged, _, _ = self._merged_conf(ctx)
        return merged

    def _load_yaml(self, path: Path) -> dict:
        try:
            data = load_yaml(path)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _merged_conf(self, ctx: Context) -> tuple[dict, dict, list]:
        merged: dict = {}
        sources: dict = {}
        raw: list = []
        for path, scope in self._conf_candidates(ctx):
            data = self._load_yaml(path)
            raw.append({"file": str(path), "scope": scope, "data": data})
            for k, v in data.items():
                merged[k] = v
                sources[k] = str(path)
        return merged, sources, raw

    def config_locations(self, ctx: Context) -> list[ConfigLocation]:
        cred = ctx.home / ".aider" / "oauth-keys.env"
        locs = [ConfigLocation(cred, "credential", ctx.exists(cred), sensitive=True)]
        for path, _scope in self._conf_candidates(ctx):
            locs.append(ConfigLocation(path, "settings", path.is_file()))
        return locs

    def detect(self, ctx: Context) -> DetectResult:
        binary = ctx.which("aider")
        # CONVENTIONS.md alone is not enough (the name is not Aider-exclusive).
        roots = [str(p) for p, _ in self._conf_candidates(ctx)]
        if (ctx.root / ".aider.chat.history.md").exists():
            roots.append(str(ctx.root / ".aider.chat.history.md"))
        return DetectResult(
            installed=bool(roots or binary),
            binary=str(binary) if binary else None,
            config_roots=roots,
            auth_mode="api-key" if (roots or binary) else "unknown",
        )

    def _value(self, ctx: Context, flag: str):
        # Env overrides YAML (configargparse AIDER_ prefix).
        env_val, where = _aider_env(ctx, flag)
        if env_val is not None:
            return env_val, where
        conf, sources, _ = self._merged_conf(ctx)
        val, key = _conf_value(conf, flag)
        if val is not None:
            return val, f"{sources.get(key, '')}:{key}"
        return None, ""

    def read_config(self, ctx: Context) -> dict:
        effective: dict = {}
        for flag, name in (
            ("model", "model"),
            ("weak-model", "weak_model"),
            ("editor-model", "editor_model"),
            ("reasoning-effort", "effort"),
            ("thinking-tokens", "thinking_tokens"),
            ("cache-prompts", "cache_prompts"),
            ("cache-keepalive-pings", "cache_keepalive_pings"),
            ("map-tokens", "map_tokens"),
            ("check-update", "check_update"),
        ):
            val, where = self._value(ctx, flag)
            if val is not None:
                effective[name] = {"value": val, "from": where}
        analytics, a_where = self._value(ctx, "analytics")
        if analytics is not None:
            effective["telemetry"] = {"value": analytics, "from": a_where}
        _, _, raw = self._merged_conf(ctx)
        return {"effective": effective, "raw": raw}

    def list_instruction_files(self, ctx: Context, cwd: str | None = None) -> list[InstructionFile]:
        root = Path(cwd) if cwd else ctx.root
        out: list[InstructionFile] = []
        # CONVENTIONS.md via --read or the `read:` key in .aider.conf.yml.
        conf = self._conf(ctx)
        declared = conf.get("read")
        paths: list[Path] = []
        if isinstance(declared, str) and declared.strip():
            paths.append((root / declared).resolve() if not Path(declared).is_absolute() else Path(declared))
        elif isinstance(declared, list):
            for item in declared:
                if isinstance(item, str) and item.strip():
                    p = Path(item)
                    paths.append(p if p.is_absolute() else (root / p))
        else:
            paths.append(root / "CONVENTIONS.md")
        seen: set[str] = set()
        for path in paths:
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key in seen:
                continue
            seen.add(key)
            item = stat_instruction(path, "project", True)
            if item:
                out.append(item)
        return out

    def list_mcp_servers(self, ctx: Context) -> list[McpServer]:
        # Research: no MCP in the docs that were read.
        return []

    def checks(self, ctx: Context) -> list:
        if not self.detect(ctx):
            return []
        findings = []
        files = self.list_instruction_files(ctx)
        findings.extend(instruction_size_findings(
            self.id, files, line_limit=200, byte_limit=25 * 1024, check_id="W01",
            slug="conventions-md-size", source="agents.md (CONVENTIONS.md; 200-line threshold is reasoning)",
        ))
        effort, e_where = self._value(ctx, "reasoning-effort")
        if str(effort).lower() in {"max", "xhigh", "ultra"}:
            findings.append(finding(
                self.id, "W06", "effort-max", "med", "waste",
                "Your AI is set to think as hard as possible on every message. That burns your allowance fastest.",
                "Lower reasoning-effort in .aider.conf.yml or AIDER_REASONING_EFFORT, and raise it only per task.",
                "agents.md (--reasoning-effort, --thinking-tokens)",
                file=e_where, key="reasoning-effort", value=str(effort),
            ))
        cache, c_where = self._value(ctx, "cache-prompts")
        if cache is False or str(cache).lower() in {"false", "0", "no", "off"}:
            findings.append(finding(
                self.id, "W09", "cache-disabled", "high", "waste",
                "A setting turns off prompt caching (--cache-prompts is off). Every message may repay earlier context.",
                "Enable cache-prompts in .aider.conf.yml (and use --no-stream if you want cache stats).",
                "agents.md (docs/usage/caching.html)",
                file=c_where, key="cache-prompts", value=str(cache),
            ))
        pings, p_where = self._value(ctx, "cache-keepalive-pings")
        try:
            n = int(pings) if pings is not None and str(pings) != "" else 0
        except (TypeError, ValueError):
            n = 1 if pings else 0
        if n:
            findings.append(finding(
                self.id, "T04", "tos-warmup", "info", "tos",
                "Aider --cache-keepalive-pings is on. It pings every 5 minutes to keep the cache warm, which costs money. This kit will never enable it.",
                "Remove cache-keepalive-pings from .aider.conf.yml and unset AIDER_CACHE_KEEPALIVE_PINGS.",
                "agents.md (docs/usage/caching.html)",
                file=p_where, key="cache-keepalive-pings", value=str(pings),
            ))
        findings.extend(self.privacy_checks(ctx))
        return findings

    def _p05(self, ctx: Context) -> list:
        check_update, c_where = self._value(ctx, "check-update")
        if check_update is None or check_update is True or str(check_update).lower() in {"true", "1", "yes", "on", ""}:
            return [finding(
                self.id, "P05", "startup-network", "info", "privacy",
                "Aider checks for updates on launch by default (a PyPI request, no user content).",
                "Set check-update: false in ~/.aider.conf.yml or AIDER_CHECK_UPDATE=false.",
                "adapter-gaps-v02.md (args.py default True; versioncheck.py)",
                file=c_where or "default true",
                key="check-update",
                value="default true" if check_update is None else str(check_update),
            )]
        return []

    def privacy_checks(self, ctx: Context) -> list:
        if not self.detect(ctx):
            return []
        findings = list(self._p05(ctx))
        analytics, a_where = self._value(ctx, "analytics")
        if analytics is True or str(analytics).lower() in {"true", "1", "yes", "on"}:
            findings.append(finding(
                self.id, "P01", "telemetry-on", "high", "privacy",
                "Aider analytics are enabled. They are opt-in; turn them off if you did not mean to share usage data.",
                "Run aider --analytics-disable, or turn analytics off in .aider.conf.yml.",
                "agents.md (docs/more/analytics.html)",
                file=a_where, key="analytics", value=str(analytics),
            ))
        history = ctx.root / ".aider.chat.history.md"
        if history.exists():
            findings.append(finding(
                self.id, "P03", "local-plaintext-retention", "info", "privacy",
                "Aider saves chats in plain text in .aider.chat.history.md (and --llm-history-file if you set one).",
                "Delete or relocate the chat history file if you do not want it kept.",
                "agents.md",
                file=str(history),
            ))
        return findings
