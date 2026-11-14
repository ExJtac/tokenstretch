"""Plugins and app-added skills in Claude Code's start-up load: how big each listing is, and whether
it was used lately.

Every listed skill puts its name and description into each session, used or not. `startup` already
sizes the skills in `~/.claude/skills/<name>/`. This module adds the three places it did not read:

  * plugins installed from the terminal (`~/.claude/plugins/installed_plugins.json`, switched in
    `enabledPlugins` in `~/.claude/settings.json`);
  * plugins the desktop app adds (`~/.claude/plugins/synced/<id>/<plugin>/`);
  * skills the desktop app adds (`~/.claude/skills/synced/<id>/<skill>/SKILL.md`).

Sizes are the listing text only (name + description, bytes / 4), an estimate. Use is counted from
the local session logs: a `Skill` call or a typed `/command` that names the skill, a helper started
from the plugin, or a call to one of the plugin's connector tools. A plugin with hooks runs on its
own, so it is never reported as unused. Read-only: nothing here changes a file.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .files import count_lines_bytes
from .util import frontmatter

USE_DAYS = 14                    # how far back "used lately" looks
USE_MAX_BYTES = 4 * 1024 * 1024 * 1024  # stop reading logs after this much; the report then says so
_TRUE = {"true", "yes", "on", "1"}
_SKILL_RE = re.compile(r'"name"\s*:\s*"Skill"\s*,\s*"input"\s*:\s*\{[^{}]*?"skill"\s*:\s*"([^"]{1,120})"')
_CMD_RE = re.compile(r"<command-name>/?([A-Za-z0-9:_.-]{1,120})</command-name>")
_AGENT_RE = re.compile(r'"subagent_type"\s*:\s*"([A-Za-z0-9_.-]{1,80}):[^"]{1,120}"')
_TOOL_RE = re.compile(r'"name"\s*:\s*"mcp__plugin_([A-Za-z0-9-]+?)_[A-Za-z0-9-]+__')


def _listing(md: Path, fallback: str) -> tuple[str, int] | None:
    """(name, listing bytes) for one SKILL.md or command file, or None when it is not listed."""
    counted = count_lines_bytes(md)
    if not counted:
        return None
    fm = frontmatter(counted[2])
    if fm.get("disable-model-invocation", "").lower() in _TRUE:
        return None  # manual-only: not listed, costs nothing per session
    name = fm.get("name") or fallback
    return name, len(name) + len(fm.get("description") or "")


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _plugin(root: Path) -> dict:
    """Skill names, listing bytes and connector count of one plugin folder."""
    names, size = [], 0
    d = root / "skills"
    for sk in sorted(d.iterdir()) if d.is_dir() else []:
        got = _listing(sk / "SKILL.md", sk.name) if sk.is_dir() and not sk.is_symlink() else None
        if got:
            names.append(got[0])
            size += got[1]
    connectors = 0
    try:
        servers = json.loads((root / ".mcp.json").read_text(encoding="utf-8")).get("mcpServers")
        connectors = len(servers) if isinstance(servers, dict) else 0
    except (OSError, ValueError, AttributeError):
        pass
    hooks = ((root / "hooks").is_dir() or (root / "hooks.json").is_file()
             or bool(_json(root / ".claude-plugin" / "plugin.json").get("hooks")))
    return {"skills": names, "bytes": size, "connectors": connectors, "hooks": hooks}


def found(home: Path) -> list[dict]:
    """Every plugin and app-added skill that is switched on, with its listing size. No log reads."""
    claude = home / ".claude"
    out: list[dict] = []
    enabled = _json(claude / "settings.json").get("enabledPlugins")
    enabled = enabled if isinstance(enabled, dict) else {}
    installed = _json(claude / "plugins" / "installed_plugins.json").get("plugins")
    for key, rows in sorted(installed.items()) if isinstance(installed, dict) else []:
        if enabled.get(key) is not True or not isinstance(rows, list) or not rows:
            continue
        path = rows[0].get("installPath") if isinstance(rows[0], dict) else None
        root = Path(path) if isinstance(path, str) else None
        if root is None or not root.is_dir():
            continue
        out.append({"kind": "plugin", "name": key.split("@")[0], "where": "terminal", "key": key, "root": str(root),
                    **_plugin(root)})
    synced = claude / "plugins" / "synced"
    for account in sorted(synced.iterdir()) if synced.is_dir() else []:
        for root in sorted(account.iterdir()) if account.is_dir() else []:
            if root.is_dir() and not root.is_symlink() and (root / ".claude-plugin").is_dir():
                out.append({"kind": "plugin", "name": root.name, "where": "app", "key": root.name, "root": str(root),
                            **_plugin(root)})
    synced = claude / "skills" / "synced"
    for account in sorted(synced.iterdir()) if synced.is_dir() else []:
        for sk in sorted(account.iterdir()) if account.is_dir() else []:
            got = _listing(sk / "SKILL.md", sk.name) if sk.is_dir() and not sk.is_symlink() else None
            if got:
                out.append({"kind": "skill", "name": got[0], "where": "app", "key": got[0],
                            "skills": [got[0]], "bytes": got[1], "connectors": 0, "hooks": False})
    return out


def used(home: Path, now: float | None = None, days: int = USE_DAYS, max_bytes: int = USE_MAX_BYTES) -> dict:
    """Skill names and plugin names called in the last `days` days of Claude Code logs.

    Returns {"skills": {name: calls}, "plugins": {name: calls}, "complete": bool}. `complete` is False
    when the byte limit stopped the read early; "not used" is then not claimed.
    """
    now = time.time() if now is None else now
    skills: dict[str, int] = {}
    plugins: dict[str, int] = {}
    files = []
    base = home / ".claude" / "projects"
    for dirpath, _dirs, names in os.walk(base) if base.is_dir() else []:
        for n in names:
            if n.endswith(".jsonl"):
                p = Path(dirpath) / n
                try:
                    st = p.stat()
                except OSError:
                    continue
                if now - st.st_mtime <= days * 86400 and not p.is_symlink():
                    files.append((st.st_mtime, st.st_size, p))
    read = 0
    complete = True
    for _mt, size, p in sorted(files, reverse=True):
        if read + size > max_bytes:
            complete = False
            break
        read += size
        try:
            with p.open(encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if "<command-name>" in line:
                        for m in _CMD_RE.finditer(line):
                            skills[m.group(1)] = skills.get(m.group(1), 0) + 1
                    if '"tool_use"' not in line:
                        continue
                    for m in _SKILL_RE.finditer(line):
                        skills[m.group(1)] = skills.get(m.group(1), 0) + 1
                    for rx in (_TOOL_RE, _AGENT_RE):
                        for m in rx.finditer(line):
                            plugins[m.group(1)] = plugins.get(m.group(1), 0) + 1
        except OSError:
            continue
    return {"skills": skills, "plugins": plugins, "complete": complete, "days": days}


def _calls(item: dict, use: dict) -> int:
    n = use["plugins"].get(item["name"], 0) if item["kind"] == "plugin" else 0
    for called, count in use["skills"].items():
        plugin, _, skill = called.rpartition(":")
        if item["kind"] == "plugin":
            if plugin == item["name"] and skill in item["skills"]:
                n += count
        elif skill == item["name"] and plugin in ("", "anthropic-skills"):
            n += count
    return n


def parts(home: Path, now: float | None = None, use: dict | None = None) -> list[dict]:
    """`startup` parts for plugins and app-added skills: size, use, and the fix for each."""
    items = found(home)
    if not items:
        return []
    use = used(home, now) if use is None else use
    out = []
    for it in items:
        calls = _calls(it, use)
        if it["hooks"] and not calls:
            note = "has hooks that run on their own, so use is not counted"
        elif calls:
            note = f"used {calls} time{'s' if calls != 1 else ''} in the last {use['days']} days"
        elif use["complete"]:
            note = f"no use found in the last {use['days']} days of Claude Code logs on this computer"
        else:
            note = "use not counted (the logs are too large to read in full)"
        extra = f", plus {it['connectors']} connector{'s' if it['connectors'] != 1 else ''} it brings (not sized)" if it["connectors"] else ""
        if it["kind"] == "plugin":
            count = len(it["skills"])
            what = f"plugin ({it['where']}): {count} skill{'s' if count != 1 else ''} listed{extra}; {note}"
            if it["where"] == "terminal":
                fix = (f'to switch it off, set "{it["key"]}": false under `enabledPlugins` in ~/.claude/settings.json; '
                       "set it back to true to undo.")
            else:
                fix = "the desktop app adds it: switch it off in the Claude desktop app's settings."
        else:
            what = f"skill added by the desktop app; {note}"
            fix = "switch it off in the Claude desktop app's settings if you don't use it."
        if not calls and (it["hooks"] or not use["complete"]):
            fix = "check whether you use it before switching it off. " + fix
        out.append({"kind": what, "name": it["name"], "tokens": max(1, it["bytes"] // 4) if it["bytes"] else None,
                    "fix": fix, "calls": calls, "unused": bool(use["complete"] and not calls and not it["hooks"])})
    return out
