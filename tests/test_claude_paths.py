"""Claude Code's paths are written in one place: the Claude Code adapter's `claude_paths`."""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

import tokenstretch
from tokenstretch import auto
from tokenstretch.adapters import claude_paths as paths

PKG = Path(tokenstretch.__file__).parent
MOVED = ["connectors", "nudge", "auto", "outputs", "startup_plugins", "trim_skills"]
# A path piece of Claude Code's layout, as a whole string. Help text that names a path for the reader
# ("... in ~/.claude/settings.json") is a sentence, not a path piece, and is not matched.
PIECES = {".claude", ".claude.json", ".mcp.json", ".claude-plugin", "settings.json", "settings.local.json",
          "installed_plugins.json", "handoff.md", "handoff-auto.md", "**/.claude/handoff*.md"}


@pytest.mark.parametrize("name", MOVED)
def test_no_claude_path_is_spelled_outside_the_adapter(name):
    tree = ast.parse((PKG / f"{name}.py").read_text(encoding="utf-8"))
    hits = [(n.lineno, n.value) for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and (n.value in PIECES or n.value.startswith((".claude/", ".claude\\")))]
    assert hits == [], f"{name}.py spells a Claude Code path itself; ask adapters/claude_paths.py instead: {hits}"
    assert "claude_paths" in (PKG / f"{name}.py").read_text(encoding="utf-8")


def test_the_layout_is_what_claude_code_uses(tmp_path):
    home, root = tmp_path / "home", tmp_path / "proj"
    assert paths.config_dir(home) == home / ".claude"
    assert paths.settings_file(home) == home / ".claude" / "settings.json"
    assert paths.settings_file(root) == root / ".claude" / "settings.json"
    assert paths.local_settings_file(root) == root / ".claude" / "settings.local.json"
    assert paths.user_file(home) == home / ".claude.json"
    assert paths.connectors_file(root) == root / ".mcp.json"
    assert paths.session_logs_dir(home) == home / ".claude" / "projects"
    assert paths.skills_dir(home) == home / ".claude" / "skills"
    assert paths.skills_dir(root) == root / ".claude" / "skills"
    assert paths.synced_skills_dir(home) == home / ".claude" / "skills" / "synced"
    assert paths.installed_plugins_file(home) == home / ".claude" / "plugins" / "installed_plugins.json"
    assert paths.synced_plugins_dir(home) == home / ".claude" / "plugins" / "synced"
    assert paths.plugin_marker_dir(root) == root / ".claude-plugin"
    assert paths.HANDOFF_NOTE.as_posix() == ".claude/handoff.md"
    assert paths.HANDOFF_PREV.as_posix() == ".claude/handoff.prev.md"
    assert paths.AUTO_HANDOFF_NOTE.as_posix() == ".claude/handoff-auto.md"
    assert paths.HANDOFF_GIT_PATTERN == "**/.claude/handoff*.md"


def test_the_adapter_and_the_hooks_use_the_same_note_paths():
    from tokenstretch.adapters.claude_code import ClaudeCodeAdapter, _settings_paths
    from tokenstretch.context import Context
    assert auto.NOTE is paths.HANDOFF_NOTE and auto.AUTO_NOTE is paths.AUTO_HANDOFF_NOTE
    assert auto.GIT_PATTERN is paths.HANDOFF_GIT_PATTERN
    assert ClaudeCodeAdapter.HANDOFF_NOTE == ".claude/handoff.md"
    assert ClaudeCodeAdapter.HANDOFF_PREV == ".claude/handoff.prev.md"
    ctx = Context(home=Path("h"), root=Path("r"))
    assert _settings_paths(ctx) == [paths.settings_file(ctx.home), paths.settings_file(ctx.root),
                                    paths.local_settings_file(ctx.root)]


def test_the_paths_file_imports_nothing_from_the_package():
    tree = ast.parse((PKG / "adapters" / "claude_paths.py").read_text(encoding="utf-8"))
    imported = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert sorted(getattr(n, "module", None) or n.names[0].name for n in imported) == ["__future__", "pathlib"]


@pytest.mark.parametrize("hook", ["auto", "nudge"])
def test_a_hook_command_does_not_load_the_adapters_to_find_a_path(hook):
    code = (f"import sys, tokenstretch.{hook}; "
            "print(sorted(m for m in sys.modules if m.startswith('tokenstretch.adapters')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=PKG.parent, check=True)
    assert out.stdout.strip() == "['tokenstretch.adapters', 'tokenstretch.adapters.claude_paths']"


def test_the_registry_is_built_on_first_use_and_is_the_same_list():
    import tokenstretch.adapters as adapters
    from tokenstretch.adapters import REGISTRY, _registry, implemented_check_ids
    assert REGISTRY is _registry.REGISTRY and adapters.REGISTRY is REGISTRY
    assert implemented_check_ids is _registry.implemented_check_ids
    assert [a.id for a in REGISTRY][:3] == ["host", "claude-code", "codex"] and len(REGISTRY) == 9
    with pytest.raises(AttributeError):
        adapters.no_such_name
