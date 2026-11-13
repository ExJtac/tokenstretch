"""Installer + undo: dry-run default, apply/undo on fixtures, drift, licenses, 8787.

Installs never run during tests: FakeRunner records argv and does not exec.
The autouse socket guard plus this fake runner keep the suite off the network.
"""
from __future__ import annotations

import io
import json
import sys
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from conftest import copy_fixture

from tokenstretch.context import build_context
from tokenstretch.installer import (
    DriftError,
    check_licenses,
    detect_8787_conflict,
    format_install_report,
    install,
    latest_manifest,
    load_snapshots,
    undo_manifest,
)
from tokenstretch.runner import FakeRunner, LiveRunner, NetworkInstallRefused


def _ctx(dest: Path):
    return build_context(dest / "home", dest / "root", dest / "sys")


def _tree(root: Path) -> dict[str, bytes]:
    """File contents under a fixture copy, excluding kit manifests/backups/wrappers."""
    out = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if ".tokenstretch" in p.parts:
            continue
        out[str(p.relative_to(root))] = p.read_bytes()
    return out


def _empty_dirs_outside_kit(root: Path) -> set[str]:
    empty = set()
    for p in root.rglob("*"):
        if p.is_dir() and ".tokenstretch" not in p.parts and not any(p.iterdir()):
            empty.add(str(p.relative_to(root)))
    return empty


