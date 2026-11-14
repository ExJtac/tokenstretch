"""Tier-1 adapter for GitHub Copilot CLI.

Every Copilot-specific path, setting key, env name, and log field lives in this module. Sources:
docs.github.com Copilot CLI command reference, configuration-directory reference, hooks
configuration, OpenTelemetry reference, and Chronicle, plus the shipped 1.0.109 CLI help and
typings (`copilot help billing|sessions|config`, `UsageGetMetricsResult`). Token semantics: input
includes cache read and write (`/usage` help, OTel metric docs) and reasoning is part of output.
The OTel file's per-line envelope is undocumented, so it is parsed defensively.
`config.json` can hold plaintext tokens (storeTokenPlaintext) and is never opened.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

from ..context import Context
from ..findings import UsageRecord
from ..identity import PRODUCT_NAME
from ..util import is_loopback_url
from .base import (
    RULES_START,
    Adapter,
    ConfigLocation,
    DetectResult,
    HeadlessRefused,
    HeadlessRun,
    HookPlan,
    InstallStep,
    InstructionFile,
    McpServer,
    SetupItem,
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

EVENTS_NOTE = ("input read as including cache (Copilot /usage help, OTel metric docs); "
               "UNVERIFIED for every older events.jsonl file, whose field comment doesn't say so")
OTEL_NOTE = "OTel file line envelope is UNVERIFIED (undocumented); plain spans and OTLP JSON are both accepted"
_TOP_MODEL = re.compile(r"opus|fable|gpt-6-astra", re.I)
# Recommendation only: GitHub publishes no light/mid/top list (adapter-gaps research, `copilot help config` ids).
MODEL_TIERS = {"light": "claude-haiku-5.5", "mid": "claude-sonnet-5.5", "top": "claude-opus-5.5"}
MODEL_ALTERNATIVES = {"light": ["gpt-6-luna"], "mid": ["gpt-6.1-sol"], "top": ["gpt-6-astra"]}
# Auto routing tiers (GitHub changelog 2026-09-14): efficiency = cost, balance = default, intelligence = quality.
AUTO_TIERS = {"light": "efficiency", "mid": "balance", "top": "intelligence"}
_HIGH_EFFORT = {"xhigh", "max"}


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y"}


def _editable_json(path: Path) -> dict | None:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class CopilotCliAdapter(DoctorOnlyAdapter):
    id = "copilot-cli"
    display_name = "GitHub Copilot CLI"
    vendor_terms = "GitHub"
    tier = 1
    implemented_checks = frozenset({
        "W01", "W02", "W05", "W06", "W13", "W16", "W18", "P01", "P02", "P03", "P05", "P06",
    })
    config_dir_env = ("COPILOT_HOME",)
    headless_patterns = (r"\bcopilot\b.*\s(-p|--prompt)\b",)
    bench_key_env = "COPILOT_GITHUB_TOKEN"
    watch_hint = "per-turn data needs the local OTel file export: set COPILOT_OTEL_FILE_EXPORTER_PATH"
    # session.start data.context.cwd / workspace.yaml cwd tie sessions to a repo, so --measure can scope them.

    def _home(self, ctx: Context) -> Path:
        val, _ = ctx.env_or_rc("COPILOT_HOME")
        return Path(val).expanduser() if val else ctx.home / ".copilot"

    def _user_settings(self, ctx: Context) -> Path:
        return self._home(ctx) / "settings.json"

    def _managed_settings(self, ctx: Context) -> Path | None:
        plat = ctx.os_name() if hasattr(ctx, "os_name") else ""
        if plat.startswith("darwin"):
            return Path("/Library/Application Support/GitHubCopilot/managed-settings.json")
        if plat.startswith("linux"):
            return Path("/etc/github-copilot/managed-settings.json")
        return None

    def _layers(self, ctx: Context) -> list[tuple[Path, str, dict]]:
        """Lowest first: user < repository < local < managed (cli-config-dir-reference)."""
        paths = [
            (self._user_settings(ctx), "user"),
            (ctx.root / ".github" / "copilot" / "settings.json", "project"),
            (ctx.root / ".github" / "copilot" / "settings.local.json", "local"),
        ]
        managed = self._managed_settings(ctx)
        if managed is not None:
            paths.append((managed, "managed"))
        return [(p, scope, load_json(p)) for p, scope in paths if p.is_file()]

    def _merged(self, ctx: Context) -> tuple[dict, dict]:
        merged: dict = {}
        sources: dict = {}
        for path, _scope, data in self._layers(ctx):
            for k, v in data.items():
                if k == "__error__":
                    continue
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k] = {**merged[k], **v}
                else:
                    merged[k] = v
                sources[k] = str(path)
        return merged, sources

    def config_locations(self, ctx: Context) -> list[ConfigLocation]:
        home = self._home(ctx)
        locs = [ConfigLocation(home / "config.json", "credential", ctx.exists(home / "config.json"), sensitive=True)]
        for path, _scope, _data in self._layers(ctx):
            locs.append(ConfigLocation(path, "settings", True))
        return locs

    def detect(self, ctx: Context) -> DetectResult:
        home = self._home(ctx)
        binary = ctx.which("copilot")
        roots = [str(p) for p in (home / "settings.json", home / "mcp-config.json", home / "hooks", home) if p.exists()]
        return DetectResult(
            installed=bool(roots or binary),
            binary=str(binary) if binary else None,
            config_roots=roots,
            auth_mode=self._auth(ctx),
        )

    def _auth(self, ctx: Context) -> str:
        if ctx.env_or_rc("COPILOT_PROVIDERS_CONFIG")[0] or (self._home(ctx) / "providers.json").is_file():
            return "byok"
        return "subscription" if self._home(ctx).exists() or ctx.which("copilot") else "unknown"

    def auth_mode(self, ctx: Context) -> str:
        return "subscription" if self._auth(ctx) == "subscription" else "unknown"

    def read_config(self, ctx: Context) -> dict:
        merged, sources = self._merged(ctx)
        effective: dict = {}

        def put(name: str, key: str, env: str | None = None):
            if env:
                val, where = ctx.env_or_rc(env)
                if val is not None:
                    effective[name] = {"value": val, "from": f"{where}:{env}"}
                    return
            if key in merged:
                effective[name] = {"value": merged[key], "from": f"{sources.get(key)}:{key}"}

        put("model", "model", "COPILOT_MODEL")
        if "model" not in effective:
            perms = merged.get("permissions") if isinstance(merged.get("permissions"), dict) else {}
            if perms.get("model"):
                effective["model"] = {"value": perms["model"], "from": f"{sources.get('permissions')}:permissions.model"}
        put("effort", "effortLevel")
        put("auto_tier", "autoTier")
        put("fusion_tier", "fusionTier")
        put("remote_export", "remoteExport")
        put("remote_sessions", "remoteSessions")
        put("store_token_plaintext", "storeTokenPlaintext")
        put("auto_update", "autoUpdate", "COPILOT_AUTO_UPDATE")
        put("continue_on_auto_mode", "continueOnAutoMode")
        put("permission_mode", "defaultPermissionMode")
        sub = merged.get("subagents") if isinstance(merged.get("subagents"), dict) else {}
        env_conc, where = ctx.env_or_rc("COPILOT_SUBAGENT_MAX_CONCURRENT")
        if env_conc is not None:
            effective["subagent_concurrency"] = {"value": env_conc, "from": f"{where}:COPILOT_SUBAGENT_MAX_CONCURRENT"}
        elif "maxConcurrency" in sub:
            effective["subagent_concurrency"] = {"value": sub["maxConcurrency"], "from": f"{sources.get('subagents')}:subagents.maxConcurrency"}
        effective["telemetry"] = {"value": "unknown" if not self._client_telemetry_off(ctx) else False,
                                  "from": "--disable-client-telemetry flag only (no setting exists)"}
        effective["auth_mode"] = {"value": self._auth(ctx), "from": "env names / file presence only"}
        raw = [{"file": str(p), "scope": scope, "data": data} for p, scope, data in self._layers(ctx)]
        mcp = self._home(ctx) / "mcp-config.json"
        if mcp.is_file():
            raw.append({"file": str(mcp), "scope": "user", "data": load_json(mcp)})
        return {"effective": effective, "raw": raw}

    def _client_telemetry_off(self, ctx: Context) -> bool:
        """True only if a shell alias/function in an rc file passes --disable-client-telemetry."""
        return bool(re.search(r"\bcopilot\b[^\n]*--disable-client-telemetry", ctx.rc_text or ""))

    def list_instruction_files(self, ctx: Context, cwd: str | None = None) -> list[InstructionFile]:
        root = Path(cwd) if cwd else ctx.root
        out: list[InstructionFile] = []
        for rel, scope in (
            (root / ".github" / "copilot-instructions.md", "project"),
            (root / "AGENTS.md", "project"),
            (root / "CLAUDE.md", "project"),
            (root / "GEMINI.md", "project"),
        ):
            item = stat_instruction(rel, scope, True)
            if item:
                out.append(item)
        instr_dir = root / ".github" / "instructions"
        if instr_dir.is_dir():
            try:
                names = sorted(instr_dir.iterdir())
            except OSError:
                names = []
            for path in names:
                if path.name.endswith(".instructions.md"):
                    item = stat_instruction(path, "project", True)
                    if item:
                        out.append(item)
        return out

    def list_mcp_servers(self, ctx: Context) -> list[McpServer]:
        path = self._home(ctx) / "mcp-config.json"
        if not path.is_file():
            return []
        return mcp_servers_in(load_json(path), file=str(path), scope="user")

    def hook_commands(self, ctx: Context) -> list[tuple[str, str]]:
        out = []
        files = sorted((self._home(ctx) / "hooks").glob("*.json")) + sorted((ctx.root / ".github" / "hooks").glob("*.json"))
        for path in files:
            hooks = load_json(path).get("hooks")
            for event, entries in (hooks.items() if isinstance(hooks, dict) else []):
                for h in entries if isinstance(entries, list) else []:
                    if isinstance(h, dict) and (h.get("bash") or h.get("command")):
                        out.append((str(h.get("bash") or h.get("command")), f"{path}:hooks.{event}"))
        return out

    def effective_models(self, ctx: Context) -> list[tuple[str, str]]:
        m = self.read_config(ctx)["effective"].get("model")
        return [(str(m["value"]), str(m["from"]))] if m else []

    # ---- doctor ------------------------------------------------------------------------
    def checks(self, ctx: Context) -> list:
        if not self.detect(ctx):
            return []
        findings = []
        files = self.list_instruction_files(ctx)
        agents = [f for f in files if Path(f.path).name == "AGENTS.md"]
        others = [f for f in files if Path(f.path).name != "AGENTS.md"]
        findings.extend(instruction_size_findings(
            self.id, others, line_limit=200, byte_limit=25 * 1024, check_id="W01",
            slug="copilot-instructions-size", source="agents.md (copilot-instructions.md / CLAUDE.md / GEMINI.md)",
        ))
        findings.extend(instruction_size_findings(
            self.id, agents, line_limit=200, byte_limit=16 * 1024, check_id="W02",
            slug="agents-md-size", source="agents.md (AGENTS.md; nearest one wins)",
        ))
        eff = self.read_config(ctx)["effective"]
        model = eff.get("model")
        if model and _TOP_MODEL.search(str(model["value"])):
            findings.append(finding(
                self.id, "W05", "default-model-heavy", "info", "waste",
                "Your default Copilot model is a top-tier model (Opus, Fable, or GPT-6 Astra), one of the "
                "highest-cost choices under usage-based billing.",
                "Pick a lighter default with /model (or the model setting) and switch up only for hard work.",
                "cli-config-dir-reference (model); GitHub model multipliers change, so none are hard-coded",
                file=model["from"], key="model", value=str(model["value"]),
            ))
        auto_tier = eff.get("auto_tier")
        if (model and str(model["value"]).lower() in {"auto", "fusion"} and auto_tier
                and str(auto_tier["value"]).lower() == "intelligence"):
            findings.append(finding(
                self.id, "W05", "default-model-heavy", "info", "waste",
                "Copilot Auto is set to the intelligence tier, which routes toward the most capable (costliest) models. "
                "You're charged for the model Auto picks.",
                "Use autoTier \"balance\" (the default mix) or \"efficiency\", and pick a top model by hand for hard work.",
                "cli-config-dir-reference (autoTier: efficiency / balance / intelligence); GitHub changelog 2026-09-14",
                file=auto_tier["from"], key="autoTier", value="intelligence",
            ))
        effort = eff.get("effort")
        if effort and str(effort["value"]).lower() in _HIGH_EFFORT:
            findings.append(finding(
                self.id, "W06", "effort-max", "med", "waste",
                "Your AI is set to think as hard as possible on every message. That burns your allowance fastest.",
                "Lower effortLevel (most models default to medium) and raise it only per task.",
                "cli-config-dir-reference (effortLevel: none…max)",
                file=effort["from"], key="effortLevel", value=str(effort["value"]),
            ))
        conc = eff.get("subagent_concurrency")
        try:
            conc_n = int(conc["value"]) if conc else 0
        except (TypeError, ValueError):
            conc_n = 0
        if conc_n > 8:
            findings.append(finding(
                self.id, "W13", "subagent-share", "info", "waste",
                "Many helper agents can run at once. Each one loads instructions and tools again.",
                "Lower subagents.maxConcurrency or COPILOT_SUBAGENT_MAX_CONCURRENT.",
                "cli-config-dir-reference (subagents.maxConcurrency); command reference (COPILOT_SUBAGENT_MAX_CONCURRENT)",
                file=conc["from"], key="subagents.maxConcurrency", value=conc_n,
            ))
        cont = eff.get("continue_on_auto_mode")
        if cont and _truthy(cont["value"]):
            findings.append(finding(
                self.id, "W16", "auto-continue", "info", "waste",
                "When rate-limited, Copilot switches to auto mode and retries automatically.",
                "Set continueOnAutoMode to false if you want to decide yourself.",
                "cli-config-dir-reference (continueOnAutoMode, default false)",
                file=cont["from"], key="continueOnAutoMode", value=True,
            ))
        perm = eff.get("permission_mode")
        allow_env, where = ctx.env_or_rc("COPILOT_ALLOW_ALL")
        if (perm and str(perm["value"]) == "allow-all") or _truthy(allow_env):
            findings.append(finding(
                self.id, "W18", "auto-approve-default", "info", "waste",
                "Copilot CLI is set to approve every tool call. That is risky outside a sandboxed copy.",
                "Use defaultPermissionMode \"manual\" or \"assisted\" and unset COPILOT_ALLOW_ALL.",
                "cli-config-dir-reference (defaultPermissionMode); command reference (COPILOT_ALLOW_ALL)",
                file=(perm or {}).get("from") or f"{where}:COPILOT_ALLOW_ALL", key="defaultPermissionMode",
                value=str((perm or {}).get("value") or "COPILOT_ALLOW_ALL"),
            ))
        return findings  # privacy_checks() is run separately by doctor

    def privacy_checks(self, ctx: Context) -> list:
        if not self.detect(ctx):
            return []
        findings = []
        eff = self.read_config(ctx)["effective"]
        settings = str(self._user_settings(ctx))
        if not self._client_telemetry_off(ctx):
            findings.append(finding(
                self.id, "P01", "telemetry-on", "info", "privacy",
                "Copilot CLI client telemetry is on unless you pass --disable-client-telemetry. There is no "
                "setting or env var for it, so doctor cannot see it off.",
                "Add an alias such as: alias copilot='copilot --disable-client-telemetry'. Server-side telemetry is not affected.",
                "cli-command-reference (--disable-client-telemetry)",
                file="--disable-client-telemetry", key="client telemetry", value="unknown",
            ))
        findings.extend(self._otel_findings(ctx))
        remote = eff.get("remote_export")
        rs = eff.get("remote_sessions")
        if rs and _truthy(rs["value"]):
            findings.append(finding(
                self.id, "P02", "third-party-cloud", "med", "privacy",
                "remoteSessions is on: remote control is enabled by default, which also syncs session history to your account.",
                "Set remoteSessions to false (and remoteExport to false) in ~/.copilot/settings.json.",
                "cli-config-dir-reference (remoteSessions forces export)",
                file=rs["from"], key="remoteSessions", value=True,
            ))
        elif remote is None or _truthy(remote["value"]):
            findings.append(finding(
                self.id, "P02", "third-party-cloud", "info", "privacy",
                "Copilot CLI remoteExport defaults to true, so session history may be synced to your account.",
                "Set remoteExport to false in ~/.copilot/settings.json, or pass --sync-session=off per run.",
                "cli-config-dir-reference (remoteExport defaults to true)",
                file=(remote or {}).get("from") or settings, key="remoteExport",
                value="default true" if remote is None else str(remote["value"]),
            ))
        for srv in self.list_mcp_servers(ctx):
            if srv.remote_url:
                findings.append(finding(
                    self.id, "P02", "third-party-cloud", "info", "privacy",
                    f"Remote MCP server {srv.name!r} is configured; it receives tool arguments.",
                    "Remove the remote MCP server if you did not mean to share tool arguments with it.",
                    "agents.md (~/.copilot/mcp-config.json)",
                    file=srv.file, key=srv.name, value=srv.remote_url,
                ))
        home = self._home(ctx)
        for rel, label in (("logs", "~/.copilot/logs"), ("session-state", "~/.copilot/session-state")):
            path = home / rel
            if path.exists():
                findings.append(finding(
                    self.id, "P03", "local-plaintext-retention", "info", "privacy",
                    f"Copilot CLI keeps local session data under {label}.",
                    "Delete old sessions under that directory if you do not want them kept.",
                    "Chronicle docs (session-state); cli-config-dir-reference (logs/)",
                    file=str(path),
                ))
                break
        auto = eff.get("auto_update")
        if auto is None or _truthy(auto["value"]):
            findings.append(finding(
                self.id, "P05", "startup-network", "info", "privacy",
                "Copilot CLI downloads updates at the start of each session by default (no prompt content).",
                "Set autoUpdate to false in ~/.copilot/settings.json or COPILOT_AUTO_UPDATE=false to update by hand.",
                "cli-config-dir-reference (autoUpdate, default true); command reference (COPILOT_AUTO_UPDATE)",
                file=(auto or {}).get("from") or settings, key="autoUpdate",
                value="default true" if auto is None else str(auto["value"]),
            ))
        plain = eff.get("store_token_plaintext")
        if plain and _truthy(plain["value"]):
            cfg = home / "config.json"
            findings.append(finding(
                self.id, "P06", "credential-file-exposure", "high", "privacy",
                "storeTokenPlaintext is on, so your GitHub login token can be saved in plain text in ~/.copilot/config.json.",
                "Set storeTokenPlaintext to false and sign in again so the token goes to the system keychain.",
                "cli-config-dir-reference (storeTokenPlaintext); config.json was stat'd, never opened",
                file=plain["from"], key="storeTokenPlaintext", value=True, config_json_present=ctx.exists(cfg),
            ))
        return findings

    def _otel_findings(self, ctx: Context) -> list:
        endpoint, ewhere = ctx.env_or_rc("OTEL_EXPORTER_OTLP_ENDPOINT")
        file_path, _ = ctx.env_or_rc("COPILOT_OTEL_FILE_EXPORTER_PATH")
        enabled, _ = ctx.env_or_rc("COPILOT_OTEL_ENABLED")
        exporter, _ = ctx.env_or_rc("COPILOT_OTEL_EXPORTER_TYPE")
        capture, _ = ctx.env_or_rc("OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT")
        legacy, _ = ctx.env_or_rc("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT")
        content = (str(capture).upper() in {"SPAN_ONLY", "SPAN_AND_EVENT", "EVENT_ONLY"}) if capture else _truthy(legacy)
        on = bool(endpoint or file_path or _truthy(enabled))
        if not on:
            return []
        to_file = bool(file_path) or str(exporter or "").lower() == "file"
        if not to_file and not (endpoint and is_loopback_url(str(endpoint))):
            return [finding(
                self.id, "P01", "telemetry-on", "high" if content else "med", "privacy",
                "Copilot CLI OpenTelemetry export is on and sends to an HTTP endpoint"
                + (" with prompt and response content capture on." if content else "."),
                "Unset OTEL_EXPORTER_OTLP_ENDPOINT / COPILOT_OTEL_ENABLED, or use COPILOT_OTEL_FILE_EXPORTER_PATH for a local file.",
                "opentelemetry-reference (OTel environment variables, content capture)",
                file=ewhere or "env", key="OTEL_EXPORTER_OTLP_ENDPOINT", value=str(endpoint or "default otlp-http"),
            )]
        if content:
            return [finding(
                self.id, "P03", "local-plaintext-retention", "info", "privacy",
                "Copilot CLI OpenTelemetry writes full prompts and responses locally (content capture is on).",
                "Set OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT=NO_CONTENT.",
                "opentelemetry-reference (content capture)",
                file=str(file_path or "OTLP"), key="OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT", value=str(capture or legacy),
            )]
        return []

    # ---- usage logs --------------------------------------------------------------------
    def _otel_paths(self, ctx: Context) -> list[Path]:
        out = []
        otel = self._home(ctx) / "otel"
        if otel.is_dir():
            out.extend(sorted(otel.glob("*.jsonl")))
        val, _ = ctx.env_or_rc("COPILOT_OTEL_FILE_EXPORTER_PATH")
        if val:
            p = Path(val).expanduser()
            if p.is_file() and p not in out:
                out.append(p)
        return out

    def _event_paths(self, ctx: Context) -> list[Path]:
        state = self._home(ctx) / "session-state"
        return sorted(state.glob("*/events.jsonl")) if state.is_dir() else []

    def default_log_paths(self, ctx: Context) -> list[Path]:
        return self._event_paths(ctx) + self._otel_paths(ctx)

    def sniff_log(self, path: Path, first_lines: list[str]) -> bool:
        blob = "\n".join(first_lines)
        return path.name == "events.jsonl" or "session.shutdown" in blob or "gen_ai.usage.input_tokens" in blob

    def parse_log(self, path: Path) -> list[UsageRecord]:
        if path.name == "events.jsonl":
            return parse_events(path, self.id)
        return parse_otel_file(path, self.id)

    def finish_usage_records(self, records: list[UsageRecord]) -> list[UsageRecord]:
        """Per-turn OTel chat spans win over the same session's events.jsonl shutdown totals."""
        otel_sessions = {r.session_id for r in records if (r.extra or {}).get("span") == "chat" and r.session_id}
        return [r for r in records
                if not ((r.extra or {}).get("event") == "session.shutdown" and r.session_id in otel_sessions)]

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
        self._add_cwd(ctx, out)
        return self.finish_usage_records(out)

    def _session_cwd(self, ctx: Context, session_id: str | None) -> str | None:
        if not session_id or "/" in session_id or session_id.startswith("."):
            return None
        info = workspace_info(self._home(ctx) / "session-state" / session_id / "workspace.yaml")
        return info.get("cwd") or info.get("git_root")

    def _add_cwd(self, ctx: Context, records: list[UsageRecord]) -> None:
        """OTel spans carry no cwd; take it from the session's workspace.yaml (bench --measure scoping)."""
        cache: dict = {}
        for r in records:
            if r.extra is None:
                r.extra = {}
            if r.extra.get("cwd") or not r.session_id:
                continue
            if r.session_id not in cache:
                cache[r.session_id] = self._session_cwd(ctx, r.session_id)
            if cache[r.session_id]:
                r.extra["cwd"] = cache[r.session_id]

    def watch_sessions(self, ctx: Context, limit: int = 5, max_age_s: float | None = None) -> list[dict] | None:
        """Per-turn data exists only in the OTel file export (chat spans). events.jsonl has session totals only."""
        from ..logcache import memo_by_stat
        paths = self._otel_paths(ctx)
        if not paths:
            return None
        cut = time.time() - max_age_s if max_age_s else None
        sessions: dict[str, dict] = {}
        for path in paths:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if cut is not None and mtime < cut:
                continue
            for r in memo_by_stat(path, "copilot-otel", lambda path=path: parse_otel_file(path, self.id)):
                sid = r.session_id or path.stem
                if sid not in sessions:
                    sessions[sid] = {"project": _home_label(self._session_cwd(ctx, r.session_id), ctx)}
                s = sessions[sid]
                s.update({} if "turns" in s else {
                    "adapter": self.id, "session": sid[-8:], "path": str(path),
                    "last_write": mtime, "turns": [], "clear_hint": "/clear", "compact_hint": "/compact",
                })
                s["turns"].append({"ts": r.ts, "model": r.model or "", "history_tokens": r.total_input(),
                                   "output_tokens": r.output_tokens, "tool_calls": None, "reads": []})
        out = [s for s in sessions.values() if s["turns"]]
        for s in out:
            s["turns"].sort(key=lambda t: t["ts"] or "")
        out.sort(key=lambda s: s["turns"][-1]["ts"] or "", reverse=True)
        return out[:limit]

    def live_sessions(self, ctx: Context, limit: int = 5, max_age_s: float | None = None) -> list[dict]:
        out = []
        for s in self.watch_sessions(ctx, limit=limit, max_age_s=max_age_s) or []:
            last = s["turns"][-1]
            out.append({"adapter": self.id, "session": s["session"], "project": s["project"], "model": last["model"],
                        "history_tokens": last["history_tokens"], "last_write": s["last_write"], "path": s["path"]})
        return out

    # ---- headless bench ----------------------------------------------------------------
    def headless_env(self, home: Path) -> dict[str, str]:
        return {"HOME": str(home), "COPILOT_HOME": str(home / ".copilot")}

    def apply_model_flag(self, argv: list[str], model: str) -> list[str]:
        return list(argv) + [f"--model={model}"]

    def official_command(self, prompt: str) -> list[str]:
        return ["copilot", "-p", prompt, "--output-format", "json",
                "--disable-client-telemetry", "--sync-session=off"]

    def headless_run(self, prompt: str, cwd: Path, model: str | None = None) -> HeadlessRun | None:
        argv = self.official_command(prompt)
        if model:
            argv = self.apply_model_flag(argv, model)
        return HeadlessRun(argv, cwd)

    def run_headless_task(self, ctx: Context, task: dict | None = None, opts: dict | None = None) -> dict:
        """Bench only: sandbox COPILOT_HOME, a fixed --session-id, and a local OTel file (no content capture)."""
        opts = opts or {}
        if not opts.get("bench"):
            raise HeadlessRefused("run_headless_task refused without --bench")
        from ..bench import default_headless_runner
        runner = opts.get("runner") or default_headless_runner()
        sandbox = Path(opts.get("sandboxDir") or opts.get("sandbox_dir") or ctx.root)
        home = Path(opts.get("home") or ctx.home)
        try:
            if home.resolve() == Path(os.path.expanduser("~")).resolve():
                raise HeadlessRefused("refusing to run the Copilot bench against the real HOME")
        except OSError:
            raise HeadlessRefused("could not resolve the bench HOME")
        task = task or {}
        spec = self.headless_run(task.get("prompt") or "", sandbox, task.get("model"))
        sid = str(uuid.uuid4())
        otel = home / ".copilot" / "otel" / f"bench-{sid}.jsonl"
        otel.parent.mkdir(parents=True, exist_ok=True)
        usage_file = home / ".copilot" / f"bench-usage-{sid}.json"
        argv = list(spec.argv) + ["--session-id", sid, "--usage-output-file", str(usage_file)]
        env = {
            "TERM": "dumb", **self.headless_env(home),
            "COPILOT_OTEL_FILE_EXPORTER_PATH": str(otel), "COPILOT_OTEL_EXPORTER_TYPE": "file",
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT": "NO_CONTENT",
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "false",
        }
        ran = runner.run(argv, cwd=spec.cwd, env=env, timeout=int(opts.get("timeoutS") or 120))
        task_id = task.get("id") or task.get("task_id") or "task"
        if ran.usage is not None:
            usage = list(ran.usage)
        else:
            # --usage-output-file (UsageGetMetricsResult) first; then OTel chat spans; then events.jsonl.
            usage = parse_usage_metrics(usage_file, self.id, sid) if usage_file.is_file() else []
            if not usage:
                usage = [r for r in (parse_otel_file(otel, self.id) if otel.is_file() else []) if r.session_id == sid]
            if not usage:
                ev = home / ".copilot" / "session-state" / sid / "events.jsonl"
                usage = parse_events(ev, self.id) if ev.is_file() else []
            for r in usage:
                r.source, r.task_id = "headless", task_id
        return {"exitCode": ran.exit_code, "usage": usage, "rawOutput": ran.stdout or "", "stderr": ran.stderr or ""}

    def parse_headless_output(self, stdout: str, task_id: str) -> UsageRecord | None:
        # The -p JSON `result.usage` has premium requests and durations, no tokens (CLI 1.0.109);
        # tokens come from --usage-output-file instead.
        return None

    # ---- install / setup ---------------------------------------------------------------
    def install_hooks(self, ctx: Context, plan: HookPlan | dict | None = None, *, dry_run: bool = True) -> dict:
        """Nothing to wire natively: sessionStart output only feeds the model (extra tokens), and the
        read gate has no native deny list. RTK is wired by its own `rtk init -g --copilot` step."""
        return {"changes": [], "backup_dir": ""}

    def install_steps(self, tool_id: str, ctx: Context, options: dict) -> list[InstallStep] | None:
        if tool_id == "rtk-oss":
            return [InstallStep("Wire the OSS command-output hook for Copilot CLI (preToolUse deny-with-suggestion; "
                                "Copilot CLI cannot rewrite commands, so each filtered command costs one extra turn).",
                                command=["rtk", "init", "-g", "--copilot"], manual=True)]
        return None

    def compact_file(self, ctx: Context) -> Path | None:
        return ctx.root / ".github" / "copilot-instructions.md"   # never created: only an existing file is edited

    def compact_also_loads(self, ctx: Context) -> list[Path]:
        return [ctx.root / n for n in ("AGENTS.md", "CLAUDE.md", "GEMINI.md")]

    def setup_items(self, ctx: Context) -> list[SetupItem]:
        if not self.detect(ctx) or ctx.root.resolve() == ctx.home.resolve():
            return []
        if any(_has_rules(ctx.root / n) for n in ("AGENTS.md", "CLAUDE.md", "GEMINI.md")):
            return []  # Copilot already loads a rules block from one of these
        path = ctx.root / ".github" / "copilot-instructions.md"
        if not path.is_file():
            return []  # never create a new always-loaded instruction file
        new = add_rules_block(path.read_text(encoding="utf-8", errors="replace"), rules_block("a lighter model", "/clear"))
        if new is None:
            return []
        return [SetupItem(
            id="copilot-rules-block", adapter_id=self.id,
            title="Add 5 short credit-saving rules to .github/copilot-instructions.md",
            why="Copilot CLI re-sends the conversation each step. Fresh tasks, narrow reads, and short replies keep that small.",
            changes=[{"path": str(path), "kind": "modify", "content": new, "key": "credit-rules block",
                      "reason": "append a marked rules block (about 0.7 KB)"}],
            done="added credit-saving rules to .github/copilot-instructions.md",
        )]

    # ---- model picker ------------------------------------------------------------------
    def model_tiers(self) -> dict[str, str]:
        return dict(MODEL_TIERS)

    def model_tier_alternatives(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in MODEL_ALTERNATIVES.items()}

    def model_tier_note(self) -> str:
        return ("a recommended mapping, not a GitHub list; with model \"auto\", "
                "autoTier efficiency / balance / intelligence plays the same role")

    def is_top_model(self, model: str) -> bool:
        return bool(_TOP_MODEL.search(model or ""))

    def model_switch_hint(self, model: str) -> str:
        return "/model"

    def plan_default_model(self, ctx: Context, tier: str) -> SetupItem | None:
        """Set `model` in the user settings.json, or `autoTier` when the model is Auto. Plans only."""
        target = MODEL_TIERS.get(tier)
        if not target:
            return None
        path = self._user_settings(ctx)
        data = _editable_json(path)
        if data is None:
            return None  # unreadable or JSONC with comments: never overwrite
        eff = self.read_config(ctx)["effective"]
        current = eff.get("model")
        cur_val = str(current["value"]) if current else ""
        notes = [f"This tier list is {self.model_tier_note()}.",
                 "Switch for one session with `/model` or `copilot --model NAME`; this only changes the default."]
        if cur_val.lower() in {"auto", "fusion"}:
            key, value = "autoTier", AUTO_TIERS[tier]
            cur_tier = eff.get("auto_tier")
            if cur_tier and str(cur_tier["value"]).lower() == value:
                return None
            reason = f"autoTier: {cur_tier['value'] if cur_tier else '(unset)'} -> {value}"
            title = f"Set Copilot Auto's routing tier to {value} ({tier} tier)"
            why = f"Your default model is Auto, so the {tier} tier maps to autoTier \"{value}\". Auto stays on."
        else:
            key, value = "model", target
            if cur_val == target:
                return None
            reason = f"model: {cur_val or '(unset)'} -> {target}"
            title = f"Set the default Copilot CLI model to {target} ({tier} tier)"
            why = (f"Your default is {cur_val} (from {current['from'].rsplit(':', 1)[0]}). " if current else
                   "No default model is set. ") + f"New sessions will start on {target}."
        env_model, env_where = ctx.env_or_rc("COPILOT_MODEL")
        if env_model:
            notes.append(f"COPILOT_MODEL={env_model} in {env_where} overrides this setting. "
                         "Remove it yourself; shell files are never edited.")
        for p, scope, layer in self._layers(ctx):
            if p != path and key in layer:
                notes.append(f"{p} ({scope}) also sets {key} and wins over the user file.")
        new = json.loads(json.dumps(data))
        new[key] = value
        return SetupItem(
            id="copilot-default-model", adapter_id=self.id, title=title, why=why,
            changes=[{"path": str(path), "kind": "modify" if path.is_file() else "create",
                      "content": json.dumps(new, indent=2) + "\n", "key": key, "reason": reason}],
            done=f"set {key} to {value}", notes=notes,
        )


