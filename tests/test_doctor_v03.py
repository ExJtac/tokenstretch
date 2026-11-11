"""v0.3 Claude Code checks W19-W27 (gaps found against rsaryev/tokendoctor).

All read-only: settings/frontmatter reads and lockfile stat. The fixture holds a credential
canary; run_doctor's credential_guard fails the test if any check opens it.
"""
from __future__ import annotations

import io
import json
import shutil
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout

from tokenstretch.adapters import implemented_check_ids
from tests.conftest import FIXTURES, credential_guard, finding_ids, findings_with, run_doctor

NEW = ("W19", "W20", "W21", "W22", "W23", "W24", "W25", "W26", "W27")


def _doctor_tree(dest):
    from tokenstretch.cli import doctor

    args = Namespace(home=dest / "home", root=dest / "root", sys_snapshot=dest / "sys",
                     plan=None, privacy=False, json=True)
    buf = io.StringIO()
    with credential_guard(), redirect_stdout(buf), redirect_stderr(io.StringIO()):
        doctor(args)
    return json.loads(buf.getvalue())


def _tree(tmp_path):
    dest = tmp_path / "t"
    shutil.copytree(FIXTURES / "claude-v03", dest)
    return dest


def test_new_ids_are_registered():
    assert set(NEW) <= implemented_check_ids()


def test_fixture_fires_w19_to_w26(tmp_path):
    _, findings, _, _ = run_doctor("claude-v03", tmp_path)
    ids = finding_ids(findings, "claude-code")
    for i in NEW[:-1]:
        assert i in ids, f"missing {i}: {sorted(ids)}"
    assert "W27" not in ids  # no lockfile in the committed fixture


def test_w19_names_the_broken_file(tmp_path):
    _, findings, _, _ = run_doctor("claude-v03", tmp_path)
    w19 = findings_with(findings, "W19", "claude-code")
    assert len(w19) == 1 and w19[0]["severity"] == "med"
    assert w19[0]["evidence"]["file"].endswith("settings.local.json")


def test_w20_w21_compaction_off(tmp_path):
    _, findings, _, _ = run_doctor("claude-v03", tmp_path)
    w20 = findings_with(findings, "W20", "claude-code")[0]
    assert w20["severity"] == "med" and w20["evidence"]["key"] == "autoCompactEnabled"
    assert findings_with(findings, "W21", "claude-code")[0]["severity"] == "info"


def test_w20_env_var(tmp_path):
    dest = _tree(tmp_path)
    s = dest / "home/.claude/settings.json"
    data = json.loads(s.read_text())
    data.pop("autoCompactEnabled")
    data["env"]["DISABLE_AUTO_COMPACT"] = "1"
    s.write_text(json.dumps(data))
    w20 = [f for f in _doctor_tree(dest) if f["id"] == "W20"]
    assert w20 and w20[0]["evidence"]["key"] == "DISABLE_AUTO_COMPACT"


def test_w22_lists_each_invalid_compaction_value(tmp_path):
    _, findings, _, _ = run_doctor("claude-v03", tmp_path)
    keys = {f["evidence"]["key"] for f in findings_with(findings, "W22", "claude-code")}
    assert keys == {"autoCompactThreshold", "autoCompactWindow",
                    "CLAUDE_CODE_AUTO_COMPACT_WINDOW", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"}


def test_w22_accepts_documented_values(tmp_path):
    dest = _tree(tmp_path)
    s = dest / "home/.claude/settings.json"
    data = json.loads(s.read_text())
    data.pop("autoCompactThreshold")
    data["autoCompactWindow"] = 400000
    data["env"]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] = "70"
    s.write_text(json.dumps(data))
    (dest / "sys/env.json").write_text(json.dumps({"PATH": "", "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200k"}))
    assert not [f for f in _doctor_tree(dest) if f["id"] == "W22"]


def test_w23_lists_inheriting_agents_only(tmp_path):
    _, findings, _, _ = run_doctor("claude-v03", tmp_path)
    w23 = findings_with(findings, "W23", "claude-code")[0]
    files = w23["evidence"]["file"]
    assert "searcher.md" in files and "reviewer.md" in files and "tester.md" not in files


def test_w23_quiet_when_subagent_model_set(tmp_path):
    dest = _tree(tmp_path)
    s = dest / "home/.claude/settings.json"
    data = json.loads(s.read_text())
    data["env"]["CLAUDE_CODE_SUBAGENT_MODEL"] = "sonnet"
    s.write_text(json.dumps(data))
    assert not [f for f in _doctor_tree(dest) if f["id"] == "W23"]


def test_w24_w25_w26(tmp_path):
    _, findings, _, _ = run_doctor("claude-v03", tmp_path)
    assert str(findings_with(findings, "W24", "claude-code")[0]["evidence"]["value"]) == "100000"
    # 16 listed skills; manual-only and both deploy copies set disable-model-invocation.
    assert str(findings_with(findings, "W25", "claude-code")[0]["evidence"]["value"]) == "16"
    assert findings_with(findings, "W26", "claude-code")[0]["evidence"]["value"] == ["deploy"]


def test_w27_large_lockfile_stat_only_and_deny_rule_quiets_it(tmp_path):
    dest = _tree(tmp_path)
    (dest / "root/package-lock.json").write_bytes(b" " * 250_000)
    (dest / "root/yarn.lock").write_bytes(b" " * 1000)  # small: not flagged
    w27 = [f for f in _doctor_tree(dest) if f["id"] == "W27"]
    assert w27 and set(w27[0]["evidence"]["value"]) == {"package-lock.json"}
    (dest / "root/.claude/settings.json").write_text(json.dumps({"permissions": {"deny": ["Read(./package-lock.json)"]}}))
    assert not [f for f in _doctor_tree(dest) if f["id"] == "W27"]


def test_new_checks_quiet_on_clean(tmp_path):
    _, findings, _, _ = run_doctor("clean", tmp_path)
    assert not (finding_ids(findings) & set(NEW))
