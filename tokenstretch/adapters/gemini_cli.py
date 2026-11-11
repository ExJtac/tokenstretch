"""Doctor-only adapter for Gemini CLI."""
from __future__ import annotations

import os
import re
from pathlib import Path

from ..context import Context
from ..files import import_refs, text_if_small
from .base import ConfigLocation, DetectResult, InstructionFile, McpServer
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
    implemented_checks = frozenset({"W01", "W02", "W05", "W06", "W09", "P01", "P02", "P03"})
    config_dir_env = ("GEMINI_CLI_HOME",)

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
        if any(ctx.exists(p) for p in files.values()):
            return "subscription"
        return "unknown"

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
        findings.extend(self.privacy_checks(ctx))
        return findings

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