def _has_rules(path: Path) -> bool:
    from ..files import text_if_small
    text = text_if_small(path) if path.is_file() else None
    return bool(text and RULES_START in text)


def _home_label(cwd: str | None, ctx: Context) -> str:
    if not cwd:
        return ""
    home = str(ctx.home).rstrip("/")
    return "~" + cwd[len(home):] if home and (cwd == home or cwd.startswith(home + "/")) else cwd


def workspace_info(path: Path) -> dict[str, str]:
    """Top-level scalar keys of a session's workspace.yaml (`cwd`, `git_root`, `repository`, `branch`).

    No YAML library: only `key: value` lines at column 0 are read; anything else is ignored.
    """
    from ..files import text_if_small
    text = text_if_small(path) if path.is_file() else None
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        m = re.match(r"^(cwd|git_root|repository|branch|host_type):[ \t]*(.*?)[ \t]*$", line)
        if not m or not m.group(2):
            continue
        val = m.group(2)
        if val[0] == '"':
            try:
                val = json.loads(val)
            except ValueError:
                continue
        elif val[0] == "'" and val.endswith("'") and len(val) > 1:
            val = val[1:-1].replace("''", "'")
        out[m.group(1)] = str(val)
    return out


def _metric_record(adapter_id: str, model: str, u: dict, **kw) -> UsageRecord:
    from ..usage import normalize_usage, record_from_normalized

    def num(k):
        try:
            return max(0, int(u.get(k) or 0))
        except (TypeError, ValueError):
            return 0
    norm = normalize_usage(
        input_tokens=num("inputTokens"), cache_read=num("cacheReadTokens"), cache_write=num("cacheWriteTokens"),
        output=num("outputTokens"), reasoning=num("reasoningTokens"),
        input_includes_cache=True, reasoning_in_output=True,
    )
    return record_from_normalized(adapter_id, "log", norm, model=str(model), **kw)


