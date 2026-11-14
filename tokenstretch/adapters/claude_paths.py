"""Where Claude Code keeps things. Part of the Claude Code adapter (`claude_code.py` imports all of it).

This is the one place these paths are written. Code outside the adapters asks here and never spells
a Claude Code path itself, so a path that moves is changed once.

It is its own small file, with no imports from the rest of the package, because the hook commands
(`auto`, `nudge`) run on every prompt: they need a path, not the whole adapter.
"""
from __future__ import annotations

from pathlib import Path

CONFIG_DIRNAME = ".claude"                               # in the home folder and in a project
PLUGIN_MARKER = ".claude-plugin"                         # the folder that makes a folder a plugin

HANDOFF_NOTE = Path(CONFIG_DIRNAME) / "handoff.md"            # in a project; written by the agent (/handoff)
HANDOFF_PREV = Path(CONFIG_DIRNAME) / "handoff.prev.md"       # the note before the last one
AUTO_HANDOFF_NOTE = Path(CONFIG_DIRNAME) / "handoff-auto.md"  # in a project; written after a compaction
HANDOFF_GIT_PATTERN = f"**/{CONFIG_DIRNAME}/handoff*.md"      # both notes and their `.prev` copies, any depth


def config_dir(base: Path) -> Path:
    """Claude Code's folder under the home folder or a project."""
    return base / CONFIG_DIRNAME


def settings_file(base: Path) -> Path:
    """The settings file of the home folder (user settings) or of a project. It can hold keys under `env`."""
    return config_dir(base) / "settings.json"


def local_settings_file(root: Path) -> Path:
    """A project's settings that are not shared."""
    return config_dir(root) / "settings.local.json"


def user_file(home: Path) -> Path:
    """`~/.claude.json`: the user's own connectors and per-project state. An entry in it can hold a key."""
    return home / ".claude.json"


def connectors_file(folder: Path) -> Path:
    """The connector list a project or a plugin folder carries."""
    return folder / ".mcp.json"


def session_logs_dir(home: Path) -> Path:
    """The session logs, one folder per project."""
    return config_dir(home) / "projects"


def skills_dir(base: Path) -> Path:
    """Skills of the home folder (user skills) or of a project."""
    return config_dir(base) / "skills"


def synced_skills_dir(home: Path) -> Path:
    """Skills the desktop app adds: `<account>/<skill>/SKILL.md` below this."""
    return skills_dir(home) / "synced"


def installed_plugins_file(home: Path) -> Path:
    """The list of plugins installed from the terminal."""
    return config_dir(home) / "plugins" / "installed_plugins.json"


def synced_plugins_dir(home: Path) -> Path:
    """Plugins the desktop app adds: `<account>/<plugin>/` below this."""
    return config_dir(home) / "plugins" / "synced"


def plugin_marker_dir(plugin_root: Path) -> Path:
    """The folder inside a plugin that holds its `plugin.json`."""
    return plugin_root / PLUGIN_MARKER
