"""The adapter layer exposes the eight methods from the Adapter interface."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import copy_fixture, credential_guard, run_doctor
from tokenstretch.adapters import REGISTRY
from tokenstretch.adapters.base import DetectResult, HeadlessRefused, HookPlan
from tokenstretch.context import build_context

METHODS = (
    "detect",
    "read_config",
    "list_instruction_files",
    "list_mcp_servers",
    "read_usage_logs",
    "install_hooks",
    "run_headless_task",
    "undo",
)


def test_registry_and_required_methods():
    ids = {ad.id for ad in REGISTRY}
    assert ids == {"host", "claude-code", "codex", "gemini-cli", "copilot-cli", "cursor", "aider", "opencode", "cline"}
    by_id = {ad.id: ad for ad in REGISTRY}
    assert by_id["claude-code"].tier == 1 and by_id["codex"].tier == 1
    assert by_id["host"].tier == 2
    for ad in REGISTRY:
        for name in METHODS:
            assert callable(getattr(ad, name)), f"{ad.id} missing {name}"


def test_detect_and_lists_on_clean_fixture(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    claude = next(ad for ad in REGISTRY if ad.id == "claude-code")
    codex = next(ad for ad in REGISTRY if ad.id == "codex")
    with credential_guard():
        cd = claude.detect(ctx)
        xd = codex.detect(ctx)
        c_files = claude.list_instruction_files(ctx)
        x_files = codex.list_instruction_files(ctx)
        c_cfg = claude.read_config(ctx)
        x_cfg = codex.read_config(ctx)
    assert isinstance(cd, DetectResult) and cd.installed
    assert isinstance(xd, DetectResult) and xd.installed
    assert cd.auth_mode in {"subscription", "api-key", "byok", "local", "unknown"}
    assert any(Path(f.path).name == "CLAUDE.md" and f.lines < 200 for f in c_files)
    assert any(Path(f.path).name == "AGENTS.md" and f.bytes < 16 * 1024 for f in x_files)
    assert "model" in c_cfg["effective"]
    assert c_cfg["effective"]["model"]["value"] == "claude-sonnet-4-5"
    assert x_cfg["effective"]["model"]["value"] == "gpt-5.1"
    assert claude.list_mcp_servers(ctx) == []
    assert codex.list_mcp_servers(ctx) == []


def test_lists_on_bloated_and_codex_fixtures(tmp_path):
    bloated = copy_fixture("bloated", tmp_path)
    ctx = build_context(bloated / "home", bloated / "root", bloated / "sys")
    claude = next(ad for ad in REGISTRY if ad.id == "claude-code")
    with credential_guard():
        files = claude.list_instruction_files(ctx)
        servers = claude.list_mcp_servers(ctx)
        logs = claude.read_usage_logs(ctx, since_days=30)
        cfg = claude.read_config(ctx)
    assert any(f.lines > 200 and f.bytes > 25 * 1024 for f in files)
    names = {s.name for s in servers}
    assert names == {"github", "idle-docs", "remote-search"}
    remote = next(s for s in servers if s.name == "remote-search")
    assert remote.transport == "http" and remote.remote_url
    assert logs, "expected usage records from the bloated transcript"
    assert cfg["effective"]["fast_mode"]["value"] is True
    assert "127.0.0.1:8787" in str(cfg["effective"]["base_url"]["value"])

    dest = copy_fixture("codex", tmp_path)
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    codex = next(ad for ad in REGISTRY if ad.id == "codex")
    with credential_guard():
        files = codex.list_instruction_files(ctx)
        servers = codex.list_mcp_servers(ctx)
        logs = codex.read_usage_logs(ctx, since_days=30)
    assert any(f.bytes > 16 * 1024 for f in files)
    assert {s.name for s in servers} == {"docs", "search"}
    assert all(s.enabled for s in servers)
    assert logs and sum(r.total_input() for r in logs) > 150_000


def test_install_hooks_stub_writes_nothing(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    before = {p: p.read_bytes() for p in dest.rglob("*") if p.is_file()}
    planned = []
    for ad in REGISTRY:
        result = ad.install_hooks(ctx, HookPlan(items=["pre_tool_shell_filter"]), dry_run=True)
        assert isinstance(result, dict)
        assert "changes" in result
        planned.extend(result["changes"] or [])
    after = {p: p.read_bytes() for p in dest.rglob("*") if p.is_file()}
    assert before == after  # planners must not write; the installer owns applies
    for ch in planned:
        assert ch.get("path")
        assert "content" in ch


def test_run_headless_task_refuses_without_bench():
    ad = next(a for a in REGISTRY if a.id == "claude-code")
    ctx = build_context()  # not used by the stub
    with pytest.raises(HeadlessRefused):
        ad.run_headless_task(ctx, {"prompt": "hi"}, opts={})
    with pytest.raises(HeadlessRefused):
        ad.run_headless_task(ctx, {"prompt": "hi"}, opts={"bench": False})


def test_undo_stub_raises():
    with pytest.raises(NotImplementedError):
        next(a for a in REGISTRY if a.id == "claude-code").undo(build_context(), manifest_path="/nonexistent/manifest.json")


def test_tos_risk_credentials_are_stat_only(tmp_path):
    dest = copy_fixture("tos-risk", tmp_path)
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    claude = next(ad for ad in REGISTRY if ad.id == "claude-code")
    codex = next(ad for ad in REGISTRY if ad.id == "codex")
    with credential_guard():
        c_locs = claude.config_locations(ctx)
        x_locs = codex.config_locations(ctx)
        assert claude.auth_mode(ctx) == "subscription"
        assert codex.auth_mode(ctx) == "subscription"
    cred_c = [loc for loc in c_locs if loc.sensitive]
    cred_x = [loc for loc in x_locs if loc.sensitive]
    assert any(loc.path.name == ".credentials.json" and loc.exists for loc in cred_c)
    assert any(loc.path.name == "auth.json" and loc.exists for loc in cred_x)