def parse_events(path: Path, adapter_id: str) -> list[UsageRecord]:
    """events.jsonl `session.shutdown` totals per model, tagged with the `session.start` working directory."""
    from ..logcache import loads, read_lines_from
    from ..usage import parse_ts
    out: list[UsageRecord] = []
    sid = path.parent.name
    ctx_cwd: list[str] = []

    def feed(lines):
        for raw in lines:
            if b"session.shutdown" not in raw and b"session.start" not in raw:
                continue
            obj = loads(raw)
            if not isinstance(obj, dict):
                continue
            data = obj.get("data") if isinstance(obj.get("data"), dict) else {}
            if obj.get("type") == "session.start":
                context = data.get("context") if isinstance(data.get("context"), dict) else {}
                cwd = context.get("cwd") or context.get("gitRoot")
                if isinstance(cwd, str) and cwd and not ctx_cwd:
                    ctx_cwd.append(cwd)
                continue
            if obj.get("type") != "session.shutdown":
                continue
            metrics = data.get("modelMetrics") if isinstance(data.get("modelMetrics"), dict) else {}
            for model, entry in metrics.items():
                entry = entry if isinstance(entry, dict) else {}
                reqs = (entry.get("requests") or {}).get("count") if isinstance(entry.get("requests"), dict) else 0
                extra = {"path": str(path), "event": "session.shutdown"}
                if ctx_cwd:
                    extra["cwd"] = ctx_cwd[0]
                out.append(_metric_record(
                    adapter_id, model, entry.get("usage") if isinstance(entry.get("usage"), dict) else {},
                    session_id=sid, ts=parse_ts(obj.get("timestamp")), turns=int(reqs or 0), extra=extra,
                    notes=[EVENTS_NOTE],
                ))
    try:
        read_lines_from(path, 0, feed)
    except OSError:
        return []
    if not ctx_cwd:
        info = workspace_info(path.parent / "workspace.yaml")
        cwd = info.get("cwd") or info.get("git_root")
        for r in out:
            if cwd:
                r.extra["cwd"] = cwd
    return out


