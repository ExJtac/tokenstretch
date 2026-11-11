"""Doctor-only adapter for Cline."""
from __future__ import annotations

from pathlib import Path

from ..context import Context
from . import host_logs
from .base import ConfigLocation, DetectResult, InstructionFile, McpServer
from .doctor_only import (
    DoctorOnlyAdapter,
    finding,
    instruction_size_findings,
    load_json,
    mcp_servers_in,
    mcp_from_mapping,
    stat_instruction,
)


def data_dir(ctx: Context) -> Path:
    val, _ = ctx.env_or_rc("CLINE_DATA_DIR")
    if val:
        return Path(val).expanduser()
    cline_dir, _ = ctx.env_or_rc("CLINE_DIR")
    if cline_dir:
        return Path(cline_dir).expanduser() / "data"
    return ctx.home / ".cline" / "data"


def sessions_dir(ctx: Context) -> Path:
    val, _ = ctx.env_or_rc("CLINE_SESSION_DATA_DIR")
    if val:
        return Path(val).expanduser()
    return data_dir(ctx) / "sessions"


def vscode_task_roots(ctx: Context) -> list[Path]:
    """Legacy VS Code globalStorage task roots quoted in usage-logs.md (standard Code layout)."""
    plat = ctx.os_name()
    if plat.startswith("darwin"):
        base = ctx.home / "Library" / "Application Support" / "Code" / "User" / "globalStorage"
    elif plat.startswith("win"):
        appdata, _ = ctx.env_or_rc("APPDATA")
        if not appdata:
            return []
        base = Path(appdata) / "Code" / "User" / "globalStorage"
    else:
        base = ctx.home / ".config" / "Code" / "User" / "globalStorage"
    return [base / "saoudrizwan.claude-dev" / "tasks"]


def log_paths(ctx: Context) -> list[Path]:
    out: list[Path] = []
    sessions = sessions_dir(ctx)
    if sessions.is_dir():
        out.extend(sessions.glob("*/*.messages.json"))
        out.extend(sessions.glob("*/*/*.messages.json"))
    tasks = data_dir(ctx) / "tasks"
    if tasks.is_dir():
        out.extend(tasks.glob("*/ui_messages.json"))
    for gs in vscode_task_roots(ctx):
        if gs.is_dir():
            out.extend(gs.glob("*/ui_messages.json"))
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


def settings_path(ctx: Context) -> Path:
    return data_dir(ctx) / "settings" / "global-settings.json"


def mcp_path(ctx: Context) -> Path:
    return data_dir(ctx) / "settings" / "cline_mcp_settings.json"


