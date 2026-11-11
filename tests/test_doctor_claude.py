"""Doctor checks implemented on the Claude Code adapter."""
from __future__ import annotations

from tests.conftest import finding_ids, findings_with, run_doctor


REQUIRED_JSON = {"id", "severity", "evidence", "message", "fix", "source"}


def test_clean_fixture_is_exit_zero(tmp_path):
    code, findings, _, _ = run_doctor("clean", tmp_path)
    assert code == 0
    actionable = [f for f in findings if f["severity"] in {"high", "med", "low"}]
    assert actionable == []
    # First-party privacy info may still appear.
    assert finding_ids(findings, "claude-code") <= {"P01", "P03"}
    for f in findings:
        assert REQUIRED_JSON <= set(f)


def test_bloated_fires_implemented_waste_checks(tmp_path):
    code, findings, _, _ = run_doctor("bloated", tmp_path)
    ids = finding_ids(findings, "claude-code")
    for expected in ("W01", "W03", "W04", "W05", "W06", "W07", "W08", "W09", "W10", "W13", "W14", "W15", "W16"):
        assert expected in ids, f"missing {expected} in {ids}"
    assert "T01" not in ids  # apiKeyHelper => API mode, not a subscription-proxy ToS hit
    # W01: both the per-file line threshold and the 25 KB total
    w01 = findings_with(findings, "W01", "claude-code")
    assert any(f["severity"] == "med" for f in w01)
    assert any(f["severity"] == "high" for f in w01)
    unused = findings_with(findings, "W03", "claude-code")[0]
    assert "idle-docs" in str(unused["evidence"])
    assert unused["severity"] == "med"  # tool search is off
    assert findings_with(findings, "W07", "claude-code")[0]["severity"] == "high"
    assert findings_with(findings, "W09", "claude-code")[0]["severity"] == "high"
    # Waste highs are not ToS/privacy highs, so exit 1.
    assert code == 1
    w13 = findings_with(findings, "W13", "claude-code")
    assert any(f["severity"] == "med" for f in w13)
    assert any(f["severity"] == "info" for f in w13)


def test_privacy_flag_hides_waste_checks(tmp_path):
    code, findings, _, _ = run_doctor("bloated", tmp_path, privacy=True)
    ids = finding_ids(findings)
    assert ids <= {"P01", "P02", "P03", "P04"}
    assert "W01" not in ids and "T01" not in ids
    # Bloated has a remote MCP server; --privacy must list that off-box setting.
    assert "P02" in ids
    assert findings_with(findings, "P02")[0]["severity"] == "info"
    assert code == 0


def test_w05_w08_evidence_points_at_model(tmp_path):
    _, findings, _, _ = run_doctor("bloated", tmp_path)
    w05 = findings_with(findings, "W05", "claude-code")[0]
    assert "opus" in str(w05["evidence"].get("value", "")).lower()
    w08 = findings_with(findings, "W08", "claude-code")[0]
    assert "SKILL.md" in str(w08["evidence"].get("file", "")) or w08["evidence"].get("value") == "opus"


def test_human_output_masks_and_includes_fix(tmp_path):
    code, _, out, _ = run_doctor("bloated", tmp_path, as_json=False)
    assert code == 1
    assert "fix:" in out and "source:" in out
    assert "W07" in out
