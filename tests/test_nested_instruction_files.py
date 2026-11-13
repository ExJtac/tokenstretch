"""Instruction files deeper in the project are not read on every message, so they are not counted."""
from __future__ import annotations

from pathlib import Path

from tokenstretch.adapters.claude_code import ClaudeCodeAdapter
from tokenstretch.adapters.codex import CodexAdapter
from tokenstretch.context import build_context

BIG = "- a rule line that is long enough to add up quickly\n" * 900  # well over 25 KB


def _world(tmp_path: Path, name: str):
    home, root = tmp_path / "home", tmp_path / "root"
    (home / ".claude").mkdir(parents=True)
    (home / ".codex").mkdir(parents=True)
    deep = root / "tests" / "fixtures" / "sample"
    deep.mkdir(parents=True)
    (root / name).write_text("# Project\n\n- one short rule\n", encoding="utf-8")
    (deep / name).write_text(BIG, encoding="utf-8")
    return build_context(home, root, tmp_path / "sys"), root, deep


def test_claude_nested_file_is_listed_but_not_every_turn(tmp_path):
    ctx, root, deep = _world(tmp_path, "CLAUDE.md")
    ad = ClaudeCodeAdapter()
    files = {Path(f.path).resolve(): f for f in ad.list_instruction_files(ctx)}
    assert files[(root / "CLAUDE.md").resolve()].always_loaded is True
    assert files[(deep / "CLAUDE.md").resolve()].always_loaded is False
    assert ad._w01(ctx) == []


def test_claude_big_root_file_still_warns(tmp_path):
    ctx, root, _ = _world(tmp_path, "CLAUDE.md")
    (root / "CLAUDE.md").write_text(BIG, encoding="utf-8")
    assert any(f.id.startswith("W01") and f.severity == "high" for f in ClaudeCodeAdapter()._w01(ctx))


def test_codex_nested_file_is_listed_but_not_every_turn(tmp_path):
    ctx, root, deep = _world(tmp_path, "AGENTS.md")
    ad = CodexAdapter()
    files = {Path(f.path).resolve(): f for f in ad.list_instruction_files(ctx)}
    assert files[(root / "AGENTS.md").resolve()].always_loaded is True
    assert files[(deep / "AGENTS.md").resolve()].always_loaded is False
    assert ad._w02(ctx) == []
