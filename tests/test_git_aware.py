"""Git work-tree copy and instruction-file discovery skip ignored paths."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tokenstretch.adapters.claude_code import ClaudeCodeAdapter
from tokenstretch.adapters.codex import CodexAdapter
from tokenstretch.adapters.host import HostAdapter
from tokenstretch.bench import _copy_repo, format_prepare, prepare_bench
from tokenstretch.context import build_context
from tokenstretch.files import clear_git_ls_cache

SAME_TEXT = "shared standing rules\n"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _make_project(tmp_path: Path, *, init_git: bool = True) -> tuple[Path, Path]:
    """A small project with an ignored work/ tree, vendor dirs, and .env files."""
    root = tmp_path / "proj"
    home = tmp_path / "home"
    sysd = tmp_path / "sys"
    root.mkdir()
    home.mkdir()
    sysd.mkdir()
    (sysd / "env.json").write_text("{}", encoding="utf-8")
    for name in ("ps.txt", "listeners.txt", "crontab.txt", "timers.txt"):
        (sysd / name).write_text("", encoding="utf-8")

    (root / "CLAUDE.md").write_text("# rules\nKeep this short.\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("# agents\nKeep this short.\n", encoding="utf-8")
    (root / "README.md").write_text("demo\n", encoding="utf-8")
    (root / ".gitignore").write_text("work/\n.env\n.env.*\n!.env.example\n", encoding="utf-8")
    (root / ".env.example").write_text("EXAMPLE=1\n", encoding="utf-8")
    (root / ".env").write_text("PLACEHOLDER=1\n", encoding="utf-8")
    (root / ".env.local").write_text("PLACEHOLDER_LOCAL=1\n", encoding="utf-8")
    src = root / "src"
    src.mkdir()
    (src / "app.py").write_text("print('ok')\n", encoding="utf-8")

    ignored = root / "work" / "snapshot"
    ignored.mkdir(parents=True)
    (ignored / "CLAUDE.md").write_text("x" * 200_000, encoding="utf-8")
    (ignored / "AGENTS.md").write_text("y" * 200_000, encoding="utf-8")

    (root / "node_modules").mkdir()
    (root / "node_modules" / "CLAUDE.md").write_text("vendor-claude\n", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "CLAUDE.md").write_text("venv-claude\n", encoding="utf-8")

    if init_git:
        _git(root, "init")
        _git(root, "config", "user.email", "test@example.com")
        _git(root, "config", "user.name", "Test")
        _git(root, "add", "CLAUDE.md", "AGENTS.md", "README.md", ".gitignore", ".env.example", "src/app.py")
    clear_git_ls_cache()
    return root, home


def _ctx(tmp_path: Path, root: Path, home: Path):
    return build_context(home, root, tmp_path / "sys")


def test_copy_skips_ignored_work_tree_and_env(tmp_path):
    root, _home = _make_project(tmp_path)
    dest = tmp_path / "copy"
    count = _copy_repo(root, dest)
    rels = {str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file() or p.is_symlink()}
    assert "CLAUDE.md" in rels
    assert "AGENTS.md" in rels
    assert "README.md" in rels
    assert ".env.example" in rels
    assert "src/app.py" in rels
    assert "work/snapshot/CLAUDE.md" not in rels
    assert "work/snapshot/AGENTS.md" not in rels
    assert ".env" not in rels
    assert ".env.local" not in rels
    assert "node_modules/CLAUDE.md" not in rels
    assert ".venv/CLAUDE.md" not in rels
    assert not (dest / "work").exists()
    assert count == len(rels)
    assert count > 0


def test_copy_fallback_without_git_still_drops_env(tmp_path):
    root, _home = _make_project(tmp_path, init_git=False)
    dest = tmp_path / "copy"
    _copy_repo(root, dest)
    assert (dest / ".env.example").is_file()
    assert not (dest / ".env").exists()
    assert not (dest / ".env.local").exists()
    # Without git, the ignored work/ tree is still copied (current fallback).
    assert (dest / "work" / "snapshot" / "CLAUDE.md").is_file()
    # Vendor dirs stay excluded by COPY_IGNORE.
    assert not (dest / "node_modules").exists()
    assert not (dest / ".venv").exists()


def test_prepare_records_copy_count_and_skips_ignored(tmp_path):
    root, home = _make_project(tmp_path)
    ctx = _ctx(tmp_path, root, home)
    out = tmp_path / "prepared"
    report = prepare_bench(ctx, {"prompt": "ok"}, adapters=[ClaudeCodeAdapter()], dest=out)
    assert any("Copied " in n and "files" in n for n in report.notices)
    for repo in (Path(report.before_repo), Path(report.after_repo)):
        assert (repo / "CLAUDE.md").is_file()
        assert not (repo / "work").exists()
        assert not (repo / ".env").exists()
        assert (repo / ".env.example").is_file()


def test_after_model_only_on_after_commands(tmp_path):
    root, home = _make_project(tmp_path)
    ctx = _ctx(tmp_path, root, home)
    out = tmp_path / "prepared"
    report = prepare_bench(
        ctx, {"prompt": "ok"},
        adapters=[ClaudeCodeAdapter(), CodexAdapter()],
        dest=out,
        after_model="claude-haiku-4-5",
    )
    for item in report.commands["before"]:
        assert "--model" not in item["argv"]
        assert "claude-haiku-4-5" not in item["cmd"]
    for item in report.commands["after"]:
        assert item["argv"][-2:] == ["--model", "claude-haiku-4-5"]
        assert "--model" in item["cmd"]
    man = json.loads(Path(report.manifest).read_text(encoding="utf-8"))
    assert man["after_model"] == "claude-haiku-4-5"
    text = format_prepare(report)
    # Before block should not gain a model flag; after should.
    assert "--bare" not in text


def test_after_model_cli_flag(tmp_path):
    from tokenstretch.cli import main

    root, home = _make_project(tmp_path)
    dest = tmp_path / "prepared"
    code = main([
        "bench", "--prepare",
        "--prepare-dir", str(dest),
        "--after-model", "sonnet",
        "--home", str(home),
        "--root", str(root),
        "--sys-snapshot", str(tmp_path / "sys"),
        "--prompt", "ok",
        "--adapter", "claude-code",
        "--json",
    ])
    assert code == 0
    man = json.loads((dest / "manifest.json").read_text(encoding="utf-8"))
    assert man["after_model"] == "sonnet"
    before = man["commands"]["before"]
    after = man["commands"]["after"]
    assert before and "--model" not in before[0]["argv"]
    assert after and after[0]["argv"][-2:] == ["--model", "sonnet"]


def test_doctor_skips_gitignored_instruction_files(tmp_path):
    root, home = _make_project(tmp_path)
    ctx = _ctx(tmp_path, root, home)

    claude = ClaudeCodeAdapter()
    files = claude.list_instruction_files(ctx)
    paths = [f.path for f in files]
    assert any(Path(p).name == "CLAUDE.md" and "work" not in Path(p).parts for p in paths)
    assert all("work" not in Path(p).parts for p in paths)
    assert all("node_modules" not in Path(p).parts for p in paths)
    assert all(".venv" not in Path(p).parts for p in paths)
    total = sum(f.bytes for f in files)
    assert total < 10_000
    w01 = claude._w01(ctx)
    assert w01 == []

    codex = CodexAdapter()
    xfiles = codex.list_instruction_files(ctx)
    assert all("work" not in Path(f.path).parts for f in xfiles)
    assert any(Path(f.path).name == "AGENTS.md" for f in xfiles)
    w02 = [f for f in codex._w02(ctx) if f.id == "W02"]
    assert w02 == []
    assert sum(f.bytes for f in xfiles) < 10_000


def test_w17_unique_paths_capped(tmp_path):
    root, home = _make_project(tmp_path)
    # Six unique tracked names with identical text, plus ignored copies that must not appear.
    names = ("CLAUDE.md", "AGENTS.md", "GEMINI.md", ".cursorrules", ".clinerules", ".windsurfrules")
    for name in names:
        (root / name).write_text(SAME_TEXT, encoding="utf-8")
    for i in range(8):
        d = root / "work" / f"copy-{i}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "CLAUDE.md").write_text(SAME_TEXT, encoding="utf-8")
    _git(root, "add", *names)
    clear_git_ls_cache()

    ctx = _ctx(tmp_path, root, home)
    hits = HostAdapter()._w17(ctx)
    assert hits
    ev = hits[0].evidence
    names_listed = ev.get("value") or []
    assert set(names_listed) == set(names)
    display = hits[0].message + " " + str(ev.get("file", ""))
    assert "and 1 more" in display
    assert display.count("CLAUDE.md") <= 2  # unique name, not repeated 8+ times
    assert "work" not in str(ev.get("file", ""))
    assert "copy-0" not in display
