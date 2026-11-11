"""Host/ecosystem adapter: missing check IDs, one fixture-backed test per ID."""
from __future__ import annotations

from tokenstretch.adapters import implemented_check_ids
from tests.conftest import finding_ids, findings_with, run_doctor

NEW_IDS = ("W10", "W11", "W12", "W17", "W18", "T02", "T03", "T04", "T05", "T07", "P01", "P02", "P04", "P06")


def test_doctor_implements_at_least_30_checks():
    ids = implemented_check_ids()
    assert len(ids) >= 30, sorted(ids)
    # The published set: W01–W18, T01–T07, P00–P06.
    expected = {f"W{i:02d}" for i in range(1, 19)} | {f"T{i:02d}" for i in range(1, 8)} | {f"P{i:02d}" for i in range(0, 7)}
    assert expected <= ids


def test_clean_host_is_quiet(tmp_path):
    code, findings, _, _ = run_doctor("clean", tmp_path)
    assert finding_ids(findings, "host") == set()
    assert code == 0


def test_ecosystem_fires_all_new_check_ids(tmp_path):
    """Sanity: the ecosystem fixture actually exercises every new ID."""
    code, findings, _, _ = run_doctor("ecosystem", tmp_path)
    ids = finding_ids(findings)
    missing = [i for i in NEW_IDS if i not in ids]
    assert missing == [], f"ecosystem fixture missed {missing}; have {sorted(ids)}"
    assert code == 2  # ToS / privacy highs


def _ecosystem(tmp_path):
    return run_doctor("ecosystem", tmp_path)


def test_w10_port_map(tmp_path):
    _, findings, _, _ = _ecosystem(tmp_path)
    hits = findings_with(findings, "W10", "host")
    assert hits
    blob = str(hits[0]["evidence"]) + hits[0]["message"]
    assert "8787" in blob or "Headroom" in blob
    assert "Kompressor" in blob or "8817" in blob or "8840" in blob


def test_w11_dup_compressor(tmp_path):
    _, findings, _, _ = _ecosystem(tmp_path)
    hits = findings_with(findings, "W11", "host")
    assert hits and hits[0]["severity"] == "med"
    tools = str(hits[0]["evidence"].get("value", "")) + hits[0]["message"]
    assert "RTK" in tools and ("Headroom" in tools or "Kompressor" in tools)


def test_w12_multi_router(tmp_path):
    _, findings, _, _ = _ecosystem(tmp_path)
    hits = findings_with(findings, "W12", "host")
    assert hits and hits[0]["severity"] == "high"
    assert "llm-router" in str(hits[0]["evidence"].get("value", "")).lower() or "llm-router" in hits[0]["message"]


def test_w17_dup_instructions(tmp_path):
    _, findings, _, _ = _ecosystem(tmp_path)
    hits = findings_with(findings, "W17", "host")
    assert hits
    names = str(hits[0]["evidence"].get("value", "")) + str(hits[0]["evidence"].get("file", ""))
    assert "CLAUDE.md" in names and "AGENTS.md" in names


def test_w18_auto_approve_default(tmp_path):
    _, findings, _, _ = _ecosystem(tmp_path)
    hits = findings_with(findings, "W18", "cline")
    assert hits and hits[0]["severity"] == "info"
    assert "toolAutoApprove" in str(hits[0]["evidence"]) or "auto-approve" in hits[0]["message"].lower() or "auto-approves" in hits[0]["message"].lower()


def test_t02_cred_reader_does_not_open_files(tmp_path):
    _, findings, out, dest = _ecosystem(tmp_path)
    hits = findings_with(findings, "T02", "host")
    assert hits and all(f["severity"] == "high" for f in hits)
    tools = " ".join(str(f["evidence"].get("value", "")) + f["message"] for f in hits)
    assert "Kompressor" in tools or "magpie" in tools or "claude-mem" in tools or "Caveman" in tools
    assert "CANARY_CREDENTIAL" not in out
    # Canaries are still on disk (stat-able) after the run.
    assert (dest / "home" / ".caveman" / "credentials").exists()
    assert (dest / "home" / ".codex" / "auth.json").exists()


def test_t03_multi_account(tmp_path):
    _, findings, _, _ = _ecosystem(tmp_path)
    hits = findings_with(findings, "T03", "host")
    assert hits and all(f["severity"] == "high" for f in hits)
    blob = " ".join(f["message"] + str(f["evidence"]) for f in hits)
    assert "CLAUDE_CONFIG_DIR" in blob or "rotation" in blob.lower() or "failover" in blob.lower() or "account" in blob.lower()


def test_t04_warmup(tmp_path):
    _, findings, _, _ = _ecosystem(tmp_path)
    hits = findings_with(findings, "T04", "host")
    assert hits
    blob = " ".join(f["message"] + str(f["evidence"]) for f in hits)
    assert "claude -p" in blob or "warm" in blob.lower() or "schedule" in blob.lower() or "keep-warm" in blob.lower() or "keep warm" in blob.lower()


def test_t05_mitm_cert(tmp_path):
    _, findings, _, _ = _ecosystem(tmp_path)
    hits = findings_with(findings, "T05", "host")
    assert hits and all(f["severity"] == "high" for f in hits)
    blob = " ".join(f["message"] + str(f["evidence"]) for f in hits)
    assert "NODE_EXTRA_CA_CERTS" in blob or "HTTPS_PROXY" in blob or "8840" in blob or "kompressor" in blob.lower()


