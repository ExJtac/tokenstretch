"""Doctor checks for Gemini CLI and Copilot CLI, and the doctor-only Cursor, Aider, OpenCode, and Cline adapters."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import copy_fixture, credential_guard, finding_ids, findings_with, run_doctor
from tokenstretch.adapters import REGISTRY
from tokenstretch.adapters.base import HeadlessRefused, HookPlan
from tokenstretch.context import build_context


def _ad(aid: str):
    return next(a for a in REGISTRY if a.id == aid)


def test_new_adapters_are_doctor_only():
    ids = {ad.id for ad in REGISTRY}
    assert {"gemini-cli", "copilot-cli", "cursor", "aider", "opencode", "cline"} <= ids
    for aid in ("cursor", "aider", "opencode", "cline"):  # Gemini CLI and Copilot CLI are tier 1 now
        ad = _ad(aid)
        assert ad.tier == 2
        ctx = build_context()
        result = ad.install_hooks(ctx, HookPlan(items=["pre_tool_shell_filter"]), dry_run=True)
        assert result.get("changes") == []
        assert result.get("refused") == "doctor-only"
        with pytest.raises(HeadlessRefused):
            ad.run_headless_task(ctx, {"prompt": "hi"}, opts={"bench": True})
        with pytest.raises(HeadlessRefused):
            ad.run_headless_task(ctx, {"prompt": "hi"}, opts={})
        assert ad.read_usage_logs(ctx) == []


def test_clean_fixture_does_not_detect_new_adapters(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    with credential_guard():
        for aid in ("gemini-cli", "copilot-cli", "cursor", "aider", "opencode", "cline"):
            assert not _ad(aid).detect(ctx)


def test_gemini_cli_fixture(tmp_path):
    dest = copy_fixture("gemini", tmp_path)
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    ad = _ad("gemini-cli")
    with credential_guard():
        det = ad.detect(ctx)
        cfg = ad.read_config(ctx)
        files = ad.list_instruction_files(ctx)
        servers = ad.list_mcp_servers(ctx)
    assert det.installed
    assert cfg["effective"]["model"]["value"] == "gemini-2.5-pro"
    assert cfg["effective"]["telemetry"]["value"] is True  # documented default
    names = {Path(f.path).name for f in files}
    assert "GEMINI.md" in names and "AGENTS.md" in names
    assert {s.name for s in servers} == {"docs", "remote"}
    remote = next(s for s in servers if s.name == "remote")
    assert remote.transport == "http" and remote.remote_url
    code, findings, _, _ = run_doctor("gemini", tmp_path / "doctor", as_json=True)
    ids = finding_ids(findings, "gemini-cli")
    assert "W01" in ids and "W05" in ids and "P01" in ids and "P02" in ids and "P03" in ids
    p01 = findings_with(findings, "P01", "gemini-cli")[0]
    assert p01["severity"] == "info"
    assert "usageStatisticsEnabled" in p01["message"] or "usage statistics" in p01["message"].lower()


def test_copilot_cli_fixture(tmp_path):
    dest = copy_fixture("copilot", tmp_path)
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    ad = _ad("copilot-cli")
    with credential_guard():
        det = ad.detect(ctx)
        cfg = ad.read_config(ctx)
        files = ad.list_instruction_files(ctx)
        servers = ad.list_mcp_servers(ctx)
    assert det.installed
    assert cfg["effective"]["effort"]["value"] == "max"
    names = {Path(f.path).name for f in files}
    assert "copilot-instructions.md" in names
    assert "AGENTS.md" in names
    assert "python.instructions.md" in names
    assert {s.name for s in servers} == {"github", "remote"}
    code, findings, _, _ = run_doctor("copilot", tmp_path / "doctor", as_json=True)
    ids = finding_ids(findings, "copilot-cli")
    assert "W01" in ids and "W02" in ids and "W06" in ids and "P02" in ids and "P03" in ids
    p02 = findings_with(findings, "P02", "copilot-cli")
    assert any("remoteExport" in (f.get("message") + str(f.get("evidence") or "")) for f in p02)


def test_cursor_fixture(tmp_path):
    dest = copy_fixture("cursor", tmp_path)
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    ad = _ad("cursor")
    with credential_guard():
        det = ad.detect(ctx)
        cfg = ad.read_config(ctx)
        files = ad.list_instruction_files(ctx)
        servers = ad.list_mcp_servers(ctx)
        ignored = ad.ignored_md_rules(ctx)
    assert det.installed
    assert cfg["effective"]["max_mode"]["value"] is True
    listed = {Path(f.path).name for f in files}
    assert "big.mdc" in listed and "AGENTS.md" in listed
    assert "ignored.md" not in listed  # plain .md is silently ignored
    assert any(p.name == "ignored.md" for p in ignored)
    assert {s.name for s in servers} == {"web"}
    code, findings, _, _ = run_doctor("cursor", tmp_path / "doctor", as_json=True)
    ids = finding_ids(findings, "cursor")
    assert "W01" in ids and "W07" in ids and "P02" in ids
    p02 = findings_with(findings, "P02", "cursor")
    assert any("User Rules" in f["message"] and "Privacy Mode" in f["message"] and "state.vscdb" in f["message"] for f in p02)
    # both the oversized .mdc and the ignored .md note use W01
    w01 = findings_with(findings, "W01", "cursor")
    slugs = {f["slug"] for f in w01}
    assert "cursor-rules-size" in slugs and "cursor-rules-md-ignored" in slugs
    w07 = findings_with(findings, "W07", "cursor")[0]
    assert w07["severity"] == "high"


def test_aider_fixture(tmp_path):
    dest = copy_fixture("aider", tmp_path)
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    ad = _ad("aider")
    with credential_guard():
        det = ad.detect(ctx)
        cfg = ad.read_config(ctx)
        files = ad.list_instruction_files(ctx)
        servers = ad.list_mcp_servers(ctx)
    assert det.installed
    assert det.auth_mode == "api-key"
    assert cfg["effective"]["model"]["value"] == "gpt-4"
    assert cfg["effective"]["cache_keepalive_pings"]["value"] == 3
    assert cfg["effective"]["cache_prompts"]["value"] is False
    assert any(f.path.endswith("CONVENTIONS.md") for f in files)
    assert servers == []
    code, findings, _, _ = run_doctor("aider", tmp_path / "doctor", as_json=True)
    ids = finding_ids(findings, "aider")
    assert "W01" in ids and "W06" in ids and "W09" in ids
    assert "T04" in ids and "P01" in ids and "P03" in ids and "P05" in ids
    t04 = findings_with(findings, "T04", "aider")[0]
    assert t04["severity"] == "info"
    p01 = findings_with(findings, "P01", "aider")[0]
    assert p01["severity"] == "high"
    assert code == 2



def test_gemini_system_settings_env_walk_oauth_and_thinking(tmp_path):
    """System/env overrides, GEMINI.md walk + @imports, OAuth-by-stat, thinking ranges."""
    from tests.conftest import copy_fixture, credential_guard, CANARIES
    from tokenstretch.context import build_context
    dest = copy_fixture("gemini", tmp_path / "syscase")
    sys_settings = dest / "sys" / "gemini-system-settings.json"
    sys_settings.write_text(
        '{"model":{"name":"gemini-2.5-flash"},'
        '"modelConfigs":{"overrides":[{"model":"gemini-2.5-flash",'
        '"generateContentConfig":{"thinkingConfig":{"thinkingBudget":24576,'
        '"thinkingLevel":"HIGH"}}}]}}\n',
        encoding="utf-8",
    )
    env = dest / "sys" / "env.json"
    import json
    data = json.loads(env.read_text(encoding="utf-8") or "{}")
    data["GEMINI_CLI_SYSTEM_SETTINGS_PATH"] = str(sys_settings)
    env.write_text(json.dumps(data), encoding="utf-8")
    # Hierarchical GEMINI.md + @import (depth 1).
    sub = dest / "root" / "pkg"
    sub.mkdir()
    imported = sub / "extra.md"
    imported.write_text("imported notes\n", encoding="utf-8")
    (sub / "GEMINI.md").write_text("@extra.md\nsub instructions\n", encoding="utf-8")
    # OAuth file: stat only, never open.
    oauth = dest / "home" / ".gemini" / "oauth_creds.json"
    oauth.write_text("CANARY_CREDENTIAL_DO_NOT_READ_GEMINI_OAUTH\n", encoding="utf-8")
    oauth.chmod(0o600)
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    ad = _ad("gemini-cli")
    with credential_guard():
        det = ad.detect(ctx)
        cfg = ad.read_config(ctx)
        files = ad.list_instruction_files(ctx)
        findings = ad.checks(ctx)
    assert det.auth_mode == "subscription"
    # System settings overlay user: model becomes flash (system wins).
    assert cfg["effective"]["model"]["value"] == "gemini-2.5-flash"
    names = {Path(f.path).name for f in files}
    assert "GEMINI.md" in names and "extra.md" in names
    ids = {f.id for f in findings}
    assert "W06" in ids and "W09" in ids
    # flash ceiling 24576 plus both budget and level.
    w06 = [f for f in findings if f.id == "W06"]
    assert w06
    blob = str(cfg) + "".join(f.message + str(f.evidence) for f in findings)
    for c in CANARIES:
        assert c not in blob


def test_gemini_secret_env_is_not_expanded(tmp_path):
    from tests.conftest import copy_fixture, credential_guard
    from tokenstretch.context import build_context
    dest = copy_fixture("gemini", tmp_path / "secret")
    settings = dest / "home" / ".gemini" / "settings.json"
    import json
    data = json.loads(settings.read_text(encoding="utf-8"))
    data.setdefault("model", {})
    data["model"]["name"] = "${GEMINI_API_KEY:-gemini-2.5-pro}"
    settings.write_text(json.dumps(data), encoding="utf-8")
    env = dest / "sys" / "env.json"
    ed = json.loads(env.read_text(encoding="utf-8") or "{}")
    ed["GEMINI_API_KEY"] = "SUPER_SECRET_SHOULD_NOT_APPEAR"
    env.write_text(json.dumps(ed), encoding="utf-8")
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    ad = _ad("gemini-cli")
    with credential_guard():
        cfg = ad.read_config(ctx)
        findings = ad.checks(ctx)
    assert cfg["effective"]["model"]["value"] == "${GEMINI_API_KEY:-gemini-2.5-pro}"
    blob = str(cfg) + "".join(f.message + str(f.evidence) for f in findings)
    assert "SUPER_SECRET_SHOULD_NOT_APPEAR" not in blob


def test_aider_config_precedence_and_env_override(tmp_path):
    from tests.conftest import copy_fixture, credential_guard
    from tokenstretch.context import build_context
    dest = copy_fixture("aider", tmp_path / "prec")
    (dest / "home" / ".aider.conf.yml").write_text("model: from-home\ncheck-update: false\n", encoding="utf-8")
    # cwd already has a conf in the fixture; later (cwd) should win over home for YAML.
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    ad = _ad("aider")
    with credential_guard():
        cfg = ad.read_config(ctx)
    # Fixture project model is gpt-4; that should beat from-home.
    assert cfg["effective"]["model"]["value"] == "gpt-4"
    # Env overrides YAML, including check-update (so P05 stays quiet when false).
    env = dest / "sys" / "env.json"
    import json
    data = json.loads(env.read_text(encoding="utf-8") or "{}")
    data["AIDER_MODEL"] = "from-env"
    data["AIDER_CHECK_UPDATE"] = "false"
    env.write_text(json.dumps(data), encoding="utf-8")
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    with credential_guard():
        cfg = ad.read_config(ctx)
        findings = ad.checks(ctx)
    assert cfg["effective"]["model"]["value"] == "from-env"
    assert not any(f.id == "P05" for f in findings)


def test_cursor_never_opens_state_vscdb(tmp_path):
    from tests.conftest import copy_fixture, credential_guard
    from tokenstretch.context import build_context
    dest = copy_fixture("cursor", tmp_path / "vscdb")
    db = dest / "home" / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text("SHOULD_NOT_BE_OPENED", encoding="utf-8")
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    ad = _ad("cursor")
    opened = []
    import builtins
    real = builtins.open

    def wrap(file, *args, **kwargs):
        if "state.vscdb" in str(file):
            opened.append(str(file))
            raise AssertionError(f"state.vscdb was opened: {file}")
        return real(file, *args, **kwargs)

    builtins.open = wrap
    try:
        with credential_guard():
            ad.read_config(ctx)
            ad.privacy_checks(ctx)
            ad.checks(ctx)
    finally:
        builtins.open = real
    assert opened == []



def test_opencode_fixture(tmp_path):
    dest = copy_fixture("opencode", tmp_path)
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    ad = _ad("opencode")
    with credential_guard():
        det = ad.detect(ctx)
        cfg = ad.read_config(ctx)
        files = ad.list_instruction_files(ctx)
        servers = ad.list_mcp_servers(ctx)
    assert det.installed
    assert cfg["effective"]["model"]["value"] == "anthropic/claude-sonnet-4-5"
    assert cfg["effective"]["warming"]["value"] is True
    names = {Path(f.path).name for f in files}
    assert "AGENTS.md" in names and "CLAUDE.md" in names
    assert {s.name for s in servers} == {"docs", "remote"}
    remote = next(s for s in servers if s.name == "remote")
    assert remote.transport == "http" and remote.remote_url
    code, findings, _, _ = run_doctor("opencode", tmp_path / "doctor", as_json=True)
    ids = finding_ids(findings, "opencode")
    assert "W01" in ids and "W02" in ids and "T04" in ids and "P02" in ids and "P03" in ids
    t04 = findings_with(findings, "T04", "opencode")[0]
    assert t04["severity"] == "med"
    assert "warming" in t04["message"].lower()
    w01 = findings_with(findings, "W01", "opencode")
    assert any("CLAUDE.md" in f["message"] for f in w01)


def test_cline_fixture(tmp_path):
    dest = copy_fixture("cline", tmp_path)
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    ad = _ad("cline")
    with credential_guard():
        det = ad.detect(ctx)
        cfg = ad.read_config(ctx)
        files = ad.list_instruction_files(ctx)
        servers = ad.list_mcp_servers(ctx)
    assert det.installed
    assert cfg["effective"]["thinking"]["value"] == "xhigh"
    assert cfg["effective"]["auto_approve"]["value"] is True
    names = {Path(f.path).name for f in files}
    assert "project.md" in names and "AGENTS.md" in names
    assert ".cursorrules" in names and ".windsurfrules" in names and ".roorules" in names
    assert {s.name for s in servers} == {"docs", "remote"}
    code, findings, _, _ = run_doctor("cline", tmp_path / "doctor", as_json=True)
    ids = finding_ids(findings, "cline")
    assert "W01" in ids and "W06" in ids and "W18" in ids and "P02" in ids and "P03" in ids
    w18 = findings_with(findings, "W18", "cline")[0]
    assert w18["severity"] == "info"
    w06 = findings_with(findings, "W06", "cline")[0]
    assert "xhigh" in (w06["message"] + str(w06.get("evidence"))).lower()
    w01 = findings_with(findings, "W01", "cline")
    assert any("Roo" in f["message"] or "roo" in f["message"].lower() for f in w01)


def test_cline_auto_approve_defaults_true_when_unset(tmp_path):
    dest = copy_fixture("cline", tmp_path / "default")
    (dest / "home" / ".cline" / "data" / "settings" / "global-settings.json").write_text('{"model": "x"}\n', encoding="utf-8")
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    ad = _ad("cline")
    with credential_guard():
        cfg = ad.read_config(ctx)
        findings = ad.checks(ctx)
    assert cfg["effective"]["auto_approve"]["value"] is True
    assert "default" in cfg["effective"]["auto_approve"]["from"]
    assert any(f.id == "W18" for f in findings)


def test_cline_does_not_open_sessions_db(tmp_path):
    dest = copy_fixture("cline", tmp_path / "db")
    db = dest / "home" / ".cline" / "data" / "db" / "sessions.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_text("SHOULD_NOT_BE_OPENED", encoding="utf-8")
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    ad = _ad("cline")
    opened = []
    import builtins
    real = builtins.open

    def wrap(file, *args, **kwargs):
        if "sessions.db" in str(file):
            opened.append(str(file))
            raise AssertionError(f"sessions.db was opened: {file}")
        return real(file, *args, **kwargs)

    builtins.open = wrap
    try:
        with credential_guard():
            ad.detect(ctx)
            ad.read_config(ctx)
            ad.privacy_checks(ctx)
            ad.checks(ctx)
            ad.read_usage_logs(ctx, since_days=30)
    finally:
        builtins.open = real
    assert opened == []


def test_opencode_jsonc_wins_over_json_and_project_overrides_user(tmp_path):
    dest = copy_fixture("opencode", tmp_path / "prec")
    user = dest / "home" / ".config" / "opencode"
    user.mkdir(parents=True, exist_ok=True)
    (user / "opencode.json").write_text('{"model": "user-json", "warming": true}\n', encoding="utf-8")
    (user / "opencode.jsonc").write_text(
        '// jsonc wins over json in the same directory\n{"model": "user-jsonc"}\n',
        encoding="utf-8",
    )
    (dest / "root" / "opencode.jsonc").write_text(
        '{"model": "project-jsonc", "warming": false}\n',
        encoding="utf-8",
    )
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    ad = _ad("opencode")
    with credential_guard():
        cfg = ad.read_config(ctx)
    assert cfg["effective"]["model"]["value"] == "project-jsonc"
    assert "project" in cfg["effective"]["model"]["from"]
    assert cfg["effective"]["warming"]["value"] is False
