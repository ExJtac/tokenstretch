"""Tier-1 adapter for Gemini CLI.

Every Gemini-specific path, setting key, and log field lives in this module. Sources are the
gemini-cli docs and source (docs/reference/configuration.md, docs/hooks/reference.md,
docs/cli/headless.md, packages/core/src/services/chatRecordingService.ts, config/storage.ts,
config/projectRegistry.ts). Credential files (oauth_creds.json, gemini-credentials.json,
google_accounts.json, mcp-oauth-tokens.json, a2a-oauth-tokens.json, .env) are only stat'd.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import time
from pathlib import Path

from ..context import Context
from ..files import import_refs, text_if_small
from ..identity import PRODUCT_NAME, STATE_DIRNAME
from ..findings import UsageRecord
from ..util import is_loopback_url
from .base import (
    Adapter,
    BaseUrl,
    ConfigLocation,
    DetectResult,
    HeadlessRefused,
    HeadlessRun,
    HookPlan,
    InstallStep,
    InstructionFile,
    McpServer,
    SetupItem,
    RULES_START,
    add_rules_block,
    rules_block,
)
from .doctor_only import (
    DoctorOnlyAdapter,
    finding,
    instruction_size_findings,
    load_json,
    mcp_servers_in,
    stat_instruction,
)

_SECRET_ENV = re.compile(r"(API_KEY|AUTH|TOKEN|SECRET|PASSWORD|CREDENTIAL|PRIVATE_KEY)", re.I)
_PRO_MODEL = re.compile(r"(^pro$|gemini-4-argon|(^|-)pro(\b|-))", re.I)
_AUTO_MODEL = re.compile(r"^auto", re.I)


def _gemini_home(ctx: Context) -> Path:
    val, _ = ctx.env_or_rc("GEMINI_CLI_HOME")
    return Path(val).expanduser() if val else ctx.home


def _gemini_dir(ctx: Context) -> Path:
    return _gemini_home(ctx) / ".gemini"


def _expand_env_ref(text: str, env: dict) -> str:
    """Expand $VAR / ${VAR} / ${VAR:-default}. Secret-named vars stay unexpanded."""
    if not isinstance(text, str) or "$" not in text:
        return text

    def repl_braced(m):
        name, default = m.group(1), m.group(2)
        if _SECRET_ENV.search(name):
            return m.group(0)
        if name in env:
            return str(env[name])
        return default if default is not None else m.group(0)

    def repl_simple(m):
        name = m.group(1)
        if _SECRET_ENV.search(name):
            return m.group(0)
        return str(env[name]) if name in env else m.group(0)

    out = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}", repl_braced, text)
    out = re.sub(r"\$([A-Za-z_][A-Za-z0-9_]*)", repl_simple, out)
    return out


def _system_settings_path(ctx: Context) -> Path:
    val, _ = ctx.env_or_rc("GEMINI_CLI_SYSTEM_SETTINGS_PATH")
    if val:
        return Path(val).expanduser()
    plat = ctx.os_name() if hasattr(ctx, "os_name") else ""
    if plat.startswith("darwin"):
        return Path("/Library/Application Support/GeminiCli/settings.json")
    if plat.startswith("win"):
        prog = ctx.env.get("PROGRAMDATA") or r"C:\ProgramData"
        return Path(prog) / "gemini-cli" / "settings.json"
    return Path("/etc/gemini-cli/settings.json")


def _system_defaults_path(ctx: Context) -> Path:
    val, _ = ctx.env_or_rc("GEMINI_CLI_SYSTEM_DEFAULTS_PATH")
    if val:
        return Path(val).expanduser()
    return _system_settings_path(ctx).with_name("system-defaults.json")


class GeminiCliAdapter(DoctorOnlyAdapter):
    id = "gemini-cli"
    display_name = "Gemini CLI"
    vendor_terms = "Google"
    tier = 1
    implemented_checks = frozenset({
        "W01", "W02", "W05", "W06", "W09", "W14", "W16", "W20", "P01", "P02", "P03", "P05",
    })
    config_dir_env = ("GEMINI_CLI_HOME",)
    headless_patterns = (r"\bgemini\b.*\s(-p|--prompt)\b",)
    bench_key_env = "GEMINI_API_KEY"
    MID_MODEL = "flash"

    def _user_settings(self, ctx: Context) -> Path:
        return _gemini_dir(ctx) / "settings.json"

    def _project_settings(self, ctx: Context) -> Path:
        return ctx.root / ".gemini" / "settings.json"

    def _auth_files(self, ctx: Context) -> dict[str, Path]:
        d = _gemini_dir(ctx)
        return {
            "oauth": d / "oauth_creds.json",
            "encrypted": d / "gemini-credentials.json",
            "accounts": d / "google_accounts.json",
            "mcp_oauth": d / "mcp-oauth-tokens.json",
            "a2a_oauth": d / "a2a-oauth-tokens.json",
            "dotenv": d / ".env",
        }

    def config_locations(self, ctx: Context) -> list[ConfigLocation]:
        locs = [ConfigLocation(p, "credential", ctx.exists(p), sensitive=True) for p in self._auth_files(ctx).values()]
        for path, _scope, _data in self._settings_layers(ctx):
            locs.append(ConfigLocation(path, "settings", path.is_file()))
        return locs

    def detect(self, ctx: Context) -> DetectResult:
        binary = ctx.which("gemini")
        roots = [str(p) for p in (self._user_settings(ctx), self._project_settings(ctx), _gemini_dir(ctx)) if p.exists()]
        return DetectResult(
            installed=bool(roots or binary),
            binary=str(binary) if binary else None,
            config_roots=roots,
            auth_mode=self._auth_mode(ctx) if (roots or binary) else "unknown",
        )

    def _auth_mode(self, ctx: Context) -> str:
        # Env *names* only; values are never read into findings.
        if any(n in ctx.env for n in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI")):
            return "api-key"
        files = self._auth_files(ctx)
        if any(ctx.exists(files[k]) for k in ("oauth", "encrypted", "accounts")):
            return "subscription"
        return "unknown"

    def auth_mode(self, ctx: Context) -> str:
        mode = self._auth_mode(ctx)
        return "api" if mode == "api-key" else mode

    def _settings_layers(self, ctx: Context) -> list[tuple[Path, str, dict]]:
        # Precedence lowest first: system-defaults < user < project < system.
        layers = [
            (_system_defaults_path(ctx), "system-defaults"),
            (self._user_settings(ctx), "user"),
            (self._project_settings(ctx), "project"),
            (_system_settings_path(ctx), "system"),
        ]
        out = []
        for path, scope in layers:
            if path.is_file():
                out.append((path, scope, load_json(path)))
        return out

    def _merged(self, ctx: Context) -> tuple[dict, str]:
        merged: dict = {}
        source = ""
        for path, scope, data in self._settings_layers(ctx):
            merged = _deep_merge(merged, data)
            source = f"{path} ({scope})"
        return merged, source

    def read_config(self, ctx: Context) -> dict:
        merged, source = self._merged(ctx)
        effective: dict = {}
        model = merged.get("model") if isinstance(merged.get("model"), dict) else {}
        if model.get("name"):
            effective["model"] = {"value": _expand_env_ref(str(model["name"]), ctx.env), "from": f"{source}:model.name"}
        if "compressionThreshold" in model:
            effective["compression_threshold"] = {
                "value": model["compressionThreshold"], "from": f"{source}:model.compressionThreshold",
            }
        privacy = merged.get("privacy") if isinstance(merged.get("privacy"), dict) else {}
        if self.detect(ctx):
            enabled = privacy.get("usageStatisticsEnabled")
            effective["telemetry"] = {
                "value": True if enabled is None else bool(enabled),
                "from": f"{source}:privacy.usageStatisticsEnabled" if source else "default true",
            }
        ctx_cfg = merged.get("context") if isinstance(merged.get("context"), dict) else {}
        if ctx_cfg.get("fileName"):
            effective["instruction_names"] = {"value": ctx_cfg["fileName"], "from": f"{source}:context.fileName"}
        effective["auth_mode"] = {"value": self._auth_mode(ctx), "from": "stat/env names only"}
        raw = [{"file": str(path), "scope": scope, "data": data} for path, scope, data in self._settings_layers(ctx)]
        return {"effective": effective, "raw": raw}

    def _instruction_names(self, ctx: Context) -> list[str]:
        merged, _ = self._merged(ctx)
        ctx_cfg = merged.get("context") if isinstance(merged.get("context"), dict) else {}
        names = ctx_cfg.get("fileName") or "GEMINI.md"
        if isinstance(names, str):
            names = [names]
        return [str(n) for n in names if n]

    def list_instruction_files(self, ctx: Context, cwd: str | None = None) -> list[InstructionFile]:
        names = self._instruction_names(ctx)
        merged, _ = self._merged(ctx)
        ctx_cfg = merged.get("context") if isinstance(merged.get("context"), dict) else {}
        markers = ctx_cfg.get("memoryBoundaryMarkers")
        if markers is None:
            markers = [".git"]
        if not isinstance(markers, list):
            markers = [markers]
        max_dirs = int(ctx_cfg.get("discoveryMaxDirs") or 200)
        root = Path(cwd) if cwd else ctx.root
        found: list[Path] = []
        # 1. Global ~/.gemini/<name>
        for name in names:
            found.append(_gemini_dir(ctx) / name)
        # 2. Upward walk from cwd, stop at the first memory boundary marker or home.
        cur = root
        try:
            home = _gemini_home(ctx).resolve()
        except OSError:
            home = _gemini_home(ctx)
        seen_dirs: set[str] = set()
        for _ in range(64):
            try:
                key = str(cur.resolve())
                resolved = cur.resolve()
            except OSError:
                key = str(cur)
                resolved = cur
            if key in seen_dirs:
                break
            seen_dirs.add(key)
            for name in names:
                found.append(cur / name)
            if markers and any((cur / str(m)).exists() for m in markers):
                break
            if resolved == home or cur == cur.parent:
                break
            cur = cur.parent
        # 3. Downward scan under cwd, capped by discoveryMaxDirs.
        dirs_seen = 0
        if root.is_dir():
            for dirpath, dirnames, filenames in os.walk(root):
                dirs_seen += 1
                if dirs_seen > max_dirs:
                    dirnames[:] = []
                    break
                for name in names:
                    if name in filenames:
                        found.append(Path(dirpath) / name)
        # 4. @imports, max depth 5, included in size.
        out: list[InstructionFile] = []
        seen: set[str] = set()
        for path in found:
            self._collect_instruction(ctx, path, out, seen, depth=0)
        return out

    def _instruction_scope(self, ctx: Context, path: Path) -> str:
        try:
            gdir = _gemini_dir(ctx).resolve()
            resolved = path.resolve()
            if resolved == gdir or gdir in resolved.parents:
                return "user"
        except OSError:
            if ".gemini" in path.parts:
                return "user"
        return "project"

    def _collect_instruction(self, ctx: Context, path: Path, out: list[InstructionFile], seen: set[str], depth: int) -> None:
        try:
            key = str(path.resolve())
        except OSError:
            key = str(path)
        if key in seen or depth > 5:
            return
        item = stat_instruction(path, self._instruction_scope(ctx, path), True)
        if not item:
            return
        seen.add(key)
        out.append(item)
        text = text_if_small(path)
        if not text:
            return
        for imp in import_refs(text, path):
            self._collect_instruction(ctx, imp, out, seen, depth + 1)

    def list_mcp_servers(self, ctx: Context) -> list[McpServer]:
        out: list[McpServer] = []
        for path, scope, data in self._settings_layers(ctx):
            out.extend(mcp_servers_in(data, file=str(path), scope=scope))
        return out

    def checks(self, ctx: Context) -> list:
        if not self.detect(ctx):
            return []
        findings = []
        files = self.list_instruction_files(ctx)
        gemini_like = [f for f in files if Path(f.path).name != "AGENTS.md"]
        agents = [f for f in files if Path(f.path).name == "AGENTS.md"]
        findings.extend(instruction_size_findings(
            self.id, gemini_like, line_limit=200, byte_limit=25 * 1024, check_id="W01",
            slug="gemini-md-size", source="adapter-gaps-v02.md (expanded size including @imports)",
        ))
        findings.extend(instruction_size_findings(
            self.id, agents, line_limit=200, byte_limit=16 * 1024, check_id="W02",
            slug="agents-md-size", source="agents.md (context.fileName may include AGENTS.md)",
        ))
        cfg = self.read_config(ctx)
        model = str((cfg["effective"].get("model") or {}).get("value") or "")
        if model and _PRO_MODEL.search(model):
            ev = cfg["effective"]["model"]
            findings.append(finding(
                self.id, "W05", "default-model-heavy", "med", "waste",
                "The default Gemini model is a Pro-tier (expensive) ID. Keep Pro for hard problems.",
                "Set model.name to a Flash or Flash-lite ID for everyday work.",
                "adapter-gaps-v02.md (models.ts Pro tier; prices change — not hard-coded in the verdict)",
                file=ev.get("from"), key="model.name", value=model,
            ))
        elif model and _AUTO_MODEL.search(model):
            ev = cfg["effective"]["model"]
            findings.append(finding(
                self.id, "W05", "default-model-heavy", "info", "waste",
                "The default model is an auto alias; it may route to Pro for complex prompts.",
                "Pin a Flash model if you want to avoid Pro-tier routing.",
                "adapter-gaps-v02.md (docs/cli/model.md)",
                file=ev.get("from"), key="model.name", value=model,
            ))
        findings.extend(self._thinking_findings(ctx))
        if self._auth_mode(ctx) == "subscription" and not any(n in ctx.env for n in ("GEMINI_API_KEY", "GOOGLE_API_KEY")):
            findings.append(finding(
                self.id, "W09", "cache-disabled", "med", "waste",
                "Gemini CLI token caching is not available for OAuth (Google account) logins.",
                "For heavy use, authenticate with an API key (Gemini API or Vertex) so caching can apply.",
                "agents.md (docs/cli/token-caching.md); oauth file presence was stat'd, never opened.",
                file=str(self._auth_files(ctx)["oauth"]),
                key="auth_mode",
                value="subscription",
            ))
        findings.extend(self._w14(ctx) + self._w16(ctx) + self._w20(ctx))
        return findings  # privacy_checks() is run separately by doctor

    def _w14(self, ctx: Context) -> list:
        from ..logcache import memo_by_stat
        peaks = []
        for path in self.default_log_paths(ctx):
            peak = memo_by_stat(path, "gemini-w14", lambda path=path: max(
                [r.total_input() for r in self.parse_log(path)] or [0]))
            if peak:
                peaks.append(peak)
        if not peaks:
            return []
        peaks.sort()
        mid = peaks[len(peaks) // 2]
        if mid <= 150_000:
            return []
        return [finding(
            self.id, "W14", "long-sessions", "med", "waste",
            "You're keeping one giant conversation going. Every new message re-sends all of it. "
            "Start fresh when you switch tasks.",
            "Use /clear between tasks, or /compress when a session must continue.",
            "chatRecordingService.ts (per-message tokens.input); docs/cli/session-management.md",
            key="median_peak_context_tokens", value=mid,
        )]

    def _w16(self, ctx: Context) -> list:
        merged, source = self._merged(ctx)
        billing = merged.get("billing") if isinstance(merged.get("billing"), dict) else {}
        if str(billing.get("overageStrategy") or "").lower() != "always":
            return []
        return [finding(
            self.id, "W16", "auto-continue", "med", "waste",
            "When your quota runs out, Gemini CLI spends AI credits automatically without asking.",
            "Set billing.overageStrategy to \"ask\" (the default) or \"never\".",
            "docs/reference/configuration.md (billing.overageStrategy)",
            file=source, key="billing.overageStrategy", value="always",
        )]

    def _w20(self, ctx: Context) -> list:
        merged, source = self._merged(ctx)
        model = merged.get("model") if isinstance(merged.get("model"), dict) else {}
        raw = model.get("compressionThreshold")
        try:
            val = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            val = None
        if val is None or val < 0.9:
            return []
        return [finding(
            self.id, "W20", "auto-compact-off", "info", "waste",
            "Automatic compression only starts when the context is almost full, so long sessions re-send "
            "nearly the whole window on every step.",
            "Lower model.compressionThreshold (default 0.5), or run /compress by hand.",
            "docs/reference/configuration.md (model.compressionThreshold, default 0.5)",
            file=source, key="model.compressionThreshold", value=raw,
        )]

    def _thinking_findings(self, ctx: Context) -> list:
        merged, source = self._merged(ctx)
        cfgs = list(_iter_thinking(merged))
        out = []
        both = False
        for where, budget, level, model in cfgs:
            if budget is not None and level is not None:
                both = True
            model_l = str(model or (self.read_config(ctx)["effective"].get("model") or {}).get("value") or "").lower()
            flash = "flash" in model_l
            try:
                b = int(budget) if budget is not None else None
            except (TypeError, ValueError):
                b = None
            if b is not None and (b < 0 or (flash and b >= 24576) or (not flash and b >= 32768)):
                out.append(finding(
                    self.id, "W06", "effort-max", "med", "waste",
                    "Gemini thinkingBudget is at the documented ceiling (or set to dynamic -1 on Pro).",
                    "Lower thinkingBudget, or use thinkingLevel on Gemini 3+ (do not set both).",
                    "adapter-gaps-v02.md (Vertex AI thinking docs)",
                    file=where, key="thinkingBudget", value=str(budget),
                ))
            if str(level or "").upper() == "HIGH":
                out.append(finding(
                    self.id, "W06", "effort-max", "med", "waste",
                    "Gemini thinkingLevel is HIGH, the maximum for Gemini 3+ models.",
                    "Use a lower thinkingLevel for everyday work.",
                    "adapter-gaps-v02.md (ai.google.dev thinking levels)",
                    file=where, key="thinkingLevel", value=str(level),
                ))
        if both:
            out.append(finding(
                self.id, "W06", "effort-max", "med", "waste",
                "thinkingBudget and thinkingLevel are both set. The API rejects that combination.",
                "Set only thinkingLevel on Gemini 3+, or only thinkingBudget on Gemini 2.5.",
                "adapter-gaps-v02.md",
                file=source, key="thinkingConfig",
            ))
        return out

    def privacy_checks(self, ctx: Context) -> list:
        if not self.detect(ctx):
            return []
        findings = []
        merged, source = self._merged(ctx)
        privacy = merged.get("privacy") if isinstance(merged.get("privacy"), dict) else {}
        enabled = privacy.get("usageStatisticsEnabled")
        if enabled is not False:
            findings.append(finding(
                self.id, "P01", "telemetry-on", "info", "privacy",
                "Gemini CLI sends usage statistics to Google by default (privacy.usageStatisticsEnabled defaults to true).",
                "Set privacy.usageStatisticsEnabled to false in ~/.gemini/settings.json or .gemini/settings.json.",
                "agents.md (docs/cli/telemetry.md)",
                file=source or str(self._user_settings(ctx)),
                key="privacy.usageStatisticsEnabled",
                value="true" if enabled is None else str(enabled),
            ))
        for srv in self.list_mcp_servers(ctx):
            if srv.remote_url:
                findings.append(finding(
                    self.id, "P02", "third-party-cloud", "info", "privacy",
                    f"Remote MCP server {srv.name!r} is configured; it receives tool arguments.",
                    "Remove the remote MCP server if you did not mean to share tool arguments with it.",
                    "agents.md (mcpServers in settings.json)",
                    file=srv.file, key=srv.name, value=srv.remote_url,
                ))
        findings.extend(self._telemetry_findings(ctx, merged, source))
        general = merged.get("general") if isinstance(merged.get("general"), dict) else {}
        if general.get("enableAutoUpdate") is not False:
            findings.append(finding(
                self.id, "P05", "startup-network", "info", "privacy",
                "Gemini CLI checks for and installs updates on launch by default (no prompt content).",
                "Set general.enableAutoUpdate to false if you want to update by hand.",
                "docs/reference/configuration.md (general.enableAutoUpdate, default true)",
                file=source or str(self._user_settings(ctx)), key="general.enableAutoUpdate",
                value="default true" if "enableAutoUpdate" not in general else str(general.get("enableAutoUpdate")),
            ))
        chats = _gemini_dir(ctx) / "tmp"
        if chats.is_dir():
            findings.append(finding(
                self.id, "P03", "local-plaintext-retention", "info", "privacy",
                "Gemini CLI sessions are saved in plain text under ~/.gemini/tmp/<project_hash>/chats/.",
                "Delete old session folders under ~/.gemini/tmp if you do not want them kept.",
                "agents.md (docs/cli/session-management.md)",
                file=str(chats),
            ))
        return findings

    def _telemetry_findings(self, ctx: Context, merged: dict, source: str) -> list:
        """OpenTelemetry export (telemetry.*). Off by default; GEMINI_TELEMETRY_* env vars override settings."""
        tel = merged.get("telemetry") if isinstance(merged.get("telemetry"), dict) else {}

        def pick(key: str, env: str):
            val, where = ctx.env_or_rc(env)
            if val is not None:
                return val, f"{where}:{env}"
            if key in tel:
                return tel[key], f"{source}:telemetry.{key}"
            return None, ""

        enabled, where = pick("enabled", "GEMINI_TELEMETRY_ENABLED")
        if not _truthy(enabled):
            return []
        target, _ = pick("target", "GEMINI_TELEMETRY_TARGET")
        endpoint, _ = pick("otlpEndpoint", "GEMINI_TELEMETRY_OTLP_ENDPOINT")
        outfile, _ = pick("outfile", "GEMINI_TELEMETRY_OUTFILE")
        prompts, _ = pick("logPrompts", "GEMINI_TELEMETRY_LOG_PROMPTS")
        with_prompts = prompts is None or _truthy(prompts)
        remote = str(target or "local").lower() == "gcp" or (
            not outfile and endpoint and not is_loopback_url(str(endpoint)) and "localhost" not in str(endpoint))
        if remote:
            return [finding(
                self.id, "P01", "telemetry-on", "high" if with_prompts else "med", "privacy",
                "Gemini CLI OpenTelemetry export is on and goes off this machine"
                + (", including your prompts (telemetry.logPrompts defaults to true)." if with_prompts else "."),
                "Set telemetry.enabled to false, or keep target \"local\" with an outfile and logPrompts false.",
                "docs/cli/telemetry.md (target, otlpEndpoint, logPrompts)",
                file=where, key="telemetry", value=str(target or endpoint),
            )]
        if outfile and with_prompts:
            return [finding(
                self.id, "P03", "local-plaintext-retention", "info", "privacy",
                "Gemini CLI writes telemetry, including prompts, to a local file.",
                "Set telemetry.logPrompts to false if you do not want prompts in that file.",
                "docs/cli/telemetry.md (outfile, logPrompts)",
                file=where, key="telemetry.outfile", value=str(outfile),
            )]
        return []

    # ---- base URLs, hooks, models (facts the core's cross-tool checks need) -----------------
    def base_urls(self, ctx: Context) -> list[BaseUrl]:
        out = []
        for name in ("GOOGLE_GEMINI_BASE_URL", "GOOGLE_VERTEX_BASE_URL"):
            val, where = ctx.env_or_rc(name)
            if val:
                out.append(BaseUrl(str(val), where or "env", name))
        return out

    def hook_commands(self, ctx: Context) -> list[tuple[str, str]]:
        out = []
        for path, _scope, data in self._settings_layers(ctx):
            hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
            for event, groups in hooks.items():
                for g in groups if isinstance(groups, list) else []:
                    for h in (g.get("hooks") or []) if isinstance(g, dict) else []:
                        if isinstance(h, dict) and h.get("command"):
                            out.append((str(h["command"]), f"{path}:hooks.{event}"))
        return out

    def effective_models(self, ctx: Context) -> list[tuple[str, str]]:
        cfg = self.read_config(ctx)["effective"].get("model")
        return [(str(cfg["value"]), str(cfg["from"]))] if cfg else []

    # ---- usage logs ------------------------------------------------------------------------
    def _project_dirs(self, ctx: Context) -> list[Path]:
        tmp = _gemini_dir(ctx) / "tmp"
        try:
            return [p for p in tmp.iterdir() if p.is_dir()]
        except OSError:
            return []

    def default_log_paths(self, ctx: Context) -> list[Path]:
        """~/.gemini/tmp/<project>/chats/**/*.jsonl (and legacy *.json) session records."""
        out: list[Path] = []
        for proj in self._project_dirs(ctx):
            chats = proj / "chats"
            if not chats.is_dir():
                continue
            for p in chats.rglob("*"):
                if p.suffix in {".jsonl", ".json"} and p.is_file():
                    out.append(p)
        return sorted(out)

    def sniff_log(self, path: Path, first_lines: list[str]) -> bool:
        blob = "\n".join(first_lines)
        return '"projectHash"' in blob and '"sessionId"' in blob

    def parse_log(self, path: Path) -> list[UsageRecord]:
        cwd = _project_root_for(path)
        if path.suffix == ".jsonl":
            parser = GeminiChatParser(path, self.id, cwd)
            from ..logcache import read_lines_from
            try:
                read_lines_from(path, 0, parser.feed)
            except OSError:
                return []
            return parser.records
        return _parse_legacy_json(path, self.id, cwd)

    incremental_logs = True

    def usage_log_parser(self, path: Path):
        return GeminiChatParser(path, self.id, _project_root_for(path)) if path.suffix == ".jsonl" else None

    def read_usage_logs(self, ctx: Context, since_days: int = 30) -> list[UsageRecord]:
        from ..usage import record_in_window
        cutoff = time.time() - since_days * 86400
        out = []
        for path in self.default_log_paths(ctx):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            for r in self.parse_log(path):
                if (r.ts and record_in_window(r, cutoff, None)) or (not r.ts and mtime >= cutoff):
                    out.append(r)
        return out

    def _session_label(self, ctx: Context, path: Path) -> str:
        root = _project_root_for(path)
        if root:
            return Path(root).name
        proj = _project_dir_of(path)
        if proj is not None:
            try:
                if proj.name == hashlib.sha256(str(ctx.root.resolve()).encode()).hexdigest():
                    return ctx.root.name
            except OSError:
                pass
            return proj.name[:12]
        return path.parent.name

    def _recent_logs(self, ctx: Context, limit: int, max_age_s: float | None) -> list[tuple[float, Path]]:
        cut = time.time() - max_age_s if max_age_s else None
        found = []
        for p in self.default_log_paths(ctx):
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if cut is None or m >= cut:
                found.append((m, p))
        return sorted(found, reverse=True)[:limit]

    def watch_sessions(self, ctx: Context, limit: int = 5, max_age_s: float | None = None) -> list[dict] | None:
        """Per-turn usage from Gemini chat records. Read-call paths are not taken (no file contents either)."""
        from ..logcache import memo_by_stat
        out = []
        for mtime, path in self._recent_logs(ctx, limit, max_age_s):
            recs = memo_by_stat(path, "gemini-chat", lambda path=path: self.parse_log(path))
            turns = [{
                "ts": r.ts, "model": r.model or "", "history_tokens": r.total_input(),
                "output_tokens": r.output_tokens, "tool_calls": (r.extra or {}).get("tool_calls"), "reads": [],
            } for r in sorted(recs, key=lambda r: r.ts or "")]
            if not turns:
                continue
            sid = (recs[0].session_id or path.stem) if recs else path.stem
            out.append({
                "adapter": self.id, "session": str(sid)[-8:], "project": self._session_label(ctx, path),
                "path": str(path), "last_write": mtime, "turns": turns,
                "clear_hint": "/clear", "compact_hint": "/compress",
            })
        return out

    def live_sessions(self, ctx: Context, limit: int = 5, max_age_s: float | None = None) -> list[dict]:
        out = []
        for s in self.watch_sessions(ctx, limit=limit, max_age_s=max_age_s) or []:
            last = s["turns"][-1]
            out.append({
                "adapter": self.id, "session": s["session"], "project": s["project"], "model": last["model"],
                "history_tokens": last["history_tokens"], "last_write": s["last_write"], "path": s["path"],
            })
        return out

    # ---- headless bench --------------------------------------------------------------------
    def headless_env(self, home: Path) -> dict[str, str]:
        return {"HOME": str(home), "GEMINI_CLI_HOME": str(home)}

    def apply_model_flag(self, argv: list[str], model: str) -> list[str]:
        return list(argv) + ["--model", model]

    def official_command(self, prompt: str) -> list[str]:
        return ["gemini", "-p", prompt, "--output-format", "stream-json"]

    def headless_run(self, prompt: str, cwd: Path, model: str | None = None) -> HeadlessRun | None:
        argv = self.official_command(prompt)
        if model:
            argv = self.apply_model_flag(argv, model)
        return HeadlessRun(argv, cwd)

    def run_headless_task(self, ctx: Context, task: dict | None = None, opts: dict | None = None) -> dict:
        """Bench only. Turns usage statistics off in the sandbox HOME's settings before the run."""
        opts = opts or {}
        if not opts.get("bench"):
            raise HeadlessRefused("run_headless_task refused without --bench")
        home = Path(opts.get("home") or ctx.home)
        try:
            if home.resolve() == Path(os.path.expanduser("~")).resolve():
                raise HeadlessRefused("refusing to run the Gemini bench against the real HOME")
        except OSError:
            raise HeadlessRefused("could not resolve the bench HOME")
        settings = home / ".gemini" / "settings.json"
        data = _editable_json(settings)
        if data is None:
            raise HeadlessRefused(f"{settings} is not plain JSON; refusing to edit it for the bench")
        privacy = data.get("privacy") if isinstance(data.get("privacy"), dict) else {}
        if privacy.get("usageStatisticsEnabled") is not False:
            data["privacy"] = {**privacy, "usageStatisticsEnabled": False}
            settings.parent.mkdir(parents=True, exist_ok=True)
            settings.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return Adapter.run_headless_task(self, ctx, task, opts)

    def parse_headless_output(self, stdout: str, task_id: str) -> UsageRecord | None:
        """`--output-format stream-json`: the final {"type":"result","stats":{...}} event (docs/cli/headless.md)."""
        from ..usage import normalize_usage, record_from_normalized
        for line in reversed((stdout or "").splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "result" or not isinstance(obj.get("stats"), dict):
                continue
            st = obj["stats"]
            models = st.get("models") if isinstance(st.get("models"), dict) else {}
            norm = normalize_usage(
                input_tokens=int(st.get("input_tokens") or 0),
                cache_read=int(st.get("cached") or 0),
                output=int(st.get("output_tokens") or 0),
                input_includes_cache=True,
            )
            return record_from_normalized(
                self.id, "headless", norm, task_id=task_id,
                model=next(iter(models)) if len(models) == 1 else ("mixed" if models else None),
                success=obj.get("status") == "success" if obj.get("status") else None,
                extra={"tool_calls": st.get("tool_calls"), "duration_ms": st.get("duration_ms")},
            )
        return None

    # ---- install / setup -------------------------------------------------------------------
    def _session_script(self, ctx: Context) -> Path:
        return ctx.home / STATE_DIRNAME / "hooks" / "gemini-session-start.sh"

    def install_hooks(self, ctx: Context, plan: HookPlan | dict | None = None, *, dry_run: bool = True) -> dict:
        """Plan settings.json hook and .geminiignore edits. Never writes; the installer applies changes."""
        items = plan.items if isinstance(plan, HookPlan) else list((plan or {}).get("items") or [])
        if not self.detect(ctx):
            return {"changes": [], "backup_dir": ""}
        changes: list[dict] = []
        if "session_start_note" in items:
            script = self._session_script(ctx)
            body = (
                "#!/bin/sh\n"
                f"# {PRODUCT_NAME} Gemini CLI session-start note. No network. Prints a systemMessage\n"
                "# (shown to you, not added to the model's context).\n"
                f"echo '{{\"systemMessage\": \"{PRODUCT_NAME}: Clean profile is active. Telemetry stays off.\"}}'\n"
            )
            existing = script.read_text(encoding="utf-8") if script.is_file() else None
            if existing != body:
                changes.append({"path": str(script), "kind": "modify" if script.is_file() else "create",
                                "content": body, "mode": 0o755,
                                "reason": "Local no-network SessionStart script for Gemini CLI (JSON systemMessage)."})
            settings = self._user_settings(ctx)
            raw = _editable_json(settings)
            if raw is not None:  # unreadable settings (e.g. comments): leave untouched
                data = json.loads(json.dumps(raw))
                if _ensure_gemini_hook(data, "SessionStart", f"sh {shlex.quote(str(script))}",
                                       f"{PRODUCT_NAME}-session-start"):
                    changes.append({"path": str(settings), "kind": "modify" if settings.is_file() else "create",
                                    "content": json.dumps(data, indent=2) + "\n",
                                    "reason": "Wire the Clean-profile SessionStart note (hooks.SessionStart)."})
        if "pre_read_gate" in items and ctx.root.resolve() != ctx.home.resolve():
            ignore = ctx.root / ".geminiignore"
            existing = ignore.read_text(encoding="utf-8", errors="replace") if ignore.is_file() else ""
            have = {ln.strip().strip("/") for ln in existing.splitlines()}
            git = ctx.root / ".gitignore"
            if git.is_file():
                have |= {ln.strip().strip("/") for ln in git.read_text(encoding="utf-8", errors="replace").splitlines()}
            add = [d for d in _IGNORE_DIRS if d.strip("/") not in have]
            if add:
                body = existing + ("" if not existing or existing.endswith("\n") else "\n")
                body += f"# {PRODUCT_NAME}: keep build and dependency folders out of Gemini reads\n"
                body += "".join(f"{d}\n" for d in add)
                changes.append({"path": str(ignore), "kind": "modify" if ignore.is_file() else "create",
                                "content": body,
                                "reason": "Native .geminiignore (read gate) for dependency/build folders not in .gitignore."})
        return {"changes": changes, "backup_dir": ""}

    def install_steps(self, tool_id: str, ctx: Context, options: dict) -> list[InstallStep] | None:
        if tool_id == "rtk-oss":
            return [InstallStep("Wire the OSS command-output hook for Gemini CLI (BeforeTool).",
                                command=["rtk", "init", "-g", "--gemini"], manual=True)]
        return None

    def setup_items(self, ctx: Context) -> list[SetupItem]:
        items: list[SetupItem] = []
        if not self.detect(ctx):
            return items
        settings = self._user_settings(ctx)
        merged, _ = self._merged(ctx)
        privacy = merged.get("privacy") if isinstance(merged.get("privacy"), dict) else {}
        data = _editable_json(settings)
        if privacy.get("usageStatisticsEnabled") is not False and data is not None:
            new = json.loads(json.dumps(data))
            new["privacy"] = {**(new.get("privacy") if isinstance(new.get("privacy"), dict) else {}),
                              "usageStatisticsEnabled": False}
            items.append(SetupItem(
                id="gemini-usage-stats-off", adapter_id=self.id,
                title="Turn off Gemini CLI usage statistics",
                why="Gemini CLI sends usage statistics to Google unless privacy.usageStatisticsEnabled is false.",
                changes=[{"path": str(settings), "kind": "modify" if settings.is_file() else "create",
                          "content": json.dumps(new, indent=2) + "\n", "key": "privacy.usageStatisticsEnabled",
                          "reason": "set privacy.usageStatisticsEnabled to false (takes effect after a restart)"}],
                done="turned off Gemini CLI usage statistics",
            ))
        if ctx.root.resolve() != ctx.home.resolve():
            names = self._instruction_names(ctx)
            loaded = [ctx.root / n for n in names if n != "GEMINI.md"]
            if not any(_has_rules(p) for p in loaded):
                path = ctx.root / "GEMINI.md"
                if "GEMINI.md" in names and path.is_file():
                    new = add_rules_block(path.read_text(encoding="utf-8", errors="replace"),
                                          rules_block(self.MID_MODEL, "/clear"))
                    if new is not None:
                        items.append(SetupItem(
                            id="gemini-rules-block", adapter_id=self.id,
                            title="Add 5 short credit-saving rules to the project GEMINI.md",
                            why="Gemini CLI re-sends the conversation each step. Fresh tasks, narrow reads, "
                                "and short replies keep that small.",
                            changes=[{"path": str(path), "kind": "modify", "content": new, "key": "credit-rules block",
                                      "reason": "append a marked rules block (about 0.7 KB)"}],
                            done="added credit-saving rules to GEMINI.md",
                        ))
        return items

    # ---- model picker ----------------------------------------------------------------------
    def model_tiers(self) -> dict[str, str]:
        # Documented aliases (docs/cli/model.md, modelDefinitions in configuration.md).
        return {"light": "flash-lite", "mid": "flash", "top": "pro"}

    def is_top_model(self, model: str) -> bool:
        return bool(_PRO_MODEL.search(model or ""))

    def model_switch_hint(self, model: str) -> str:
        return "/model"

    def plan_default_model(self, ctx: Context, tier: str) -> SetupItem | None:
        target = self.model_tiers().get(tier)
        if not target:
            return None
        settings = self._user_settings(ctx)
        data = _editable_json(settings)
        if data is None:
            return None  # unreadable: never overwrite
        model = data.get("model") if isinstance(data.get("model"), dict) else {}
        current = self.read_config(ctx)["effective"].get("model")
        if current and str(current.get("value")) == target:
            return None
        new = json.loads(json.dumps(data))
        new["model"] = {**model, "name": target}
        project = self._project_settings(ctx)
        note = []
        if project.is_file() and isinstance(load_json(project).get("model"), dict) and load_json(project)["model"].get("name"):
            note.append(f"{project} also sets model.name and wins over the user file in this project.")
        return SetupItem(
            id="gemini-default-model", adapter_id=self.id,
            title=f"Make {target} the default Gemini CLI model",
            why=f"Pro-tier models cost the most per token; {target} is the {tier} tier.",
            changes=[{"path": str(settings), "kind": "modify" if settings.is_file() else "create",
                      "content": json.dumps(new, indent=2) + "\n", "key": "model.name",
                      "reason": f"set model.name to {target}"}],
            done=f"set the default Gemini CLI model to {target}",
            notes=note,
        )


_IGNORE_DIRS = ("node_modules/", "dist/", "build/", ".venv/", "venv/", "target/", "__pycache__/")


def _editable_json(path: Path) -> dict | None:
    """Settings as a dict for a planned edit: {} if missing, None if it is not a plain JSON object."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _has_rules(path: Path) -> bool:
    text = text_if_small(path) if path.is_file() else None
    return bool(text and RULES_START in text)


def _project_dir_of(path: Path) -> Path | None:
    """~/.gemini/tmp/<project>/ for a chat file under .../<project>/chats/..."""
    for parent in path.parents:
        if parent.name == "chats":
            return parent.parent
    return None


def _project_root_for(path: Path) -> str | None:
    """Project path from the `.project_root` ownership marker (projectRegistry.ts). A path only."""
    proj = _project_dir_of(path)
    if proj is None:
        return None
    marker = proj / ".project_root"
    try:
        if marker.is_file() and marker.stat().st_size < 4096:
            return marker.read_text(encoding="utf-8", errors="replace").strip() or None
    except OSError:
        return None
    return None


def _ensure_gemini_hook(data: dict, event: str, command: str, name: str) -> bool:
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else None
    if hooks is None:
        hooks = {}
        data["hooks"] = hooks
    groups = hooks.get(event) if isinstance(hooks.get(event), list) else None
    if groups is None:
        groups = []
        hooks[event] = groups
    for g in groups:
        for h in (g.get("hooks") or []) if isinstance(g, dict) else []:
            if isinstance(h, dict) and (h.get("command") == command or h.get("name") == name):
                return False
    groups.append({"hooks": [{"type": "command", "name": name, "command": command, "timeout": 5000}]})
    return True


class GeminiChatParser:
    """Incremental reader for one Gemini CLI chat .jsonl (chatRecordingService.ts).

    Lines are prefiltered on byte markers so only metadata lines and messages that carry `tokens`
    are decoded. A message id can be appended again later with tokens filled in; the last copy wins.
    Only ids, timestamps, model names, token counts, and the number of tool calls are kept.
    """

    def __init__(self, path: Path, adapter_id: str, cwd: str | None = None) -> None:
        self.path = path
        self.adapter_id = adapter_id
        self.cwd = cwd
        self.session_id: str | None = None
        self.kind: str | None = None
        self._msgs: dict[str, dict] = {}

    def feed(self, lines: list[bytes]) -> None:
        from ..logcache import loads
        for raw in lines:
            if b'"tokens"' not in raw and b'"sessionId"' not in raw:
                continue
            obj = loads(raw)
            if not isinstance(obj, dict):
                continue
            if isinstance(obj.get("$set"), dict):
                st = obj["$set"]
                self._meta(st)
                for m in st.get("messages") or []:
                    self._msg(m)
                continue
            if "$rewindTo" in obj or "$patch" in obj:
                continue  # tokens already spent stay counted
            if "type" not in obj and "sessionId" in obj:
                self._meta(obj)
                continue
            self._msg(obj)

    def _meta(self, d: dict) -> None:
        if d.get("sessionId"):
            self.session_id = str(d["sessionId"])
        if d.get("kind"):
            self.kind = str(d["kind"])

    def _msg(self, m) -> None:
        if not isinstance(m, dict) or m.get("type") != "gemini" or not isinstance(m.get("tokens"), dict) or not m.get("id"):
            return
        calls = m.get("toolCalls")
        self._msgs[str(m["id"])] = {
            "ts": m.get("timestamp"), "model": m.get("model"), "tokens": dict(m["tokens"]),
            "tool_calls": len(calls) if isinstance(calls, list) else 0,
        }

    @property
    def records(self) -> list[UsageRecord]:
        return [_gemini_record(mid, m, self.session_id or self.path.stem, self.kind, self.path, self.cwd, self.adapter_id)
                for mid, m in self._msgs.items()]


def _gemini_record(mid: str, m: dict, session_id: str, kind: str | None, path: Path, cwd: str | None,
                   adapter_id: str) -> UsageRecord:
    from ..usage import normalize_usage, parse_ts, record_from_normalized
    t = m["tokens"]

    def num(key: str) -> int:
        try:
            return max(0, int(t.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    # Vertex UsageMetadata (gemini-cli v0.68.0 copies it 1:1): `input` (promptTokenCount) includes `cached`;
    # `tool` (toolUsePromptTokenCount) is input on top of it; `thoughts` is separate from `output`.
    # So the five normalized buckets add up to `total` (= prompt + candidates + tool + thoughts).
    norm = normalize_usage(
        input_tokens=num("input") + num("tool"), cache_read=num("cached"),
        output=num("output"), reasoning=num("thoughts"), input_includes_cache=True,
    )
    extra = {"path": str(path), "message_id": mid, "tool_calls": m.get("tool_calls", 0)}
    if cwd:
        extra["cwd"] = cwd
    return record_from_normalized(
        adapter_id, "log", norm, session_id=session_id, ts=parse_ts(m.get("ts")), model=m.get("model"),
        is_subagent=kind == "subagent", turns=1, extra=extra,
    )


def _parse_legacy_json(path: Path, adapter_id: str, cwd: str | None) -> list[UsageRecord]:
    """Older whole-file ConversationRecord .json sessions."""
    try:
        if path.stat().st_size > 64 * 1024 * 1024:
            return []
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        return []
    parser = GeminiChatParser(path, adapter_id, cwd)
    parser._meta(data)
    for m in data["messages"]:
        parser._msg(m)
    return parser.records


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _iter_thinking(cfg: dict):
    mc = cfg.get("modelConfigs") if isinstance(cfg.get("modelConfigs"), dict) else {}
    for key in ("overrides", "customOverrides", "customAliases"):
        block = mc.get(key)
        entries = block if isinstance(block, list) else (list(block.values()) if isinstance(block, dict) else [])
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            model = entry.get("model") or entry.get("name")
            inner = entry.get("modelConfig") if isinstance(entry.get("modelConfig"), dict) else entry
            gen = inner.get("generateContentConfig") if isinstance(inner.get("generateContentConfig"), dict) else inner
            think = gen.get("thinkingConfig") if isinstance(gen.get("thinkingConfig"), dict) else {}
            if "thinkingBudget" in think or "thinkingLevel" in think:
                yield (f"modelConfigs.{key}", think.get("thinkingBudget"), think.get("thinkingLevel"), model)