def test_t07_sdk_on_subscription(tmp_path):
    _, findings, _, _ = _ecosystem(tmp_path)
    hits = findings_with(findings, "T07", "host")
    assert hits and hits[0]["severity"] == "high"
    assert "claude-mem" in hits[0]["message"].lower() or "subscription" in hits[0]["message"].lower() or "background" in hits[0]["message"].lower()


def test_p01_third_party_telemetry(tmp_path):
    _, findings, _, _ = _ecosystem(tmp_path)
    hits = findings_with(findings, "P01", "host")
    assert hits and any(f["severity"] == "high" for f in hits)
    blob = " ".join(f["message"] for f in hits)
    # All four tools in the table should be called out when they are installed and not opted out.
    for name in ("Caveman", "Headroom", "claude-mem", "RTK"):
        assert name.lower() in blob.lower(), f"P01 missing {name} in {blob}"


def test_p02_third_party_cloud(tmp_path):
    _, findings, _, _ = _ecosystem(tmp_path)
    hits = findings_with(findings, "P02", "host")
    assert hits
    blob = " ".join(f["message"] + str(f["evidence"]) for f in hits)
    assert "Caveman" in blob or "cmem" in blob.lower() or "openrouter" in blob.lower() or "llm-router" in blob.lower()
    assert any(f["severity"] == "high" for f in hits)


def test_p03_claude_mem_retention(tmp_path):
    _, findings, _, _ = _ecosystem(tmp_path)
    hits = findings_with(findings, "P03", "host")
    assert hits and hits[0]["severity"] == "info"
    assert "claude-mem" in hits[0]["message"].lower() or "screenshot" in hits[0]["message"].lower()


def test_p04_listening_ports(tmp_path):
    _, findings, _, _ = _ecosystem(tmp_path)
    hits = findings_with(findings, "P04", "host")
    assert hits
    exposed = [f for f in hits if f["severity"] == "high"]
    assert exposed, "0.0.0.0:3425 magpie should be high"
    assert any("3425" in str(f["evidence"]) or "magpie" in f["message"].lower() for f in exposed)
    assert any(f["severity"] == "info" and ("8787" in str(f["evidence"]) or "Headroom" in f["message"]) for f in hits)


def test_privacy_mode_lists_every_off_box_setting(tmp_path):
    code, findings, _, _ = run_doctor("ecosystem", tmp_path, privacy=True)
    ids = finding_ids(findings)
    assert "P01" in ids and "P02" in ids and "P04" in ids
    assert "W10" not in ids and "T02" not in ids and "W11" not in ids
    # Third-party telemetry, not just first-party Claude info.
    p01 = findings_with(findings, "P01")
    assert any(f.get("adapter") == "host" and f["severity"] == "high" for f in p01)
    p04_high = [f for f in findings_with(findings, "P04") if f["severity"] == "high"]
    assert p04_high  # exposed listener is a privacy high, so exit 2
    assert code == 2


def test_p06_linux_world_readable(tmp_path):
    """ecosystem credential files are 0644 in the fixture; Linux flags mode only, never contents."""
    code, findings, out, dest = run_doctor("ecosystem", tmp_path)
    hits = findings_with(findings, "P06", "host")
    assert hits
    assert any("0644" in (f.get("evidence") or {}).get("value", "") or "0644" in f.get("message", "") for f in hits)
    # Canaries must never appear (stat only).
    from tests.conftest import CANARIES
    blob = out + str(findings)
    for c in CANARIES:
        assert c not in blob


def test_p06_linux_0600_is_quiet(tmp_path):
    from tests.conftest import copy_fixture
    from tokenstretch.adapters.host import HostAdapter
    from tokenstretch.context import build_context
    dest = copy_fixture("ecosystem", tmp_path)
    for rel in (
        "home/.claude/.credentials.json",
        "home/.codex/auth.json",
        "home/.caveman/credentials",
    ):
        path = dest / rel
        if path.is_file():
            path.chmod(0o600)
    # Pin the snapshot's OS. Without platform.txt the context falls back to the test host's
    # sys.platform, and on macOS the plaintext-Claude-login check (correctly) fires.
    (dest / "sys" / "platform.txt").write_text("linux\n")
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    assert ctx.os_name() == "linux"
    with __import__("tests.conftest", fromlist=["credential_guard"]).credential_guard():
        hits = HostAdapter()._p06(ctx)
    assert hits == []


def test_p06_macos_credentials_fallback(tmp_path):
    from tests.conftest import copy_fixture, credential_guard, CANARIES
    from tokenstretch.adapters.host import HostAdapter
    from tokenstretch.context import build_context
    dest = copy_fixture("clean", tmp_path)
    cred = dest / "home" / ".claude" / ".credentials.json"
    cred.parent.mkdir(parents=True, exist_ok=True)
    cred.write_text("CANARY_CREDENTIAL_DO_NOT_READ_CLAUDE\n", encoding="utf-8")
    cred.chmod(0o600)
    (dest / "sys" / "platform.txt").write_text("darwin\n", encoding="utf-8")
    ctx = build_context(dest / "home", dest / "root", dest / "sys")
    assert ctx.os_name().startswith("darwin")
    with credential_guard():
        hits = HostAdapter()._p06(ctx)
    assert hits and hits[0].id == "P06"
    assert "Keychain" in hits[0].message
    assert hits[0].evidence.get("value") == "0600"
    blob = hits[0].message + str(hits[0].evidence)
    for c in CANARIES:
        assert c not in blob
