"""Doctor-only adapter for Cursor (editor agent + agent CLI)."""
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


class CursorAdapter(DoctorOnlyAdapter):
    id = "cursor"
    display_name = "Cursor"
    vendor_terms = "Cursor"
    implemented_checks = frozenset({"W01", "W02", "W07", "P02"})
    # User Rules path and Privacy Mode setting key are not quoted — unverified.
    # The generic `agent` binary name is not used as a sole detect signal.

    def _user_dir(self, ctx: Context) -> Path:
        # Official docs: ~/.cursor/cli-config.json, relocated by CURSOR_CONFIG_DIR.
        # The exact XDG_CONFIG_HOME subdirectory is not fully quoted; only CURSOR_CONFIG_DIR is honored.
        val, _ = ctx.env_or_rc("CURSOR_CONFIG_DIR")
        return Path(val).expanduser() if val else ctx.home / ".cursor"

    def detect(self, ctx: Context) -> DetectResult:
        user = self._user_dir(ctx)
        candidates = [
            user / "cli-config.json",
            user / "hooks.json",
            user / "mcp.json",
            ctx.root / ".cursor" / "hooks.json",
            ctx.root / ".cursor" / "mcp.json",
            ctx.root / ".cursor" / "rules",
            user,
            ctx.root / ".cursor",
        ]
        roots = [str(p) for p in candidates if p.exists()]
        return DetectResult(
            installed=bool(roots),
            binary=None,
            config_roots=roots,
            auth_mode="unknown",
        )

    def read_config(self, ctx: Context) -> dict:
        path = self._user_dir(ctx) / "cli-config.json"
        data = load_json(path) if path.is_file() else {}
        effective: dict = {}
        if data.get("model") is not None:
            effective["model"] = {"value": data["model"], "from": str(path)}
        if "maxMode" in data:
            effective["max_mode"] = {"value": data["maxMode"], "from": f"{path}:maxMode"}
        raw = []
        if path.is_file():
            raw.append({"file": str(path), "scope": "user", "data": data})
        for file, scope in (
            (self._user_dir(ctx) / "mcp.json", "user"),
            (ctx.root / ".cursor" / "mcp.json", "project"),
            (self._user_dir(ctx) / "hooks.json", "user"),
            (ctx.root / ".cursor" / "hooks.json", "project"),
        ):
            if file.is_file():
                raw.append({"file": str(file), "scope": scope, "data": load_json(file)})
        return {"effective": effective, "raw": raw}

    def list_instruction_files(self, ctx: Context, cwd: str | None = None) -> list[InstructionFile]:
        root = Path(cwd) if cwd else ctx.root
        out: list[InstructionFile] = []
        rules = root / ".cursor" / "rules"
        if rules.is_dir():
            try:
                names = sorted(rules.iterdir())
            except OSError:
                names = []
            for path in names:
                # Plain .md is ignored (https://cursor.com/docs/rules). Only *.mdc is loaded.
                if path.suffix == ".mdc":
                    item = stat_instruction(path, "project", True)
                    if item:
                        out.append(item)
        item = stat_instruction(root / "AGENTS.md", "project", True)
        if item:
            out.append(item)
        return out

    def ignored_md_rules(self, ctx: Context) -> list[Path]:
        """`.cursor/rules/*.md` files exist but are silently ignored (research)."""
        rules = ctx.root / ".cursor" / "rules"
        if not rules.is_dir():
            return []
        try:
            return sorted(p for p in rules.iterdir() if p.is_file() and p.suffix == ".md")
        except OSError:
            return []

    def list_mcp_servers(self, ctx: Context) -> list[McpServer]:
        out: list[McpServer] = []
        for path, scope in (
            (ctx.root / ".cursor" / "mcp.json", "project"),
            (self._user_dir(ctx) / "mcp.json", "user"),
        ):
            if path.is_file():
                out.extend(mcp_servers_in(load_json(path), file=str(path), scope=scope))
        return out

    def checks(self, ctx: Context) -> list:
        if not self.detect(ctx):
            return []
        findings = []
        files = self.list_instruction_files(ctx)
        rules = [f for f in files if Path(f.path).suffix == ".mdc"]
        agents = [f for f in files if Path(f.path).name == "AGENTS.md"]
        # Cursor guidance: "Keep rules under 500 lines".
        over = [f for f in rules if f.lines > 500]
        if over:
            findings.append(finding(
                self.id, "W01", "cursor-rules-size", "med", "waste",
                "A Cursor rule file is over the documented 500-line guidance and is loaded as standing instruction.",
                "Keep each .cursor/rules/*.mdc file under 500 lines.",
                "agents.md (cursor.com/docs/rules: under 500 lines)",
                file=",".join(f.path for f in over), key="lines", value=str(max(f.lines for f in over)),
            ))
        findings.extend(instruction_size_findings(
            self.id, agents, line_limit=200, byte_limit=16 * 1024, check_id="W02",
            slug="agents-md-size", source="agents.md (AGENTS.md)",
        ))
        ignored = self.ignored_md_rules(ctx)
        if ignored:
            findings.append(finding(
                self.id, "W01", "cursor-rules-md-ignored", "info", "waste",
                "One or more Cursor rules are saved as .md and are silently ignored; Cursor only loads .mdc.",
                "Rename rule files under .cursor/rules/ to .mdc.",
                "agents.md (cursor.com/docs/rules)",
                file=",".join(str(p) for p in ignored),
            ))
        cfg = self.read_config(ctx)
        max_mode = (cfg["effective"].get("max_mode") or {}).get("value")
        if max_mode is True or str(max_mode).lower() in {"true", "1"}:
            ev = cfg["effective"]["max_mode"]
            findings.append(finding(
                self.id, "W07", "fast-mode-on", "high", "waste",
                "Cursor maxMode is on by default in the CLI config. Turn it on only when you need it.",
                "Set maxMode to false in ~/.cursor/cli-config.json and enable it per session when needed.",
                "agents.md (~/.cursor/cli-config.json model, maxMode)",
                file=ev.get("from"), key="maxMode", value=str(max_mode),
            ))
        # privacy_checks() is run separately by doctor
        return findings

    def privacy_checks(self, ctx: Context) -> list:
        if not self.detect(ctx):
            return []
        findings = []
        findings.append(finding(
            self.id, "P02", "third-party-cloud", "info", "privacy",
            "Cursor User Rules and Privacy Mode are account/cloud settings. Check them in Cursor Settings. "
            "This kit never opens state.vscdb (it is credential-adjacent and undocumented as a doctor source).",
            "Review User Rules and Privacy Mode in Cursor Settings → General (teams can enforce Privacy Mode from the dashboard).",
            "adapter-gaps-v02.md (cursor.com/docs/rules, privacy-and-data-governance; no official local key)",
        ))
        for srv in self.list_mcp_servers(ctx):
            if srv.remote_url:
                findings.append(finding(
                    self.id, "P02", "third-party-cloud", "info", "privacy",
                    f"Remote MCP server {srv.name!r} is configured; it receives tool arguments.",
                    "Remove the remote MCP server if you did not mean to share tool arguments with it.",
                    "agents.md (.cursor/mcp.json, ~/.cursor/mcp.json)",
                    file=srv.file, key=srv.name, value=srv.remote_url,
                ))
        return findings