def parse_usage_metrics(path: Path, adapter_id: str, session_id: str | None = None) -> list[UsageRecord]:
    """`--usage-output-file` JSON (UsageGetMetricsResult): one record per model in top-level modelMetrics.

    agentMetrics repeats the same numbers per agent, so it is not added again.
    """
    from ..usage import parse_ts
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    metrics = data.get("modelMetrics") if isinstance(data.get("modelMetrics"), dict) else {}
    out = []
    for model, entry in metrics.items():
        entry = entry if isinstance(entry, dict) else {}
        reqs = entry.get("requests") if isinstance(entry.get("requests"), dict) else {}
        out.append(_metric_record(
            adapter_id, model, entry.get("usage") if isinstance(entry.get("usage"), dict) else {},
            session_id=session_id, ts=parse_ts(data.get("sessionStartTime")), turns=int(reqs.get("count") or 0),
            extra={"path": str(path), "usage_file": True},
        ))
    return out


def _flat_attrs(attrs) -> dict:
    """OTel attributes as a dict: plain mapping, or OTLP JSON [{key, value: {intValue|stringValue|...}}]."""
    if isinstance(attrs, dict):
        return attrs
    out = {}
    if isinstance(attrs, list):
        for a in attrs:
            if isinstance(a, dict) and "key" in a:
                v = a.get("value")
                if isinstance(v, dict):
                    v = next(iter(v.values()), None) if v else None
                out[str(a["key"])] = v
    return out