def test_dry_run_default_writes_nothing_and_prints_plan(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    ctx = _ctx(dest)
    before = _tree(dest)
    runner = FakeRunner()
    result = install(ctx, apply=False, runner=runner)
    report = format_install_report(result)

    assert result.dry_run is True
    assert result.ok is True
    assert result.exit_code == 0
    assert result.written == []
    assert result.manifest_path == ""
    assert runner.calls == []  # fetch steps only run on --apply
    assert _tree(dest) == before
    assert "DRY RUN" in report
    assert "no files were written" in report.lower() or "Dry run" in report
    assert "v1.2.0" in report
    assert "v4.1.0" in report
    assert "v0.1.0" in report
    assert "20.0.48" in report
    assert "telemetry" in report.lower()
    assert "--offline" in report
    assert "Apache-2.0" in report
    assert "MIT" in report
    assert "e288a18c04452fd26192f59132e5324a8bb6924d" in report
    assert "1e91f1306d602d4ca23618701b1185575ae1869b" in report
    assert "ec8ff5ec7ed5924520ee71b705a920ee64fe2918" in report
    assert "linux-gnu x64" in report
    assert "Windows arm64" in report
    assert "85ae798116fa0dda42a627333b36903b71291785c7395ad3634f8419f7adaeb5" in report
    # Planned hook/support files are listed, but nothing is on disk yet.
    assert result.file_changes
    paths = {ch["path"] for ch in result.file_changes}
    assert any(Path(p).as_posix().endswith("/.claude/settings.json") for p in paths)
    assert any("permissions" in (ch.get("reason") or "").lower() or "deny" in (ch.get("reason") or "").lower()
               or p.endswith("/.claude/settings.json") for p in paths for ch in result.file_changes)
    assert any("ccusage" in ch["path"] or "offline" in (ch.get("reason") or "").lower()
               for ch in result.file_changes)


def test_cli_install_defaults_to_dry_run(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    before = _tree(dest)
    from tokenstretch.cli import cmd_install

    args = Namespace(
        home=dest / "home",
        root=dest / "root",
        sys_snapshot=dest / "sys",
        plan=None,
        apply=False,
        force=False,
        license_snapshots=None,
        json=False,
    )
    buf, err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        code = cmd_install(args)
    assert code == 0
    assert "DRY RUN" in buf.getvalue()
    assert "--apply" in buf.getvalue()
    assert _tree(dest) == before


def test_apply_undo_round_trip(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    ctx = _ctx(dest)
    before = _tree(dest)
    before_empty = _empty_dirs_outside_kit(dest)
    runner = FakeRunner()

    applied = install(ctx, apply=True, runner=runner)
    assert applied.dry_run is False
    assert applied.ok is True
    assert applied.exit_code == 0
    assert applied.written
    assert applied.manifest_path
    assert Path(applied.manifest_path).is_file()
    assert Path(applied.backup_dir).is_dir()
    # Fake runner recorded fetch steps and never executed them.
    assert runner.calls
    assert all(c.skipped for c in runner.calls)
    assert all(c.exit_code == 0 for c in runner.calls)
    tools = {c.argv[0] if c.argv else "" for c in runner.calls}
    argv_blob = " ".join(" ".join(c.argv) for c in runner.calls)
    assert "v1.2.0" in argv_blob or "RTK_VERSION" in argv_blob or any("rtk" in " ".join(c.argv).lower() for c in runner.calls)
    assert "e288a18c04452fd26192f59132e5324a8bb6924d" in argv_blob
    assert "JuliusBrussee/caveman" in argv_blob and "v4.1.0" in argv_blob
    assert "TokenEconomizer" in argv_blob
    assert "npm" in argv_blob and "ci" in argv_blob
    assert "--no-audit" in argv_blob and "--no-fund" in argv_blob
    assert any(c.env.get("npm_config_update_notifier") == "false" for c in runner.calls)
    assert any(c.env.get("RTK_EDITION") == "oss" and c.env.get("RTK_VERSION") == "v1.2.0" for c in runner.calls)
    assert "@latest" not in argv_blob
    assert "master" not in argv_blob
    assert "caveman-setup" not in argv_blob

    manifest = json.loads(Path(applied.manifest_path).read_text(encoding="utf-8"))
    actions = {rec["action"] for rec in manifest["files"]}
    assert actions <= {"created", "modified"}
    assert "created" in actions and "modified" in actions
    for rec in manifest["files"]:
        path = Path(rec["path"])
        assert path.is_file()
        digest = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
        assert digest == rec["sha256_after"]
        if rec["action"] == "created":
            assert rec["sha256_before"] is None
        else:
            assert rec["sha256_before"]
            backup = Path(applied.backup_dir) / rec["backup"]
            assert backup.is_file()
            assert __import__("hashlib").sha256(backup.read_bytes()).hexdigest() == rec["sha256_before"]

    # Telemetry-off configuration is on disk.
    rtk = (dest / "home" / ".config" / "rtk" / "config.toml").read_text(encoding="utf-8")
    assert "telemetry = false" in rtk
    te = json.loads((dest / "home" / ".tokeneconomizer" / "config.json").read_text(encoding="utf-8"))
    assert te == {"microskills": "off", "toolprune": "propose", "loopguard": "off"}
    wrapper = (dest / "home" / ".tokenstretch" / "bin" / "ccusage").read_text(encoding="utf-8")
    assert "--offline" in wrapper and "20.0.48" in wrapper
    ccjson = json.loads((dest / "home" / ".claude" / "ccusage.json").read_text(encoding="utf-8"))
    assert ccjson == {"defaults": {"offline": True}}
    lock = json.loads((dest / "home" / ".tokenstretch" / "ccusage" / "package-lock.json").read_text(encoding="utf-8"))
    assert lock["packages"]["node_modules/ccusage"]["integrity"].startswith("sha512-8SKmBGO")
    settings = json.loads((dest / "home" / ".claude" / "settings.json").read_text(encoding="utf-8"))
    hook_blob = json.dumps(settings.get("hooks") or {})
    assert "rtk" in hook_blob
    project = json.loads((dest / "root" / ".claude" / "settings.json").read_text(encoding="utf-8"))
    deny = ((project.get("permissions") or {}).get("deny")) or []
    assert any(str(p).startswith("Read(") for p in deny)
    # Do not create Caveman CLI paths.
    assert not (dest / "home" / ".config" / "caveman").exists()
    assert not (dest / "home" / ".caveman").exists()

    # Tree is different after apply (excluding .tokenstretch).
    assert _tree(dest) != before

    undone = undo_manifest(Path(applied.manifest_path), force=False)
    assert undone["restored"] or undone["deleted"]
    assert _tree(dest) == before
    assert _empty_dirs_outside_kit(dest) == before_empty

    # Undo is idempotent when already restored.
    again = undo_manifest(Path(applied.manifest_path), force=False)
    assert not again["restored"] and not again["deleted"]
    assert _tree(dest) == before


def test_undo_refuses_drift_unless_force(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    ctx = _ctx(dest)
    before = _tree(dest)
    applied = install(ctx, apply=True, runner=FakeRunner())
    manifest = json.loads(Path(applied.manifest_path).read_text(encoding="utf-8"))
    # Drift a created or modified file.
    target = Path(manifest["files"][0]["path"])
    original = target.read_bytes()
    target.write_bytes(original + b"\n# drifted\n")

    with pytest.raises(DriftError, match="changed since the install"):
        undo_manifest(Path(applied.manifest_path), force=False)
    # File still drifted; tree not restored.
    assert target.read_bytes() != original

    forced = undo_manifest(Path(applied.manifest_path), force=True)
    assert forced["restored"] or forced["deleted"]
    assert _tree(dest) == before


def test_license_check_failure_writes_nothing(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    ctx = _ctx(dest)
    before = _tree(dest)
    snap_dir = tmp_path / "bad-snaps"
    snap_dir.mkdir()
    # Copy packaged snapshots, then mark RTK as the excluded BUSL license.
    packaged = load_snapshots()
    assert packaged, "packaged license snapshots should exist"
    for stem, data in packaged.items():
        payload = dict(data)
        if stem == "rtk-oss" or data.get("id") == "rtk-oss":
            payload["license"] = "BUSL-1.1"
            payload["allowed"] = False
        (snap_dir / f"{stem}.json").write_text(json.dumps(payload, indent=2) + "\n")

    runner = FakeRunner()
    result = install(ctx, apply=True, runner=runner, snapshot_dir=snap_dir)
    report = format_install_report(result)
    assert result.ok is False
    assert result.exit_code == 1
    assert result.written == []
    assert result.manifest_path == ""
    assert runner.calls == []  # refused before fetch
    assert _tree(dest) == before
    assert "FAIL" in report
    assert "BUSL-1.1" in report or "not installable" in report.lower()
    assert "No files were written" in " ".join(result.messages)


def test_8787_conflict_detected_and_blocks_apply(tmp_path):
    dest = copy_fixture("ecosystem", tmp_path)
    ctx = _ctx(dest)
    before = _tree(dest)
    conflict = detect_8787_conflict(ctx)
    assert conflict is not None
    assert conflict["port"] == 8787
    assert "8787" in conflict["message"]

    dry = install(ctx, apply=False, runner=FakeRunner())
    assert dry.conflict_8787 is not None
    assert dry.exit_code == 2
    assert dry.written == []
    assert _tree(dest) == before
    report = format_install_report(dry)
    assert "8787" in report
    assert "Headroom" in report or "headroom" in report.lower()

    runner = FakeRunner()
    blocked = install(ctx, apply=True, runner=runner, force=False)
    assert blocked.ok is False
    assert blocked.exit_code == 2
    assert blocked.written == []
    assert runner.calls == []
    assert _tree(dest) == before
    assert any("8787" in m for m in blocked.messages)

    # --force may apply despite the conflict; still uses the fake runner.
    forced = install(ctx, apply=True, runner=FakeRunner(), force=True)
    assert forced.written
    assert forced.manifest_path
    # Telemetry off is still written (ecosystem starts with rtk telemetry = true).
    rtk = (dest / "home" / ".config" / "rtk" / "config.toml").read_text(encoding="utf-8")
    assert "telemetry = false" in rtk
    undo_manifest(Path(forced.manifest_path), force=False)
    assert _tree(dest) == before


def test_live_runner_refuses_under_pytest():
    with pytest.raises(NetworkInstallRefused):
        LiveRunner().run(["echo", "should-not-run"], network=True)


def test_packaged_license_snapshots_match_clean_pins():
    verdicts = check_licenses()
    assert {v.tool_id for v in verdicts} >= {"rtk-oss", "caveman-skill", "tokeneconomizer", "ccusage"}
    assert all(v.ok for v in verdicts)
    by_id = {v.tool_id: v for v in verdicts}
    assert by_id["rtk-oss"].license == "Apache-2.0"
    assert by_id["caveman-skill"].license == "Apache-2.0"
    assert by_id["tokeneconomizer"].license == "Apache-2.0"
    assert by_id["ccusage"].license == "MIT"


def test_cli_undo_uses_latest_manifest(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    ctx = _ctx(dest)
    before = _tree(dest)
    applied = install(ctx, apply=True, runner=FakeRunner())
    assert latest_manifest(ctx) == Path(applied.manifest_path)

    from tokenstretch.cli import cmd_undo

    args = Namespace(
        home=dest / "home",
        root=dest / "root",
        sys_snapshot=dest / "sys",
        plan=None,
        manifest=None,
        manifest_flag=None,
        force=False,
        json=False,
    )
    buf, err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        code = cmd_undo(args)
    assert code == 0
    assert "Restored" in buf.getvalue() or "deleted" in buf.getvalue().lower()
    assert _tree(dest) == before


def test_rtk_asset_allow_list_refuses_busl():
    from tokenstretch.catalog import PinMismatchError, rtk_asset_allowed, rtk_asset_refusal, verify_rtk_asset

    assert rtk_asset_allowed("rtk-oss-x86_64-unknown-linux-musl.tar.gz")
    assert rtk_asset_allowed("rtk-oss_1.2.0-1_amd64.deb")
    assert rtk_asset_allowed("rtk-oss_amd64.deb")
    assert not rtk_asset_allowed("rtk-x86_64-unknown-linux-musl.tar.gz")
    assert not rtk_asset_allowed("rtk-proxy-x86_64-unknown-linux-musl.tar.gz")
    assert "BUSL" in rtk_asset_refusal("rtk-x86_64-apple-darwin.tar.gz")
    assert "BUSL" in rtk_asset_refusal("rtk-proxy-aarch64-apple-darwin.tar.gz")
    with pytest.raises(PinMismatchError, match="BUSL"):
        verify_rtk_asset("rtk-x86_64-unknown-linux-musl.tar.gz", b"anything")
    with pytest.raises(PinMismatchError, match="BUSL"):
        verify_rtk_asset("rtk-proxy-x86_64-pc-windows-msvc.zip", b"anything")


def test_hash_mismatch_refuses_and_writes_nothing(tmp_path):
    from tokenstretch.catalog import PinMismatchError, RTK_INSTALL_SH_SHA256, verify_rtk_install_sh, verify_rtk_asset
    from tokenstretch.installer import InstallArtifacts

    dest = copy_fixture("clean", tmp_path)
    ctx = _ctx(dest)
    before = _tree(dest)
    bad = tmp_path / "install.sh"
    bad.write_bytes(b"not the pinned install.sh")
    with pytest.raises(PinMismatchError, match="does not match pinned"):
        verify_rtk_install_sh(bad.read_bytes())

    # Allowed name, wrong bytes — refused for hash mismatch, not for the allow-list.
    with pytest.raises(PinMismatchError, match="does not match pinned"):
        verify_rtk_asset("rtk-oss-x86_64-unknown-linux-musl.tar.gz", b"wrong-bytes")

    runner = FakeRunner()
    result = install(
        ctx, apply=True, runner=runner,
        artifacts=InstallArtifacts(rtk_install_sh=bad),
    )
    assert result.ok is False
    assert result.exit_code == 1
    assert result.written == []
    assert runner.calls == []  # refused before fetch
    assert _tree(dest) == before
    assert "does not match pinned" in " ".join(result.messages)
    assert RTK_INSTALL_SH_SHA256 not in __import__("hashlib").sha256(b"not the pinned install.sh").hexdigest()


def test_caveman_sha_mismatch_refuses_and_skips_setup(tmp_path):
    from tokenstretch.catalog import CAVEMAN_COMMIT, PinMismatchError, verify_commit
    from tokenstretch.installer import InstallArtifacts

    dest = copy_fixture("clean", tmp_path)
    ctx = _ctx(dest)
    before = _tree(dest)

    repo = tmp_path / "caveman-src"
    (repo / "skills" / "caveman").mkdir(parents=True)
    (repo / "skills" / "caveman" / "SKILL.md").write_text("# caveman skill\n")
    (repo / "skills" / "caveman-setup" / "SKILL.md").mkdir(parents=True) if False else None
    (repo / "skills" / "caveman-setup").mkdir(parents=True)
    (repo / "skills" / "caveman-setup" / "SKILL.md").write_text("# setup — must not be copied\n")
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("0000000000000000000000000000000000000000\n")

    with pytest.raises(PinMismatchError, match="does not match pinned"):
        verify_commit("0000000000000000000000000000000000000000", CAVEMAN_COMMIT, what="Caveman")

    runner = FakeRunner()
    result = install(
        ctx, apply=True, runner=runner,
        artifacts=InstallArtifacts(caveman_repo=repo),
    )
    assert result.ok is False
    assert result.exit_code == 1
    assert result.written == []
    assert runner.calls == []
    assert _tree(dest) == before
    assert "Caveman" in " ".join(result.messages)
    assert not (dest / "home" / ".claude" / "skills" / "caveman").exists()
    assert not (dest / "home" / ".agents" / "skills" / "caveman").exists()
    assert not (dest / "home" / ".claude" / "skills" / "caveman-setup").exists()

    # A verified repo copies only skills/caveman into adapter-owned dirs.
    (repo / ".git" / "HEAD").write_text(CAVEMAN_COMMIT + "\n")
    ok = install(ctx, apply=True, runner=FakeRunner(), artifacts=InstallArtifacts(caveman_repo=repo))
    assert ok.ok is True
    skill = dest / "home" / ".claude" / "skills" / "caveman" / "SKILL.md"
    codec = dest / "home" / ".agents" / "skills" / "caveman" / "SKILL.md"
    assert skill.read_text(encoding="utf-8") == "# caveman skill\n"
    assert codec.read_text(encoding="utf-8") == "# caveman skill\n"
    assert not (dest / "home" / ".claude" / "skills" / "caveman-setup").exists()
    assert not (dest / "home" / ".agents" / "skills" / "caveman-setup").exists()


def test_te_clean_config_contents(tmp_path):
    from tokenstretch.catalog import TE_CLEAN_CONFIG

    dest = copy_fixture("clean", tmp_path)
    ctx = _ctx(dest)
    result = install(ctx, apply=True, runner=FakeRunner())
    assert result.ok
    path = dest / "home" / ".tokeneconomizer" / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == TE_CLEAN_CONFIG
    assert data == {"microskills": "off", "toolprune": "propose", "loopguard": "off"}
    # The old inferred boolean form is gone.
    assert data.get("microskills") != False


# ---------------------------------------------------------------------------
# Opt-in API-key proxy profile
# ---------------------------------------------------------------------------

CANARY_ANTHROPIC = "CANARY_ANTHROPIC_KEY_MUST_NOT_LEAK"
CANARY_CODEX = "CANARY_CODEX_KEY_MUST_NOT_LEAK"


def _api_key_home(dest: Path):
    """Fixture home with no credential files and API-key env *names* set (values never written)."""
    cred = dest / "home" / ".claude" / ".credentials.json"
    if cred.exists():
        cred.unlink()
    auth = dest / "home" / ".codex" / "auth.json"
    if auth.exists():
        auth.unlink()
    ctx = _ctx(dest)
    ctx.env["ANTHROPIC_API_KEY"] = CANARY_ANTHROPIC
    ctx.env["CODEX_API_KEY"] = CANARY_CODEX
    return ctx


def _blob_under(dest: Path) -> str:
    parts = []
    for p in dest.rglob("*"):
        if p.is_file():
            try:
                parts.append(p.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    return "\n".join(parts)


def test_api_proxy_refuses_oauth_subscription(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    # Stat-only signal: touch a credential file, never open it in the installer.
    cred = dest / "home" / ".claude" / ".credentials.json"
    cred.write_bytes(b"")
    ctx = _ctx(dest)
    # No ANTHROPIC_API_KEY in ctx.env.
    before = _tree(dest)
    result = install(ctx, profile="api-proxy", proxy="headroom", runner=FakeRunner())
    assert result.ok is False
    assert result.exit_code == 2
    assert result.written == []
    assert result.manifest_path == ""
    blob = " ".join(result.messages)
    assert "subscription" in blob.lower() or "OAuth" in blob or "oauth" in blob.lower()
    assert _tree(dest) == before

    applied = install(ctx, profile="api-proxy", proxy="headroom", apply=True, runner=FakeRunner(),
                      allow_unpinned_for_tests=True)
    assert applied.ok is False
    assert applied.exit_code == 2
    assert applied.written == []
    assert _tree(dest) == before


def test_api_proxy_refuses_both_proxies(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    ctx = _api_key_home(dest)
    before = _tree(dest)
    for proxy in (["headroom", "kompressor"], "headroom,kompressor"):
        result = install(ctx, profile="api-proxy", proxy=proxy, runner=FakeRunner())
        assert result.ok is False
        assert result.exit_code == 2
        blob = " ".join(result.messages).lower()
        assert "at most one" in blob or "both" in blob
        assert result.written == []
        assert _tree(dest) == before


def test_api_proxy_refuses_wildcard_bind(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    ctx = _api_key_home(dest)
    before = _tree(dest)
    for bind in ("0.0.0.0", "0.0.0.0:8787", "*"):
        result = install(ctx, profile="api-proxy", proxy="headroom", bind=bind, runner=FakeRunner())
        assert result.ok is False
        assert result.exit_code == 2
        blob = " ".join(result.messages)
        assert "0.0.0.0" in blob or "127.0.0.1" in blob
        assert result.written == []
        assert _tree(dest) == before


def test_headroom_lock_contains_pinned_wheel_hash():
    from tokenstretch.catalog import (
        HEADROOM_WHEEL_SHA256_LINUX_X86_64,
        headroom_lock_contains_wheel_hash,
        headroom_packaged_lock,
        verify_headroom_lock,
    )

    lock = headroom_packaged_lock()
    assert lock.is_file()
    text = lock.read_text(encoding="utf-8")
    assert "headroom-ai[proxy]==0.44.0" in text
    assert HEADROOM_WHEEL_SHA256_LINUX_X86_64 == "3b5c7fd289e7d227dcd9b896615541ea9d3d37388bd97b3257fc8769c4b7ef70"
    assert HEADROOM_WHEEL_SHA256_LINUX_X86_64 in text
    assert headroom_lock_contains_wheel_hash(lock) is True
    assert verify_headroom_lock(lock) == HEADROOM_WHEEL_SHA256_LINUX_X86_64


def test_headroom_apply_uses_require_hashes_and_beacon_env(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    ctx = _api_key_home(dest)
    dry = install(ctx, profile="api-proxy", proxy="headroom", runner=FakeRunner())
    assert dry.dry_run is True
    assert dry.ok is True
    assert dry.exit_code == 0
    report = format_install_report(dry)
    argv_blob = " ".join(" ".join(s.get("argv") or []) for s in dry.fetch_steps)
    env_blob = " ".join(
        f"{k}={v}" for s in dry.fetch_steps for k, v in (s.get("env") or {}).items()
    )
    assert "--require-hashes" in argv_blob
    assert "uv pip install" in argv_blob or ("uv" in argv_blob and "pip" in argv_blob and "install" in argv_blob)
    assert "uv tool install" not in argv_blob
    assert "--python" in argv_blob and "3.13" in argv_blob
    assert "tools/headroom" in argv_blob.replace("\\", "/")
    assert "UV_NO_PROGRESS" in env_blob
    assert "require-hashes" in report or "--require-hashes" in argv_blob
    assert "--no-telemetry" in " ".join(dry.notices)
    assert "8788" in " ".join(dry.notices) or "8788" in report
    assert "/v1" in " ".join(dry.notices) or "8787/v1" in report

    from tokenstretch.api_proxy import HEADROOM_TELEMETRY_ENV, plan_proxy_file_changes
    planned = plan_proxy_file_changes(ctx, "headroom", "127.0.0.1")
    settings = next(ch for ch in planned if ch["path"].endswith("/.claude/settings.json"))
    env = json.loads(settings["content"]).get("env") or {}
    for key, val in HEADROOM_TELEMETRY_ENV.items():
        assert env.get(key) == val, key
    assert env.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:8787"
    assert not str(env.get("ANTHROPIC_BASE_URL")).endswith("/v1")
    assert "HEADROOM_ALLOW_UNAUTHENTICATED_BIND" not in env
    assert "HEADROOM_PROXY_TOKEN" not in env
    codex = next(ch for ch in planned if ch["path"].endswith("/.codex/config.toml"))
    assert 'openai_base_url = "http://127.0.0.1:8787/v1"' in codex["content"]


def test_headroom_macos_below_14_refused(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    ctx = _api_key_home(dest)
    ctx.platform = "darwin"
    ctx.os_version = "13.6.1"
    before = _tree(dest)
    dry = install(ctx, profile="api-proxy", proxy="headroom", runner=FakeRunner())
    assert dry.ok is False
    assert dry.exit_code == 1
    assert dry.written == []
    blob = " ".join(dry.messages) + " " + " ".join(dry.notices)
    assert "14.0" in blob
    assert "onnxruntime" in blob.lower() or "macosx_14" in blob.lower()
    assert "not been install-tested" in blob or "not been install-tested on a real Mac" in blob
    applied = install(ctx, profile="api-proxy", proxy="headroom", apply=True, runner=FakeRunner())
    assert applied.ok is False
    assert applied.exit_code == 1
    assert applied.written == []
    assert applied.manifest_path == ""
    assert _tree(dest) == before


def test_headroom_apply_undo_round_trip_and_doctor(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    ctx = _api_key_home(dest)
    before = _tree(dest)
    before_empty = _empty_dirs_outside_kit(dest)
    runner = FakeRunner()

    applied = install(
        ctx,
        profile="api-proxy",
        proxy="headroom",
        apply=True,
        runner=runner,
    )
    assert applied.ok is True
    assert applied.dry_run is False
    assert applied.exit_code == 0
    assert applied.profile == "api-proxy"
    assert applied.proxy == "headroom"
    assert applied.written
    assert applied.manifest_path
    manifest = json.loads(Path(applied.manifest_path).read_text(encoding="utf-8"))
    assert manifest["profile"] == "api-proxy"
    assert manifest.get("proxy") == "headroom"
    assert manifest.get("bind") == "127.0.0.1"

    argv_blob = " ".join(" ".join(c.argv) for c in runner.calls)
    env_blob = " ".join(f"{k}={v}" for c in runner.calls for k, v in (c.env or {}).items())
    assert "--require-hashes" in argv_blob
    assert "uv" in argv_blob and "pip" in argv_blob and "install" in argv_blob
    assert "uv tool install" not in argv_blob
    assert "UV_NO_PROGRESS" in env_blob
    assert all(c.skipped for c in runner.calls)

    settings_path = dest / "home" / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    env = settings.get("env") or {}
    assert env.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:8787"
    assert env.get("HEADROOM_BEACON") == "off"
    assert env.get("HEADROOM_UPDATE_CHECK") == "off"
    assert "HEADROOM_ALLOW_UNAUTHENTICATED_BIND" not in env

    codex_cfg = (dest / "home" / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert 'openai_base_url = "http://127.0.0.1:8787/v1"' in codex_cfg
    assert 'openai_base_url = "http://127.0.0.1:8787"' not in codex_cfg

    lock_path = dest / "home" / ".tokenstretch" / "headroom" / "requirements.txt"
    assert lock_path.is_file()
    assert "3b5c7fd289e7d227dcd9b896615541ea9d3d37388bd97b3257fc8769c4b7ef70" in lock_path.read_text(encoding="utf-8")
    wrapper = dest / "home" / ".tokenstretch" / "bin" / "headroom"
    assert wrapper.is_file()
    body = wrapper.read_text(encoding="utf-8")
    assert "tools/headroom/bin/headroom" in body

    leaked = _blob_under(dest)
    assert CANARY_ANTHROPIC not in leaked
    assert CANARY_CODEX not in leaked

    from tokenstretch.adapters import REGISTRY
    from tokenstretch.util import is_loopback_url
    from tokenstretch.adapters.host import _local_compression_proxy_label

    assert is_loopback_url("http://127.0.0.1:8787/v1") is True
    assert _local_compression_proxy_label("http://127.0.0.1:8787/v1")
    fresh = _api_key_home(dest)
    findings = []
    for ad in REGISTRY:
        if not ad.detect(fresh):
            continue
        findings += ad.checks(fresh) + ad.privacy_checks(fresh)
    ids = {f.id for f in findings}
    assert "W10" in ids
    assert "P02" in ids
    w10 = [f for f in findings if f.id == "W10"]
    p02 = [f for f in findings if f.id == "P02"]
    blob = " ".join(
        (f.message or "") + " " + str((f.evidence or {}).get("value") or "")
        for f in w10 + p02
    )
    assert "8787" in blob
    assert any(
        "8787/v1" in (f.message or "")
        or str((f.evidence or {}).get("value") or "").endswith("/v1")
        for f in w10 + p02
    )
    assert any(f.severity == "info" for f in p02)

    undone = undo_manifest(Path(applied.manifest_path), force=False)
    assert undone["restored"] or undone["deleted"]
    assert _tree(dest) == before
    assert _empty_dirs_outside_kit(dest) == before_empty
    if settings_path.is_file():
        after = settings_path.read_text(encoding="utf-8")
        assert "ANTHROPIC_BASE_URL" not in after
        assert "HEADROOM_BEACON" not in after
    if (dest / "home" / ".codex" / "config.toml").is_file():
        assert "8787/v1" not in (dest / "home" / ".codex" / "config.toml").read_text(encoding="utf-8")


def test_api_proxy_not_part_of_clean(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    ctx = _api_key_home(dest)
    before = _tree(dest)
    # Default profile stays Clean: no Headroom/Kompressor files, and --proxy alone is refused.
    clean = install(ctx, apply=False, runner=FakeRunner())
    assert clean.profile == "clean"
    assert clean.proxy is None
    assert all(s["tool"] not in {"headroom", "kompressor"} for s in clean.fetch_steps)
    refused = install(ctx, profile="clean", proxy="headroom", runner=FakeRunner())
    assert refused.ok is False
    assert "never part of Clean" in " ".join(refused.messages) or "api-proxy" in " ".join(refused.messages).lower()
    assert _tree(dest) == before


def _pin_host(monkeypatch, system: str, machine: str = "x86_64", musl: bool = False) -> None:
    """Make the Kompressor asset choice independent of the machine running the tests."""
    import platform
    import sys

    from tokenstretch import catalog

    monkeypatch.setattr(sys, "platform", system)
    monkeypatch.setattr(platform, "machine", lambda: machine)
    monkeypatch.setattr(catalog, "_linux_is_musl", lambda: musl)


@pytest.mark.parametrize("system, machine, musl, expected", [
    ("linux", "x86_64", False, "kompressor-1.5.14-x86_64-linux-gnu"),
    ("linux", "amd64", True, "kompressor-1.5.14-x86_64-linux-musl"),
    ("darwin", "arm64", False, "kompressor-1.5.14-universal2.pkg"),
    ("darwin", "x86_64", False, "kompressor-1.5.14-universal2.pkg"),
    ("linux", "aarch64", False, None),
    ("win32", "AMD64", False, None),
])
def test_kompressor_asset_per_platform(monkeypatch, system, machine, musl, expected):
    from tokenstretch.catalog import KOMPRESSOR_SHA256, kompressor_asset_for_host
    _pin_host(monkeypatch, system, machine, musl)
    asset = kompressor_asset_for_host()
    assert asset == (None if expected is None else (expected, KOMPRESSOR_SHA256[expected]))


def test_kompressor_apply_undo_round_trip_and_doctor(tmp_path, monkeypatch):
    _pin_host(monkeypatch, "linux")  # plans the Linux x86_64 gnu asset on any host
    dest = copy_fixture("clean", tmp_path)
    ctx = _api_key_home(dest)
    before = _tree(dest)
    before_empty = _empty_dirs_outside_kit(dest)
    runner = FakeRunner()

    applied = install(
        ctx,
        profile="api-proxy",
        proxy="kompressor",
        apply=True,
        runner=runner,
    )
    assert applied.ok is True
    assert applied.dry_run is False
    assert applied.exit_code == 0
    assert applied.profile == "api-proxy"
    assert applied.proxy == "kompressor"
    assert applied.written
    assert applied.manifest_path
    manifest = json.loads(Path(applied.manifest_path).read_text(encoding="utf-8"))
    assert manifest["profile"] == "api-proxy"
    assert manifest.get("proxy") == "kompressor"
    assert manifest.get("bind") == "127.0.0.1"

    settings_path = dest / "home" / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    env = settings.get("env") or {}
    assert env.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:8817"
    assert env.get("KOMPRESSOR__SERVER__LISTEN") == "127.0.0.1:8817"
    assert env.get("KOMPRESSOR__PROXY__WEBSOCKET_RELAY") == "false"
    assert "KOMPRESSOR__PROXY__CACHE_KEEP_WARM" not in env
    assert "KOMPRESSOR__PROXY__PROVIDERS__ANTHROPIC__CACHE_KEEP_WARM" not in env
    assert "HTTPS_PROXY" not in env
    assert "NODE_EXTRA_CA_CERTS" not in env
    assert "0.0.0.0" not in json.dumps(settings)

    hook_blob = json.dumps(settings.get("hooks") or {})
    assert "rtk" in hook_blob

    kcfg = (dest / "home" / ".config" / "kompressor" / "config.toml").read_text(encoding="utf-8")
    assert 'listen = "127.0.0.1:8817"' in kcfg
    assert "websocket_relay = false" in kcfg
    assert "cache_keep_warm" not in kcfg
    assert not any(line.strip() == "[proxy.intercept]" for line in kcfg.splitlines())
    assert "/etc/kompressor" in kcfg  # comment warning only
    assert "0.0.0.0" not in kcfg.split("[")[0] or "0.0.0.0:8817" in kcfg  # Dockerfile warning in comments is ok
    # The listen value itself must not be 0.0.0.0
    assert 'listen = "0.0.0.0' not in kcfg

    codex_cfg = (dest / "home" / ".codex" / "config.toml").read_text(encoding="utf-8")
    assert 'openai_base_url = "http://127.0.0.1:8817"' in codex_cfg

    leaked = _blob_under(dest)
    assert CANARY_ANTHROPIC not in leaked
    assert CANARY_CODEX not in leaked

    argv_blob = " ".join(" ".join(c.argv) for c in runner.calls)
    assert "kompressor-1.5.14-x86_64-linux-gnu" in argv_blob
    assert "github.com/hystax/optscale-kompressor/releases/download/1.5.14" in argv_blob
    assert "docker" not in argv_blob.lower()
    assert "0.0.0.0" not in argv_blob
    # cargo is documented as a fallback, not the apply fetch
    assert not any(c.argv and c.argv[0] == "cargo" for c in runner.calls)
    assert all(c.skipped for c in runner.calls)

    from tokenstretch.adapters import REGISTRY
    fresh = _api_key_home(dest)
    findings = []
    for ad in REGISTRY:
        if not ad.detect(fresh):
            continue
        findings += ad.checks(fresh) + ad.privacy_checks(fresh)
    ids = {f.id for f in findings}
    assert "W10" in ids
    assert "P02" in ids
    w10 = [f for f in findings if f.id == "W10"]
    p02 = [f for f in findings if f.id == "P02"]
    assert any("8817" in (f.message or "") for f in w10 + p02)
    assert any(f.severity == "info" for f in p02)
    # Relay is off, intercept is off, listen is loopback: no T01/P04 from those.
    t01 = [f for f in findings if f.id == "T01" and "websocket_relay" in (f.message or "")]
    assert t01 == []
    p04_bad = [f for f in findings if f.id == "P04" and ("intercept" in (f.message or "").lower() or "0.0.0.0" in (f.message or ""))]
    assert p04_bad == []

    undone = undo_manifest(Path(applied.manifest_path), force=False)
    assert undone["restored"] or undone["deleted"]
    assert _tree(dest) == before
    assert _empty_dirs_outside_kit(dest) == before_empty
    if settings_path.is_file():
        after = settings_path.read_text(encoding="utf-8")
        assert "ANTHROPIC_BASE_URL" not in after
        assert "KOMPRESSOR__SERVER__LISTEN" not in after


@pytest.mark.skipif(
    sys.platform == "win32", reason="Kompressor ships pinned binaries for Linux x86_64 and macOS only, none for Windows")
def test_kompressor_hash_mismatch_refused(tmp_path):
    from tokenstretch.catalog import (
        PinMismatchError,
        KOMPRESSOR_SHA256,
        kompressor_asset_for_host,
        verify_kompressor_asset,
    )
    from tokenstretch.installer import InstallArtifacts

    dest = copy_fixture("clean", tmp_path)
    ctx = _api_key_home(dest)
    before = _tree(dest)
    asset = kompressor_asset_for_host()
    assert asset is not None, "this platform should have a pinned linux x86_64 or macOS Kompressor asset"
    name, expected = asset
    assert expected == KOMPRESSOR_SHA256[name]
    bad = tmp_path / name
    bad.write_bytes(b"not-the-pinned-kompressor-binary")
    with pytest.raises(PinMismatchError, match="does not match pinned"):
        verify_kompressor_asset(name, bad.read_bytes())

    runner = FakeRunner()
    result = install(
        ctx,
        profile="api-proxy",
        proxy="kompressor",
        apply=True,
        runner=runner,
        artifacts=InstallArtifacts(kompressor_binary=bad, kompressor_asset_name=name),
    )
    assert result.ok is False
    assert result.exit_code == 1
    assert result.written == []
    assert result.manifest_path == ""
    assert runner.calls == []  # refused before fetch
    assert _tree(dest) == before
    assert "does not match pinned" in " ".join(result.messages)


def test_api_proxy_8787_clash(tmp_path):
    dest = copy_fixture("ecosystem", tmp_path)
    ctx = _api_key_home(dest)
    before = _tree(dest)
    conflict = detect_8787_conflict(ctx)
    assert conflict is not None

    dry = install(ctx, profile="api-proxy", proxy="headroom", runner=FakeRunner())
    assert dry.ok is False
    assert dry.exit_code == 2
    assert dry.conflict_8787
    blob = " ".join(dry.messages)
    assert "8787" in blob
    assert "Caveman" in blob or "Headroom" in blob
    assert dry.written == []
    assert _tree(dest) == before

    applied = install(
        ctx,
        profile="api-proxy",
        proxy="headroom",
        apply=True,
        runner=FakeRunner(),
    )
    assert applied.ok is False
    assert applied.exit_code == 2
    assert applied.written == []
    assert applied.manifest_path == ""
    assert "Refusing --apply" in " ".join(applied.messages)
    assert "8787" in " ".join(applied.messages)
    assert _tree(dest) == before


def test_api_proxy_license_snapshots_match_pins():
    from tokenstretch.catalog import (
        HEADROOM_COMMIT,
        HEADROOM_SDIST_SHA256,
        KOMPRESSOR_COMMIT,
        api_proxy_tools,
    )

    verdicts = check_licenses(api_proxy_tools())
    assert {v.tool_id for v in verdicts} == {"headroom", "kompressor"}
    assert all(v.ok for v in verdicts)
    by_id = {v.tool_id: v for v in verdicts}
    assert by_id["headroom"].license == "Apache-2.0"
    assert by_id["kompressor"].license == "MIT"
    snaps = load_snapshots()
    assert snaps["headroom"]["checksum"] == HEADROOM_SDIST_SHA256
    assert snaps["headroom"]["commit"] == HEADROOM_COMMIT
    from tokenstretch.catalog import HEADROOM_WHEEL_SHA256_LINUX_X86_64, headroom_packaged_lock
    assert HEADROOM_WHEEL_SHA256_LINUX_X86_64 in headroom_packaged_lock().read_text(encoding="utf-8")
    assert snaps["kompressor"]["commit"] == KOMPRESSOR_COMMIT
    assert snaps["kompressor"]["sha256_linux_musl"] == "4ec4db8e6e1b5ea2bc15f05b804e8c127233e876a92399a6b99ff40c40be17f1"
    assert snaps["kompressor"]["sha256_linux_gnu"] == "5fa88e9e7991cb1c63d3be8eb2ba7a56ea7dbd5db7afed7d74798f70534edae0"


def test_cli_has_no_unpinned_bypass_and_refuses_both_proxies(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    cred = dest / "home" / ".claude" / ".credentials.json"
    if cred.exists():
        cred.unlink()
    import inspect
    from tokenstretch import cli as cli_mod
    from tokenstretch.cli import cmd_install

    src = inspect.getsource(cli_mod)
    assert "allow_unpinned_for_tests" not in src

    args = Namespace(
        home=dest / "home",
        root=dest / "root",
        sys_snapshot=dest / "sys",
        plan=None,
        apply=False,
        force=False,
        license_snapshots=None,
        json=False,
        profile="api-proxy",
        proxy=["headroom", "kompressor"],
        bind="127.0.0.1",
    )
    buf, err = io.StringIO(), io.StringIO()
    before = _tree(dest)
    with redirect_stdout(buf), redirect_stderr(err):
        code = cmd_install(args)
    assert code == 2
    out = buf.getvalue() + err.getvalue()
    assert "at most one" in out.lower() or "both" in out.lower()
    assert _tree(dest) == before


def test_doctor_warns_kompressor_intercept_relay_and_non_loopback(tmp_path):
    from tokenstretch.adapters.host import HostAdapter

    dest = copy_fixture("clean", tmp_path)
    ctx = _api_key_home(dest)
    cfg_dir = dest / "home" / ".config" / "kompressor"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.toml").write_text(
        '[server]\nlisten = "0.0.0.0:8817"\n\n'
        "[proxy]\nwebsocket_relay = true\n\n"
        '[proxy.intercept]\nlisten = "127.0.0.1:8840"\n',
        encoding="utf-8",
    )
    host = HostAdapter()
    findings = host.checks(ctx) + host.privacy_checks(ctx)
    messages = " ".join(f.message for f in findings)
    assert any(f.id == "T01" and "websocket_relay" in f.message for f in findings)
    assert any(f.id == "P04" and "intercept" in f.message.lower() for f in findings)
    assert any(f.id == "P04" and "0.0.0.0" in f.message for f in findings)
    assert "loopback" in messages.lower() or "127.0.0.1" in messages
