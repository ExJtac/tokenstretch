"""trim --to-skills: move workflow sections into Claude Code skills; guards, apply, undo."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from conftest import CANARIES, credential_guard

from tokenstretch.adapters.base import rules_block
from tokenstretch.cli import main

STEPS = "\n".join(f"{i}. Step {i}: run the {w} stage and wait for it to finish cleanly." for i, w in
                  enumerate(["lint", "unit", "build", "package", "upload", "verify", "tag", "announce"], 1))
RELEASE = f"""## Release process

Cut a release from a clean main branch.

{STEPS}

```bash
git tag -a v1.0 -m release
git push origin v1.0
```
"""
SMALL = "## Quick setup\n\n1. one\n2. two\n3. three\n"
RULE = "## Safety rules\n\n" + "\n".join(f"{i}. Never skip check number {i} before running the long deploy steps." for i in range(1, 14)) + "\n"
STYLE = "## Style\n\nKeep replies short.\n"
FENCED = "## Notes\n\n```\n# Release process\n# Deploy steps\n```\n\nPlain notes only.\n"


def _world(tmp_path: Path, project_text: str, user_text: str | None = None) -> Path:
    home, root, sys_ = tmp_path / "home", tmp_path / "root", tmp_path / "sys"
    for d in (home / ".claude", root, sys_):
        d.mkdir(parents=True, exist_ok=True)
    (root / "CLAUDE.md").write_text(project_text, encoding="utf-8")
    if user_text is not None:
        (home / ".claude" / "CLAUDE.md").write_text(user_text, encoding="utf-8")
    return tmp_path


def _run(w: Path, *args: str, cmd: str = "trim", code: int = 0, to_skills: bool = True) -> str:
    buf = io.StringIO()
    argv = [cmd, "--home", str(w / "home"), "--project", str(w / "root"), "--sys-snapshot", str(w / "sys")]
    if to_skills:
        argv.append("--to-skills")
    with credential_guard(), redirect_stdout(buf):
        assert main([*argv, *args]) == code
    out = buf.getvalue()
    for c in CANARIES:
        assert c not in out
    return out


def _undo(w: Path) -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(["undo", "--home", str(w / "home"), "--root", str(w / "root"), "--sys-snapshot", str(w / "sys")]) == 0


def _tree(base: Path) -> dict:
    return {str(p.relative_to(base)): p.read_bytes() for p in sorted(base.rglob("*")) if p.is_file()}


def _doc(*parts: str) -> str:
    return "# Project\n\nIntro line.\n\n" + "\n".join(parts)


def test_dry_run_plans_move_and_writes_nothing(tmp_path):
    w = _world(tmp_path, _doc(STYLE, RELEASE))
    before = _tree(w)
    out = _run(w)
    assert "Planned moves (1)" in out and '"Release process"' in out
    assert ".claude/skills/release-process/SKILL.md" in out
    assert "approx." in out and "net ~" in out and "no longer guaranteed to be read" in out
    assert "--- a/CLAUDE.md" in out and "+- Release process: use the `release-process` skill." in out
    assert "Dry run: nothing was changed" in out
    assert _tree(w) == before and not (w / "home" / ".tokenstretch").exists()


def test_apply_writes_skill_pointer_and_undo_restores(tmp_path):
    src = _doc(STYLE, RELEASE, "## Layout\n\nsrc/ holds code.\n")
    w = _world(tmp_path, src)
    original = (w / "root" / "CLAUDE.md").read_bytes()
    out = _run(w, "--apply", "--yes")
    assert "created (undo deletes it)" in out and "no longer guaranteed to be read" in out
    skill = w / "root" / ".claude" / "skills" / "release-process" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert text.startswith("---\nname: release-process\ndescription: \"")
    desc = text.split("\n")[2]
    assert len(desc) < 220 and "Release process." in desc and "Moved from CLAUDE.md by tokenstretch" in desc
    assert RELEASE.rstrip("\n") in text
    new = (w / "root" / "CLAUDE.md").read_text(encoding="utf-8")
    assert "- Release process: use the `release-process` skill." in new and "Step 1" not in new
    # every other section is byte-identical
    assert new == src.replace(RELEASE, "- Release process: use the `release-process` skill.\n")
    manifest = json.loads(next((w / "home" / ".tokenstretch" / "manifests").glob("*.json")).read_text())
    assert manifest["profile"] == "trim"
    _undo(w)
    assert (w / "root" / "CLAUDE.md").read_bytes() == original
    assert not (w / "root" / ".claude").exists()


def test_user_file_goes_to_home_skills(tmp_path):
    w = _world(tmp_path, "# P\n\nshort\n", user_text=_doc(RELEASE))
    out = _run(w)
    assert "~/.claude/skills/release-process/SKILL.md" in out
    _run(w, "--apply", "--yes")
    assert (w / "home" / ".claude" / "skills" / "release-process" / "SKILL.md").is_file()
    _undo(w)
    assert not (w / "home" / ".claude" / "skills").exists()


def test_guard_word_section_is_kept_with_reason(tmp_path):
    w = _world(tmp_path, _doc(RULE, RELEASE))
    out = _run(w)
    assert "Kept in place:" in out and '"Safety rules"' in out and "standing rule" in out
    assert "safety-rules" not in out.split("Kept in place:")[0]
    _run(w, "--apply", "--yes")
    assert "Never skip check number 1" in (w / "root" / "CLAUDE.md").read_text()
    assert not (w / "root" / ".claude" / "skills" / "safety-rules").exists()


def test_existing_skill_dir_is_not_overwritten(tmp_path):
    w = _world(tmp_path, _doc(RELEASE))
    existing = w / "root" / ".claude" / "skills" / "release-process"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text("mine\n")
    out = _run(w)
    assert "already exists; not overwritten" in out and "Planned moves: none" in out
    _run(w, "--apply", "--yes")
    assert (existing / "SKILL.md").read_text() == "mine\n"
    assert "Step 1" in (w / "root" / "CLAUDE.md").read_text()


def test_section_under_min_tokens_is_not_moved(tmp_path):
    w = _world(tmp_path, _doc(SMALL))
    assert "Planned moves: none" in _run(w)
    assert "Planned moves: none" in _run(w, "--min-tokens", "150")


def test_fenced_hash_lines_are_not_sections(tmp_path):
    w = _world(tmp_path, _doc(FENCED, STYLE), user_text=None)
    out = _run(w, "--min-tokens", "5")
    assert '"Release process"' not in out and '"Deploy steps"' not in out and "Planned moves: none" in out
    # the fenced `# ...` lines never become a heading, so a fence-only section moves whole or not at all
    w2 = _world(tmp_path / "b", _doc("## How to deploy\n\n```bash\n# Release process\ngit push origin main\n# Deploy steps\nmake deploy\n"
                                + "".join(f"make step-{i} --verbose --target=production\n" for i in range(8)) + "```\n"))
    _run(w2, "--min-tokens", "5", "--apply", "--yes")
    skill = w2 / "root" / ".claude" / "skills" / "how-to-deploy" / "SKILL.md"
    assert "# Release process\ngit push origin main\n# Deploy steps\nmake deploy\n" in skill.read_text()
    assert not (w2 / "root" / ".claude" / "skills" / "release-process").exists()
    assert not (w2 / "root" / ".claude" / "skills" / "deploy-steps").exists()


def test_net_negative_proposes_nothing(tmp_path):
    w = _world(tmp_path, _doc("## Deploy\n\n1. a\n2. b\n3. c\n"))
    before = _tree(w)
    out = _run(w, "--min-tokens", "1", "--apply", "--yes")
    assert "Planned moves: none" in out and "adds more per turn than it removes" in out and "zero or negative" in out
    assert "Dry run" in out and _tree(w) == before


def test_rules_block_and_whole_file_and_level1_stay(tmp_path):
    block = f"## Release process\n\n{STEPS}\n\n{rules_block('sonnet', '/clear')}\n"
    w = _world(tmp_path, _doc(block))
    out = _run(w)
    assert "contains the tokenstretch rules block" in out and "Planned moves: none" in out
    w2 = _world(tmp_path / "b", RELEASE)  # the section is the whole file
    assert "it is the whole file" in _run(w2)
    w3 = _world(tmp_path / "c", "# Release process\n\n" + STEPS + "\n")  # level 1 never moves
    assert "Planned moves: none" in _run(w3)


def test_other_tools_files_and_json(tmp_path):
    w = _world(tmp_path, _doc(RELEASE))
    (w / "root" / "AGENTS.md").write_text(_doc(RELEASE.replace("Release process", "Other flow")))
    data = json.loads(_run(w, "--json"))
    assert data["mode"] == "dry-run" and [m["slug"] for m in data["moves"]] == ["release-process"]
    assert data["net"]["net_per_turn"] > 0 and "load only when" in data["limits"]


def test_cannot_combine_with_move_blocks(tmp_path):
    w = _world(tmp_path, _doc(RELEASE))
    err = io.StringIO()
    with redirect_stderr(err), redirect_stdout(io.StringIO()):
        assert main(["trim", "--to-skills", "--move-blocks", "--home", str(w / "home"), "--project", str(w / "root"),
                     "--sys-snapshot", str(w / "sys")]) == 2
    assert "cannot be combined" in err.getvalue()


def test_crlf_file_keeps_line_endings(tmp_path):
    w = _world(tmp_path, "x")
    (w / "root" / "CLAUDE.md").write_bytes(_doc(STYLE, RELEASE).replace("\n", "\r\n").encode())
    _run(w, "--apply", "--yes")
    data = (w / "root" / "CLAUDE.md").read_bytes()
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")
    assert b"use the `release-process` skill." in data


def test_file_shared_with_another_tool_is_planned_with_a_note(tmp_path):
    w = _world(tmp_path, "# Project\n\n" + STYLE + "\n" + RELEASE)
    (w / "home" / ".copilot").mkdir()
    out = _run(w, "--no-diff")
    assert "Planned moves (1)" in out
    assert "GitHub Copilot CLI also reads this file" in out and "only the pointer line" in out


def test_project_root_memory_file_is_not_counted_but_auto_memory_is(tmp_path):
    import re
    from tokenstretch.adapters.claude_code import ClaudeCodeAdapter
    from tokenstretch.context import build_context
    w = _world(tmp_path, "# Project\n")
    root = w / "root"
    (root / "MEMORY.md").write_text("- a note\n" * 50, encoding="utf-8")
    ctx = build_context(w / "home", root, w / "sys")
    names = lambda: [Path(f.path) for f in ClaudeCodeAdapter().list_instruction_files(ctx)]
    assert root / "MEMORY.md" not in names()
    auto = w / "home" / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(ctx.root)) / "memory" / "MEMORY.md"
    auto.parent.mkdir(parents=True)
    auto.write_text("- [Note](note.md) - hook\n", encoding="utf-8")
    assert auto in names()
