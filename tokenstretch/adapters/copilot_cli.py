"""Doctor-only adapter for GitHub Copilot CLI."""
from __future__ import annotations

from pathlib import Path

from ..context import Context
from .base import DetectResult, InstructionFile, McpServer
from .doctor_only import (
    DoctorOnlyAdapter,
    finding,
    instruction_size_findings,
    load_json,
    mcp_servers_in,
    stat_instruction,
)


class CopilotCliAdapter(DoctorOnlyAdapter):
    id = "copilot-cli"
    display_name = "GitHub Copilot CLI"
    vendor_terms = "GitHub"
    implemented_checks = frozenset({"W01", "W02", "W06", "P02", "P03"})
    config_dir_env = ("COPILOT_HOME",)
    # settings.json keys for client telemetry and --fleet are not quoted — unverified.

    def _home(self, ctx: Context) -> Path:
        val, _ = ctx.env_or_rc("COPILOT_HOME")
        return Path(val).expanduser() if val else ctx.home / ".copilot"

    def detect(self, ctx: Context) -> DetectResult:
        home = self._home(ctx)
        binary = ctx.which("copilot")
        roots = [str(p) for p in (home / "settings.json", home / "mcp-config.json", home / "hooks", home) if p.exists()]
        auth = "unknown"
        if ctx.env_or_rc("COPILOT_PROVIDERS_CONFIG")[0]:
            auth = "byok"
        return DetectResult(
            installed=bool(roots or binary),
            binary=str(binary) if binary else None,
            config_roots=roots,
            auth_mode=auth,
        )

    def read_config(self, ctx: Context) -> dict:
        settings = self._home(ctx) / "settings.json"
        data = load_json(settings) if settings.is_file() else {}
        effective: dict = {}
        if data.get("model") is not None:
            effective["model"] = {"value": data["model"], "from": str(settings)}
        # Effort is documented as --effort/--reasoning-effort; read those keys only if present.
        for key in ("effort", "reasoning-effort", "reasoning_effort"):
            if data.get(key) is not None:
                effective["effort"] = {"value": data[key], "from": f"{settings}:{key}"}
                break
        if "remoteExport" in data:
            effective["remote_export"] = {"value": data["remoteExport"], "from": f"{settings}:remoteExport"}
        raw = []
        if settings.is_file():
            raw.append({"file": str(settings), "scope": "user", "data": data})
        for proj, scope in (
            (ctx.root / ".github" / "copilot" / "settings.json", "project"),
            (ctx.root / ".github" / "copilot" / "settings.local.json", "local"),
        ):
            if proj.is_file():
                pdata = load_json(proj)
                raw.append({"file": str(proj), "scope": scope, "data": pdata})
                if "remoteExport" in pdata:
                    effective["remote_export"] = {"value": pdata["remoteExport"], "from": f"{proj}:remoteExport"}
                if pdata.get("effort") or pdata.get("reasoning-effort"):
                    pass
        mcp = self._home(ctx) / "mcp-config.json"
        if mcp.is_file():
            raw.append({"file": str(mcp), "scope": "user", "data": load_json(mcp)})
        return {"effective": effective, "raw": raw}

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
                if path.suffix == ".md" and path.name.endswith(".instructions.md"):
                    item = stat_instruction(path, "project", True)
                    if item:
                        out.append(item)
        return out

    def list_mcp_servers(self, ctx: Context) -> list[McpServer]:
        path = self._home(ctx) / "mcp-config.json"
        if not path.is_file():
            return []
        return mcp_servers_in(load_json(path), file=str(path), scope="user")

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
        cfg = self.read_config(ctx)
        effort = (cfg["effective"].get("effort") or {}).get("value")
        if str(effort).lower() in {"max", "xhigh", "ultra"}:
            ev = cfg["effective"]["effort"]
            findings.append(finding(
                self.id, "W06", "effort-max", "med", "waste",
                "Your AI is set to think as hard as possible on every message. That burns your allowance fastest.",
                "Lower --effort / --reasoning-effort from max, or change the saved setting, and raise it only per task.",
                "agents.md (--effort/--reasoning-effort low…max)",
                file=ev.get("from"), key="effort", value=str(effort),
            ))
        findings.extend(self.privacy_checks(ctx))
        return findings

    def privacy_checks(self, ctx: Context) -> list:
        if not self.detect(ctx):
            return []
        findings = []
        cfg = self.read_config(ctx)
        remote = cfg["effective"].get("remote_export")
        # Defaults to true when unset (adapter-gaps-v02.md).
        value = True if not remote else remote.get("value")
        if value is True or value is None or str(value).lower() in {"true", "1"}:
            findings.append(finding(
                self.id, "P02", "third-party-cloud", "info", "privacy",
                "Copilot CLI remoteExport defaults to true, so session history may be synced to your account.",
                "Set remoteExport to false in ~/.copilot/settings.json, or pass --sync-session=off per run.",
                "adapter-gaps-v02.md (cli-config-dir-reference: remoteExport defaults to true)",
                file=(remote or {}).get("from") or str(self._home(ctx) / "settings.json"),
                key="remoteExport",
                value="default true" if not remote else str(value),
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
                    "agents.md / usage-logs.md",
                    file=str(path),
                ))
                break
        return findings
