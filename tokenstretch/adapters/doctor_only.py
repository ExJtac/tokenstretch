"""Shared behavior for doctor-only adapters: hooks and headless always refuse."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..files import count_lines_bytes
from ..findings import Finding
from ..util import read_json
from .base import Adapter, HeadlessRefused, HookPlan, InstructionFile, McpServer


class DoctorOnlyRefused(PermissionError):
    """Raised when a doctor-only adapter is asked to install hooks or run headless."""


class DoctorOnlyAdapter(Adapter):
    """Tier-2 adapter: detect/config/instructions/MCP/checks only."""

    tier = 2

    def install_hooks(self, ctx, plan: HookPlan | dict | None = None, *, dry_run: bool = True) -> dict:
        return {
            "changes": [],
            "backup_dir": "",
            "refused": "doctor-only",
            "reason": f"{self.id} is doctor-only; hook install is not supported",
        }

    def run_headless_task(self, ctx, task: dict | None = None, opts: dict | None = None) -> dict:
        raise HeadlessRefused(f"{self.id} is doctor-only; run_headless_task is refused")

    def read_usage_logs(self, ctx, since_days: int = 30):
        return []


def load_json(path: Path) -> dict:
    data = read_json(path)
    return data if isinstance(data, dict) else {}


def stat_instruction(path: Path, scope: str, always_loaded: bool = True) -> InstructionFile | None:
    counted = count_lines_bytes(path)
    if not counted:
        return None
    lines, size, _ = counted
    return InstructionFile(
        path=str(path), scope=scope, bytes=size, lines=lines, always_loaded=always_loaded,
    )


def mcp_from_mapping(servers: Any, *, file: str, scope: str) -> list[McpServer]:
    out: list[McpServer] = []
    if not isinstance(servers, dict):
        return out
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        typ = str(entry.get("type") or entry.get("transport") or "")
        if typ in {"http", "sse"}:
            transport = typ
        elif url:
            transport = typ or "http"
        else:
            transport = "stdio"
        out.append(McpServer(
            name=str(name),
            scope=scope,
            transport=transport,
            enabled=entry.get("enabled") is not False,
            file=file,
            remote_url=str(url) if url else None,
        ))
    return out


def mcp_servers_in(data: dict, *, file: str, scope: str) -> list[McpServer]:
    if not isinstance(data, dict):
        return []
    if isinstance(data.get("mcpServers"), dict):
        return mcp_from_mapping(data["mcpServers"], file=file, scope=scope)
    # Copilot's mcp-config.json shape is not quoted beyond the file path;
    # accept a top-level map of server objects only.
    if data and all(isinstance(v, dict) and ("command" in v or "url" in v or "type" in v) for v in data.values()):
        return mcp_from_mapping(data, file=file, scope=scope)
    return []


def finding(
    adapter_id: str,
    check_id: str,
    slug: str,
    severity: str,
    category: str,
    message: str,
    fix: str,
    source: str,
    **evidence: Any,
) -> Finding:
    return Finding(
        id=check_id,
        severity=severity,
        message=message,
        fix=fix,
        source=source,
        evidence=evidence,
        category=category,
        slug=slug,
        adapter_id=adapter_id,
    )


def instruction_size_findings(adapter_id: str, files: list[InstructionFile], *, line_limit: int, byte_limit: int, check_id: str, slug: str, source: str) -> list[Finding]:
    loaded = [f for f in files if f.always_loaded]
    if not loaded:
        return []
    total_lines = sum(f.lines for f in loaded)
    total_bytes = sum(f.bytes for f in loaded)
    over_lines = [f for f in loaded if f.lines > line_limit]
    if not over_lines and total_bytes <= byte_limit and total_lines <= line_limit:
        return []
    severity = "high" if total_bytes > byte_limit else "med"
    paths = ",".join(f.path for f in loaded)
    return [finding(
        adapter_id, check_id, slug, severity, "waste",
        f"Your instruction files total {total_lines} lines / {total_bytes} bytes and are re-read on every message.",
        "Trim standing instructions; keep only the rules that matter.",
        source, file=paths, key="bytes", value=str(total_bytes),
    )]