def _iter_spans(obj):
    if isinstance(obj, dict):
        if "attributes" in obj and ("name" in obj or "spanId" in obj or "traceId" in obj):
            yield obj
        for k in ("resourceSpans", "scopeSpans", "spans"):
            for child in obj.get(k) or []:
                yield from _iter_spans(child)
    elif isinstance(obj, list):
        for child in obj:
            yield from _iter_spans(child)


def _span_ts(span: dict, attrs: dict) -> str | None:
    from datetime import datetime, timezone
    for key in ("startTimeUnixNano", "endTimeUnixNano"):
        if span.get(key):
            try:
                return datetime.fromtimestamp(int(span[key]) / 1e9, timezone.utc).isoformat().replace("+00:00", "Z")
            except (TypeError, ValueError, OSError):
                pass
    st = span.get("startTime")
    if isinstance(st, list) and st:
        try:
            return datetime.fromtimestamp(float(st[0]) + float(st[1] if len(st) > 1 else 0) / 1e9,
                                          timezone.utc).isoformat().replace("+00:00", "Z")
        except (TypeError, ValueError, OSError):
            pass
    from ..usage import parse_ts
    return parse_ts(span.get("timestamp") or st)


def parse_otel_file(path: Path, adapter_id: str) -> list[UsageRecord]:
    """`chat` spans (one per model request) from the COPILOT_OTEL_FILE_EXPORTER_PATH JSON-lines file.

    Attribute names are documented (opentelemetry-reference); the exact line envelope is not, so
    both a plain span object and the OTLP JSON envelope are accepted. `invoke_agent` spans are
    skipped (they repeat the chat totals). No content attributes are read.
    """
    from ..logcache import loads, read_lines_from
    from ..usage import normalize_usage, record_from_normalized
    out: list[UsageRecord] = []

    def feed(lines):
        for raw in lines:
            if b"gen_ai.usage" not in raw:
                continue
            obj = loads(raw)
            for span in _iter_spans(obj):
                attrs = _flat_attrs(span.get("attributes"))
                op = attrs.get("gen_ai.operation.name") or span.get("name")
                if op != "chat" and not str(span.get("name") or "").startswith("chat"):
                    continue

                def num(k):
                    try:
                        return int(attrs.get(k) or 0)
                    except (TypeError, ValueError):
                        return 0
                norm = normalize_usage(
                    input_tokens=num("gen_ai.usage.input_tokens"),
                    cache_read=num("gen_ai.usage.cache_read.input_tokens"),
                    cache_write=num("gen_ai.usage.cache_write.input_tokens") or num("gen_ai.usage.cache_creation.input_tokens"),
                    output=num("gen_ai.usage.output_tokens"),
                    reasoning=num("gen_ai.usage.reasoning.output_tokens"),
                    input_includes_cache=True, reasoning_in_output=True,
                )
                out.append(record_from_normalized(
                    adapter_id, "log", norm, session_id=attrs.get("gen_ai.conversation.id"),
                    model=attrs.get("gen_ai.response.model") or attrs.get("gen_ai.request.model"),
                    ts=_span_ts(span, attrs), turns=1, notes=[OTEL_NOTE],
                    extra={"path": str(path), "span": "chat",
                           "compaction": attrs.get("github.copilot.request.type") == "compaction"},
                ))
    try:
        read_lines_from(path, 0, feed)
    except OSError:
        return []
    return out