class ClineAdapter(DoctorOnlyAdapter):
    id = "cline"
    display_name = "Cline"
    vendor_terms = "Cline"
    implemented_checks = frozenset({"W01", "W02", "W06", "W18", "P02", "P03"})
    config_dir_env = ("CLINE_DATA_DIR", "CLINE_DIR", "CLINE_SESSION_DATA_DIR")

    def _global_rules_dirs(self, ctx: Context) -> list[Path]:
        """Same Documents path on every OS."""
        out: list[Path] = []
        cline_dir, _ = ctx.env_or_rc("CLINE_DIR")
        out.append(Path(cline_dir).expanduser() / "rules" if cline_dir else ctx.home / ".cline" / "rules")
        out.append(ctx.home / "Cline" / "Rules")
        out.append(ctx.home / "Documents" / "Cline" / "Rules")
        if (ctx.os_name() or "").lower().startswith("win"):
            for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
                val, _ = ctx.env_or_rc(var)
                if val:
                    out.append(Path(val).expanduser() / "Documents" / "Cline" / "Rules")
        # De-dupe while keeping order
        seen: set[str] = set()
        uniq: list[Path] = []
        for path in out:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            uniq.append(path)
        return uniq

    def detect(self, ctx: Context) -> DetectResult:
        binary = ctx.which("cline")
        candidates = [
            settings_path(ctx),
            mcp_path(ctx),
            ctx.home / ".cline" / "hooks",
            ctx.home / ".cline",
            ctx.root / ".clinerules",
            ctx.root / ".cline" / "rules",
            data_dir(ctx),
        ]
        candidates.extend(self._global_rules_dirs(ctx))
        roots = [str(p) for p in candidates if p.exists()]
        return DetectResult(
            installed=bool(roots or binary),
            binary=str(binary) if binary else None,
            config_roots=roots,
            auth_mode="unknown",
        )

    def config_locations(self, ctx: Context) -> list[ConfigLocation]:
        locs = [
            ConfigLocation(settings_path(ctx), "settings", settings_path(ctx).is_file()),
            ConfigLocation(mcp_path(ctx), "mcp", mcp_path(ctx).is_file()),
        ]
        db = data_dir(ctx) / "db" / "sessions.db"
        # Index only; no token columns. Stat, never opened by this adapter.
        locs.append(ConfigLocation(db, "logs", ctx.exists(db), sensitive=False))
        return locs

    def _settings(self, ctx: Context) -> tuple[dict, str]:
        path = settings_path(ctx)
        if path.is_file():
            return load_json(path), str(path)
        return {}, str(path)

    def _value(self, data: dict, *names: str):
        for name in names:
            if name in data:
                return data[name], name
        return None, names[0] if names else ""

    def read_config(self, ctx: Context) -> dict:
        data, source = self._settings(ctx)
        effective: dict = {}
        # CLI flags are documented; JSON keys are read only when those names (or the normalized auto_approve) are present.
        for flag, dest in (("model", "model"), ("provider", "provider"), ("thinking", "thinking"), ("compaction", "compaction")):
            val, key = self._value(data, flag)
            if val is not None:
                effective[dest] = {"value": val, "from": f"{source}:{key}"}
        auto, akey = self._value(data, "toolAutoApprove")
        if auto is not None:
            effective["auto_approve"] = {"value": auto, "from": f"{source}:{akey}"}
        elif self.detect(ctx):
            effective["auto_approve"] = {"value": True, "from": "default true (toolAutoApprove unset)"}
        raw = []
        if Path(source).is_file():
            raw.append({"file": source, "scope": "user", "data": data})
        mcp = mcp_path(ctx)
        if mcp.is_file():
            raw.append({"file": str(mcp), "scope": "user", "data": load_json(mcp)})
        return {"effective": effective, "raw": raw}

    def list_instruction_files(self, ctx: Context, cwd: str | None = None) -> list[InstructionFile]:
        root = Path(cwd) if cwd else ctx.root
        out: list[InstructionFile] = []
        seen: set[str] = set()

        def add(path: Path, scope: str, always: bool = True) -> None:
            try:
                key = str(path.resolve())
            except OSError:
                key = str(path)
            if key in seen:
                return
            item = stat_instruction(path, scope, always)
            if item:
                seen.add(key)
                out.append(item)

        def add_dir(folder: Path, scope: str) -> None:
            if not folder.is_dir():
                return
            try:
                names = sorted(folder.iterdir())
            except OSError:
                return
            for path in names:
                if path.name == "hooks" and path.is_dir():
                    continue
                if path.is_file():
                    add(path, scope)

        # .clinerules/ or a file named .clinerules; .cline/rules/; global Rules paths (all OSes).
        clinerules = root / ".clinerules"
        if clinerules.is_dir():
            add_dir(clinerules, "project")
        elif clinerules.is_file():
            add(clinerules, "project")
        add_dir(root / ".cline" / "rules", "project")
        for folder in self._global_rules_dirs(ctx):
            add_dir(folder, "user")
        add(root / ".cursorrules", "project")
        add(root / ".windsurfrules", "project")
        add(root / "AGENTS.md", "project")
        add(ctx.home / ".agents" / "AGENTS.md", "user")
        # Leftover Roo Code files: detect and count as instruction weight (agents.md).
        roorules = root / ".roorules"
        if roorules.is_dir():
            add_dir(roorules, "project")
        elif roorules.is_file():
            add(roorules, "project")
        add_dir(root / ".roo", "project")
        return out

    def leftover_roo_paths(self, ctx: Context) -> list[Path]:
        root = ctx.root
        out: list[Path] = []
        for p in (root / ".roorules", root / ".roo"):
            if p.exists():
                out.append(p)
        return out

    def list_mcp_servers(self, ctx: Context) -> list[McpServer]:
        path = mcp_path(ctx)
        if not path.is_file():
            return []
        data = load_json(path)
        servers = mcp_servers_in(data, file=str(path), scope="user")
        if servers:
            return servers
        # If the file is a top-level map of server objects, mcp_servers_in already accepts that shape.
        if isinstance(data.get("mcpServers"), dict):
            return mcp_from_mapping(data["mcpServers"], file=str(path), scope="user")
        return []

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
        # Sessions index db/sessions.db has no token columns and is never opened here.
        # VS Code globalStorage ui_messages.json and SDK session files are read-only.
        return host_logs.collect_usage(self, ctx, self.default_log_paths(ctx), since_days=since_days)

    def checks(self, ctx: Context) -> list:
        if not self.detect(ctx):
            return []
        findings = []
        files = self.list_instruction_files(ctx)
        agents = [f for f in files if Path(f.path).name == "AGENTS.md"]
        others = [f for f in files if Path(f.path).name != "AGENTS.md"]
        findings.extend(instruction_size_findings(
            self.id, others, line_limit=200, byte_limit=25 * 1024, check_id="W01",
            slug="cline-rules-size",
            source="agents.md (.clinerules/, .cline/rules/, .cursorrules, .windsurfrules, leftover .roo/.roorules)",
        ))
        findings.extend(instruction_size_findings(
            self.id, agents, line_limit=200, byte_limit=16 * 1024, check_id="W02",
            slug="agents-md-size", source="agents.md (AGENTS.md and ~/.agents/AGENTS.md)",
        ))
        roo = self.leftover_roo_paths(ctx)
        if roo:
            findings.append(finding(
                self.id, "W01", "roo-leftover-rules", "info", "waste",
                "Leftover Roo Code rule files are present. Roo Code shut down; migrate rules to AGENTS.md (a fork may still load these).",
                "Move the rules you still want into AGENTS.md and remove .roo/ / .roorules.",
                "agents.md (Roo Code shutdown; leftover .roo/ and .roorules)",
                file=",".join(str(p) for p in roo),
            ))
        cfg = self.read_config(ctx)
        thinking = (cfg["effective"].get("thinking") or {}).get("value")
        if str(thinking).lower() in {"high", "xhigh", "max"}:
            ev = cfg["effective"]["thinking"]
            findings.append(finding(
                self.id, "W06", "effort-max", "med", "waste",
                "Cline thinking is set to the high end (--thinking high|xhigh). That burns allowance fastest.",
                "Lower --thinking (none|low|medium) for everyday work, and raise it only per task.",
                "agents.md (CLI --thinking none|low|medium|high|xhigh)",
                file=ev.get("from"), key="thinking", value=str(thinking),
            ))
        findings.extend(self._w18(ctx))
        # privacy_checks() is run separately by doctor
        return findings

    def _w18(self, ctx: Context) -> list:
        cfg = self.read_config(ctx)
        ev = cfg["effective"].get("auto_approve")
        if not ev:
            return []
        val = ev["value"]
        # CLI auto-approve defaults to true.
        if val in {False, "false", "off", 0, "0"}:
            return []
        return [finding(
            self.id, "W18", "auto-approve-default", "info", "waste",
            "Cline auto-approves actions by default (global-settings.json toolAutoApprove is true when unset). That is risky outside a sandboxed copy.",
            "Set toolAutoApprove to false in ~/.cline/data/settings/global-settings.json, or pass --auto-approve false (the flag wins and does not have to rewrite the file).",
            "Cline CLI reference; W18 auto-approve-default",
            file=ev.get("from"), key="toolAutoApprove", value="default true" if "default" in str(ev.get("from")) else str(val),
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
                    "agents.md (~/.cline/data/settings/cline_mcp_settings.json)",
                    file=srv.file, key=srv.name, value=srv.remote_url,
                ))
        sessions = sessions_dir(ctx)
        tasks = data_dir(ctx) / "tasks"
        if sessions.is_dir() or tasks.is_dir() or any(r.is_dir() for r in vscode_task_roots(ctx)):
            findings.append(finding(
                self.id, "P03", "local-plaintext-retention", "info", "privacy",
                "Cline keeps local session transcripts under ~/.cline/data/sessions/ (and legacy tasks / VS Code globalStorage).",
                "Delete old session folders if you do not want them kept. Transcripts are read-only here; db/sessions.db is never opened (it has no token columns).",
                "usage-logs.md; agents.md",
                file=str(sessions if sessions.exists() else data_dir(ctx)),
            ))
        return findings
