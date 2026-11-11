"""Doctor checks implemented on the Codex adapter."""
from __future__ import annotations

from tests.conftest import finding_ids, findings_with, run_doctor


def test_codex_fixture_fires_waste_checks(tmp_path):
    code, findings, _, _ = run_doctor("codex", tmp_path)
    ids = finding_ids(findings, "codex")
    for expected in ("W02", "W03", "W05", "W06", "W07", "W13", "W14"):
        assert expected in ids, f"missing {expected} in {ids}"
    w02 = findings_with(findings, "W02", "codex")
    assert any(f["severity"] == "med" for f in w02)
    assert any(f["severity"] == "info" for f in w02)  # over project_doc_max_bytes
    assert findings_with(findings, "W05", "codex")[0]["evidence"]["value"] == "gpt-5-pro"
    assert findings_with(findings, "W07", "codex")[0]["severity"] == "high"
    assert "docs" in str(findings_with(findings, "W03", "codex")[0]["evidence"])
    assert code == 1


def test_clean_codex_has_no_waste(tmp_path):
    _, findings, _, _ = run_doctor("clean", tmp_path)
    waste = [f for f in findings if f.get("adapter") == "codex" and f["severity"] in {"high", "med", "low"}]
    assert waste == []


def test_tos_risk_t01_and_t06(tmp_path):
    code, findings, out, dest = run_doctor("tos-risk", tmp_path)
    assert code == 2
    assert findings_with(findings, "T01", "claude-code")
    assert findings_with(findings, "T01", "codex")
    t06 = findings_with(findings, "T06", "codex")
    assert t06 and t06[0]["severity"] == "high"
    assert "backend-api/codex" in str(t06[0]["evidence"].get("value", ""))
    # Subscription proxy also flags W10 on both adapters.
    assert findings_with(findings, "W10", "claude-code")
    assert findings_with(findings, "W10", "codex")
    # Canaries must never appear (also checked in run_doctor).
    assert "CANARY_CREDENTIAL" not in out
    # Files are still on disk (stat-able) after the run.
    assert (dest / "home" / ".claude" / ".credentials.json").exists()
    assert (dest / "home" / ".codex" / "auth.json").exists()


def test_plan_override_api_drops_t01(tmp_path):
    code, findings, _, _ = run_doctor("tos-risk", tmp_path, plan="api")
    assert not findings_with(findings, "T01")
    # T06 is about the unofficial endpoint, not auth mode.
    assert findings_with(findings, "T06", "codex")
    assert code != 2 or findings_with(findings, "T06")  # T06 is still a ToS high
    assert code == 2
