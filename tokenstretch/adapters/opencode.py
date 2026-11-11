"""Doctor-only adapter for OpenCode v2."""
from __future__ import annotations

import json
import re
from pathlib import Path

from ..context import Context
from . import host_logs
from .base import ConfigLocation, DetectResult, InstructionFile, McpServer
from .doctor_only import (
    DoctorOnlyAdapter,
    finding,
    instruction_size_findings,
    mcp_from_mapping,
    stat_instruction,
)


def data_root(ctx: Context) -> Path:
    """Official data dir: $XDG_DATA_HOME/opencode, else ~/.local/share/opencode (all OSes)."""
    xdg, _ = ctx.env_or_rc("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "opencode"
    return ctx.home / ".local" / "share" / "opencode"


def user_config_dir(ctx: Context) -> Path:
    """$OPENCODE_CONFIG_DIR, else $XDG_CONFIG_HOME/opencode, else ~/.config/opencode."""
    override, _ = ctx.env_or_rc("OPENCODE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    xdg, _ = ctx.env_or_rc("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "opencode"
    return ctx.home / ".config" / "opencode"


def db_path(ctx: Context) -> Path | None:
    override, _ = ctx.env_or_rc("OPENCODE_DB")
    if override:
        if override == ":memory:":
            return None
        return Path(override).expanduser()
    return data_root(ctx) / "opencode.db"


def config_files(ctx: Context) -> list[tuple[Path, str]]:
    # Load order is lowest to highest: user opencode.json, then user opencode.jsonc
    # (jsonc wins if both exist), then project json, then project jsonc (project
    # overrides user). config.json is not a current name.
    out: list[tuple[Path, str]] = []
    seen: set[str] = set()
    candidates: list[tuple[Path, str]] = [
        (user_config_dir(ctx) / "opencode.json", "user"),
        (user_config_dir(ctx) / "opencode.jsonc", "user"),
    ]
    explicit, _ = ctx.env_or_rc("OPENCODE_CONFIG")
    if explicit:
        candidates.append((Path(explicit).expanduser(), "explicit"))
    candidates.extend((
        (ctx.root / "opencode.json", "project"),
        (ctx.root / "opencode.jsonc", "project"),
    ))
    for path, scope in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            out.append((path, scope))
    return out


def log_paths(ctx: Context) -> list[Path]:
    out: list[Path] = []
    db = db_path(ctx)
    if db is not None and db.is_file():
        out.append(db)
    legacy = data_root(ctx) / "storage" / "message"
    if legacy.is_dir():
        out.extend(legacy.glob("*/*.json"))
    seen: set[Path] = set()
    uniq: list[Path] = []
    for p in out:
        try:
            key = p.resolve()
        except OSError:
            key = p
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    return uniq


def load_jsonc(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    # Strip comments without touching // inside strings (e.g. https:// URLs).
    text = _strip_jsonc(raw)
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _strip_jsonc(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    escape = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i = min(i + 2, n)
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    stripped = "".join(out)
    prev = None
    while prev != stripped:
        prev = stripped
        stripped = re.sub(r",(\s*[}\]])", r"\1", stripped)
    return stripped


class OpenCodeAdapter(DoctorOnlyAdapter):
    id = "opencode"
    display_name = "OpenCode"
    vendor_terms = "OpenCode (provider-dependent)"
    implemented_checks = frozenset({"W01", "W02", "T01", "T04", "P02", "P03"})

    def detect(self, ctx: Context) -> DetectResult:
        binary = ctx.which("opencode")
        roots = []
        for path, _scope in config_files(ctx):
            roots.append(str(path))
        user = user_config_dir(ctx)
        for p in (user / "AGENTS.md", user / "plugins"):
            if p.exists():
                roots.append(str(p))
        db = db_path(ctx)
        log = data_root(ctx) / "log" / "opencode.log"
        for p in (db, log):
            if p is not None and p.exists():
                roots.append(str(p))
        # Dedup while preserving order
        seen: set[str] = set()
        uniq = []
        for r in roots:
            if r not in seen:
                seen.add(r)
                uniq.append(r)
        return DetectResult(
            installed=bool(uniq or binary),
            binary=str(binary) if binary else None,
            config_roots=uniq,
            auth_mode="unknown",
        )

    def config_locations(self, ctx: Context) -> list[ConfigLocation]:
        locs = [ConfigLocation(path, "settings", True) for path, _ in config_files(ctx)]
        agents = user_config_dir(ctx) / "AGENTS.md"
        locs.append(ConfigLocation(agents, "memory", agents.is_file()))
        db = db_path(ctx)
        if db is not None:
            locs.append(ConfigLocation(db, "logs", db.is_file()))
        return locs

    def _merged(self, ctx: Context) -> tuple[dict, str]:
        # User .jsonc wins over .json in the same dir; project overrides user (adapter-gaps-v03.md).
        merged: dict = {}
        source = ""
        for path, scope in config_files(ctx):
            merged.update(load_jsonc(path))
            source = f"{path} ({scope})"
        return merged, source

    def read_config(self, ctx: Context) -> dict:
        merged, source = self._merged(ctx)
        effective: dict = {}
        if merged.get("model") is not None:
            effective["model"] = {"value": merged["model"], "from": f"{source}:model"}
        compaction = merged.get("compaction") if isinstance(merged.get("compaction"), dict) else {}
        if "auto" in compaction:
            effective["compaction"] = {"value": compaction["auto"], "from": f"{source}:compaction.auto"}
        elif self.detect(ctx):
            effective["compaction"] = {"value": True, "from": "default true (docs/compaction)"}
        if "warming" in merged:
            effective["warming"] = {"value": merged["warming"], "from": f"{source}:warming"}
        raw = [{"file": str(path), "scope": scope, "data": load_jsonc(path)} for path, scope in config_files(ctx)]
        return {"effective": effective, "raw": raw}

    def list_instruction_files(self, ctx: Context, cwd: str | None = None) -> list[InstructionFile]:
        root = Path(cwd) if cwd else ctx.root
        out: list[InstructionFile] = []
        seen: set[str] = set()

        def add(path: Path, scope: str) -> None:
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key in seen:
                return
            item = stat_instruction(path, scope, True)
            if item:
                seen.add(key)
                out.append(item)

        add(user_config_dir(ctx) / "AGENTS.md", "user")
        add(root / "AGENTS.md", "project")
        # Documented fallbacks (https://opencode.ai/docs/rules).
        add(ctx.home / ".claude" / "CLAUDE.md", "user")
        add(root / "CLAUDE.md", "project")
        skills = ctx.home / ".claude" / "skills"
        if skills.is_dir():
            try:
                names = sorted(skills.iterdir())
            except OSError:
                names = []
            for path in names:
                if path.is_file() and path.suffix == ".md":
                    item = stat_instruction(path, "user", False)
                    if item:
                        out.append(item)
        return out

    def list_mcp_servers(self, ctx: Context) -> list[McpServer]:
        out: list[McpServer] = []
        for path, scope in config_files(ctx):
            data = load_jsonc(path)
            mcp = data.get("mcp")
            if isinstance(mcp, dict):
                out.extend(mcp_from_mapping(mcp, file=str(path), scope=scope))
        return out

    def default_log_paths(self, ctx: Context) -> list[Path]:
        return log_paths(ctx)

    def sniff_log(self, path: Path, first_lines: list[str]) -> bool:
        return host_logs.sniff(path, first_lines)

    def parse_log(self, path: Path) -> list:
        recs, notices = host_logs.parse_path(path, self.id)
        existing = getattr(self, "last_log_notices", None)
        if existing is None:
            self.last_log_notices = list(notices)
        else:
            existing.extend(notices)
        return recs

    def read_usage_logs(self, ctx: Context, since_days: int = 30) -> list:
        return host_logs.collect_usage(self, ctx, self.default_log_paths(ctx), since_days=since_days)

    def checks(self, ctx: Context) -> list:
        if not self.detect(ctx):
            return []
        findings = []
        files = self.list_instruction_files(ctx)
        agents = [f for f in files if Path(f.path).name == "AGENTS.md"]
        others = [f for f in files if Path(f.path).name != "AGENTS.md" and f.always_loaded]
        findings.extend(instruction_size_findings(
            self.id, others, line_limit=200, byte_limit=25 * 1024, check_id="W01",
            slug="opencode-instructions-size", source="agents.md (AGENTS.md + CLAUDE.md fallback)",
        ))
        findings.extend(instruction_size_findings(
            self.id, agents, line_limit=200, byte_limit=16 * 1024, check_id="W02",
            slug="agents-md-size", source="agents.md (project AGENTS.md and ~/.config/opencode/AGENTS.md)",
        ))
        has_agents = any(Path(f.path).name == "AGENTS.md" for f in files)
        has_claude = any(Path(f.path).name == "CLAUDE.md" for f in files)
        if has_agents and has_claude:
            findings.append(finding(
                self.id, "W01", "opencode-claude-md-fallback", "info", "waste",
                "OpenCode loads AGENTS.md and also falls back to CLAUDE.md, so both may be sent as standing instruction.",
                "Keep rules in one AGENTS.md and thin or remove the CLAUDE.md copy if you do not need the fallback.",
                "agents.md (https://opencode.ai/docs/rules; CLAUDE.md fallback double-loading)",
            ))
        findings.extend(self._t01(ctx))
        findings.extend(self._t04(ctx))
        # privacy_checks() is run separately by doctor
        return findings

    # Plugins that reuse Gemini CLI's Google OAuth. Only names with a current source are listed:
    # opencode-gemini-auth's README says it "mirrors the official Gemini CLI OAuth flow" (adapter-gaps v0.4).
    GEMINI_OAUTH_PLUGINS = ("opencode-gemini-auth",)

    def _t01(self, ctx: Context) -> list:
        """High ToS risk: an OpenCode plugin that piggybacks Gemini CLI OAuth (config ids and plugin file names only)."""
        hits: list[tuple[str, str]] = []
        for path, _scope in config_files(ctx):
            plugins = load_jsonc(path).get("plugin")
            for p in plugins if isinstance(plugins, list) else []:
                name = p[0] if isinstance(p, list) and p else p
                if isinstance(name, str) and any(g in name.lower() for g in self.GEMINI_OAUTH_PLUGINS):
                    hits.append((str(path), name))
        for d in (user_config_dir(ctx) / "plugins", ctx.root / ".opencode" / "plugins"):
            if d.is_dir():
                for f in sorted(d.iterdir()):
                    if any(g in f.name.lower() for g in self.GEMINI_OAUTH_PLUGINS):
                        hits.append((str(f), f.name))
        if not hits:
            return []
        return [finding(
            self.id, "T01", "tos-gemini-oauth-reuse", "high", "tos",
            "OpenCode has a plugin that signs in with Gemini CLI's Google OAuth "
            f"({', '.join(dict.fromkeys(n for _, n in hits))}). Google's Gemini CLI terms call using third-party "
            "tools on that OAuth a violation that can get the account suspended.",
            "Remove the plugin and use a Gemini API key (Google AI Studio or Vertex AI), the supported path for "
            "third-party agents. Credentials are not read by this check.",
            "Gemini CLI docs/resources/tos-privacy.md and faq.md (v0.68.0); opencode-gemini-auth README",
            file=", ".join(dict.fromkeys(p for p, _ in hits)), key="plugin", value=hits[0][1],
        )]

    def _t04(self, ctx: Context) -> list:
        cfg = self.read_config(ctx)
        ev = cfg["effective"].get("warming")
        if not ev:
            return []
        warm = ev["value"]
        on = False
        if isinstance(warm, dict):
            # Object shape is not quoted; any configured warming other than an explicit false is treated as on.
            on = any(v not in {False, None, "false", "off", 0, "0"} for v in warm.values()) or (
                not warm
            )
            if warm.get("enabled") in {False, "false", "off", 0, "0"}:
                on = False
        else:
            on = warm not in {False, None, "false", "off", 0, "0", ""}
        if not on:
            return []
        return [finding(
            self.id, "T04", "tos-warmup", "med", "tos",
            "OpenCode warming is on. It sends periodic model requests to keep a provider cache warm, which costs money on an API key and is a warm-up pattern on a subscription.",
            "Turn warming off in opencode.json(c). This kit will never enable it.",
            "agents.md (https://opencode.ai/docs/warming); doctor-checks.md T04",
            file=ev.get("from"), key="warming", value=str(warm),
        )]

    def privacy_checks(self, ctx: Context) -> list:
        if not self.detect(ctx):
            return []
        findings = []
        for srv in self.list_mcp_servers(ctx):
            if srv.remote_url:
                findings.append(finding(
                    self.id, "P02", "third-party-cloud", "info", "privacy",
                    f"Remote MCP server {srv.name!r} is configured; it receives tool arguments.",
                    "Remove the remote MCP server if you did not mean to share tool arguments with it.",
                    "agents.md (mcp key in opencode.json(c))",
                    file=srv.file, key=srv.name, value=srv.remote_url,
                ))
        data = data_root(ctx)
        log = data / "log" / "opencode.log"
        db = db_path(ctx)
        if (db is not None and db.is_file()) or log.is_file() or (data / "storage").is_dir():
            findings.append(finding(
                self.id, "P03", "local-plaintext-retention", "info", "privacy",
                "OpenCode keeps local session data under $XDG_DATA_HOME/opencode or ~/.local/share/opencode (opencode.db, and legacy storage/message JSON).",
                "Delete old session data under that directory if you do not want it kept. This kit opens opencode.db read-only (mode=ro).",
                "usage-logs.md; agents.md (https://opencode.ai/docs/troubleshooting)",
                file=str(db or log or data),
            ))
        return findings
