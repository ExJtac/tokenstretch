"""Connectors (MCP servers) in Claude Code: which ones you call, and a switch for the ones you don't.

A connector puts its whole tool list into every session, used or not. This module answers two
questions from local files only:

  * How often was each connector called? Counted from the session logs: every tool call named
    `mcp__<connector>__<tool>`, over the last 30 days.
  * Which ones can be switched off here? Only the ones in your own user file (`~/.claude.json`,
    top-level `mcpServers`). `off` moves the entry into `~/.tokenstretch/connectors-off.json`
    and `on` moves it back, unchanged. Connectors from a project file, a plugin or an app are
    reported, with where to switch them, and never edited.

A switch takes effect in the next session: a running session keeps the connectors it started with.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .identity import CLI_NAME

USE_DAYS = 30
USE_MAX_BYTES = 4 * 1024 * 1024 * 1024   # stop reading logs after this much; the report then says so
MIN_SESSIONS = 5                          # fewer sessions than this is too little to call anything unused
PARK = Path(".tokenstretch") / "connectors-off.json"
_CALL_RE = re.compile(r'"name"\s*:\s*"mcp__([A-Za-z0-9_.-]+?)__[A-Za-z0-9_.-]+"')
_TS_RE = re.compile(r'"timestamp"\s*:\s*"(\d{4}-\d{2}-\d{2})')
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


def log_name(name: str) -> str:
    """A connector's name as it appears in tool names (other characters become `_`)."""
    return _UNSAFE.sub("_", name)


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _servers(data) -> dict:
    got = data.get("mcpServers") if isinstance(data, dict) else None
    return {k: v for k, v in got.items() if isinstance(v, dict)} if isinstance(got, dict) else {}


def parked(home: Path) -> dict:
    """Connectors switched off by `off`: {name: entry}."""
    return _servers(_json(home / PARK))


def configured(home: Path, root: Path | None = None) -> list[dict]:
    """Every connector named in a local file: {name, scope, log, can_switch, where}."""
    out: list[dict] = []
    data = _json(home / ".claude.json")
    for name in _servers(data):
        out.append({"name": name, "scope": "user", "log": log_name(name), "can_switch": True, "state": "on",
                    "where": "your user file"})
    projects = data.get("projects")
    for proj, pdata in sorted(projects.items()) if isinstance(projects, dict) else []:
        for name in _servers(pdata):
            out.append({"name": name, "scope": "project (private)", "log": log_name(name), "can_switch": False,
                        "state": "on", "where": f"one project's private settings ({Path(proj).name})"})
    if root is not None:
        for name in _servers(_json(root / ".mcp.json")):
            out.append({"name": name, "scope": "project (shared)", "log": log_name(name), "can_switch": False,
                        "state": "on", "where": "this project's .mcp.json"})
    from .startup_plugins import found
    for plugin in found(home):
        if plugin["kind"] != "plugin":
            continue
        base = Path(plugin["root"])
        for name in _servers(_json(base / ".mcp.json")):
            out.append({"name": name, "scope": f"plugin {plugin['name']}", "can_switch": False, "state": "on",
                        "log": f"plugin_{log_name(plugin['name'])}_{log_name(name)}", "plugin": plugin["name"],
                        "where": f"the {plugin['name']} plugin ({plugin['where']})"})
    for name in parked(home):
        out.append({"name": name, "scope": "user", "log": log_name(name), "can_switch": True, "state": "off",
                    "where": f"switched off by {CLI_NAME}"})
    return out


def used(home: Path, now: float | None = None, days: int = USE_DAYS, max_bytes: int = USE_MAX_BYTES) -> dict:
    """Calls per connector in the last `days` days of Claude Code logs.

    Returns {"servers": {log name: {"calls", "sessions", "last"}}, "sessions": n, "complete": bool, "days"}.
    `last` is the newest day (YYYY-MM-DD) a call was logged. `complete` is False when the byte limit
    stopped the read early; nothing is then called unused.
    """
    now = time.time() if now is None else now
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
    servers: dict[str, dict] = {}
    read = 0
    complete = True
    for _mt, size, p in sorted(files, reverse=True):
        if read + size > max_bytes:
            complete = False
            break
        read += size
        seen: set[str] = set()
        try:
            with p.open(encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"mcp__' not in line or '"tool_use"' not in line:
                        continue
                    day = _TS_RE.search(line)
                    for m in _CALL_RE.finditer(line):
                        s = servers.setdefault(m.group(1), {"calls": 0, "sessions": 0, "last": ""})
                        s["calls"] += 1
                        if day and day.group(1) > s["last"]:
                            s["last"] = day.group(1)
                        seen.add(m.group(1))
        except OSError:
            continue
        for name in seen:
            servers[name]["sessions"] += 1
    return {"servers": servers, "sessions": len(files), "complete": complete, "days": days}


def build(home: Path, root: Path | None = None, now: float | None = None, use: dict | None = None) -> dict:
    """The report: every connector from a local file or the logs, with its calls and what to do."""
    use = used(home, now) if use is None else use
    enough = use["complete"] and use["sessions"] >= MIN_SESSIONS
    rows, known = [], set()
    for c in configured(home, root):
        u = use["servers"].get(c["log"], {})
        known.add(c["log"])
        rows.append({**c, "calls": u.get("calls", 0), "sessions": u.get("sessions", 0), "last": u.get("last", ""),
                     "unused": bool(enough and c["state"] == "on" and not u.get("calls"))})
    for log, u in use["servers"].items():
        if log not in known:
            rows.append({"name": log, "scope": "app", "log": log, "can_switch": False, "state": "on",
                         "where": "added by an app or a plugin, not in a local file", **u, "unused": False})
    rows.sort(key=lambda r: (r["state"] == "off", -r["calls"], r["name"]))
    return {"rows": rows, "sessions": use["sessions"], "days": use["days"], "complete": use["complete"],
            "enough": enough, "can_switch_off": [r["name"] for r in rows if r["unused"] and r["can_switch"]]}


def format_report(d: dict) -> list[str]:
    out = [f"Connectors: calls in the last {d['days']} days of Claude Code logs on this computer "
           f"({d['sessions']} session file{'s' if d['sessions'] != 1 else ''})."]
    if not d["rows"]:
        return out + ["  No connector is named in a local file and none was called in the logs."]
    if not d["complete"]:
        out.append("  The logs were too large to read in full, so no connector is called unused.")
    elif not d["enough"]:
        out.append(f"  Fewer than {MIN_SESSIONS} sessions in that time: too little to call a connector unused.")
    out.append("    calls  sessions  last call   connector")
    for r in d["rows"]:
        mark = "  (off)" if r["state"] == "off" else "  <- no call found" if r["unused"] else ""
        out.append(f"   {r['calls']:>6}  {r['sessions']:>8}  {r['last'] or '-':<10}  {r['name']}: {r['where']}{mark}")
    idle = [r for r in d["rows"] if r["unused"]]
    mine = d["can_switch_off"]
    if mine:
        out.append(f"  You can switch off here: {', '.join(mine)}. Run `{CLI_NAME} connectors off --unused` to see "
                   "the change, and add `--apply` to make it. `connectors on NAME --apply` brings one back.")
    plugins = sorted({r["plugin"] for r in idle if r.get("plugin")})
    if plugins:
        out.append(f"  No call found to any connector of: {', '.join(plugins)}. A plugin's connectors are switched "
                   f"with the plugin; `{CLI_NAME} startup` shows how for each.")
    other = [r["name"] for r in idle if not r["can_switch"] and not r.get("plugin")]
    if other:
        out.append(f"  No call found, set in a project file: {', '.join(other)}. This command does not edit "
                   "project files; switch those in the tool itself.")
    off = [r["name"] for r in d["rows"] if r["state"] == "off"]
    if off:
        out.append(f"  Switched off: {', '.join(off)}. `{CLI_NAME} connectors on NAME --apply` brings one back "
                   "for the next session.")
    out.append("  What this can't see: a connector an app adds shows up only after its first call, and other "
               "apps that share a connector are not counted. A switch takes effect in the next session.")
    return out


def switch(home: Path, names: list[str], on: bool, apply: bool = False) -> dict:
    """Move connectors between the user file and the off file. Dry-run unless `apply`.

    Returns {"moved": [...], "skipped": {name: reason}, "applied": bool}, plus "manifest" (its path)
    when applied. The entry is moved as it is; nothing in it is read, printed or changed.

    Both files are written the way setup writes: each is backed up first and the change is recorded
    in a manifest, so `undo` puts the switch back. An entry can hold a key, so the two files, their
    backups and the copies of what was written are all readable by the owner only. The manifest
    holds paths and checksums, never an entry.
    """
    user_file, park_file = home / ".claude.json", home / PARK
    data = _json(user_file)
    live, off = _servers(data), parked(home)
    src, dst = (off, live) if on else (live, off)
    moved, skipped = [], {}
    for name in names:
        if name not in src:
            skipped[name] = ("it is not switched off" if on else
                             "it is not in your user file (project, plugin and app connectors are not edited)")
        elif name in dst:
            skipped[name] = "a connector with that name is already there"
        else:
            moved.append(name)
    if not apply or not moved:
        return {"moved": moved, "skipped": skipped, "applied": False}
    if not on and not user_file.is_file():
        return {"moved": [], "skipped": {n: "the user file could not be read" for n in names}, "applied": False}
    from .context import Context
    from .installer import _now_stamp, apply_changes, kit_root

    for name in moved:
        dst[name] = src.pop(name)
    data["mcpServers"] = live
    word = "on" if on else "off"
    reason = f"connectors {word}: {len(moved)} moved"       # a count, not the names or the entries
    changes = [                                      # the off file first: an entry is never in neither file
        {"path": str(park_file), "content": json.dumps({"mcpServers": off}, indent=2) + "\n",
         "private": True, "reason": reason},
        {"path": str(user_file), "content": json.dumps(data, indent=2) + "\n", "private": True, "reason": reason},
    ]
    ctx = Context(home=home, root=home)
    base, n = f"{_now_stamp()}-connectors", 1
    while (kit_root(ctx) / "manifests" / f"{base}-{n:02d}.json").exists():   # two switches in the same second
        n += 1
    stamp = f"{base}-{n:02d}"                        # numbered so that the newest sorts last, as undo expects
    done = apply_changes(ctx, changes, stamp=stamp, profile="connectors",
                         extra_manifest={"title": f"Switch connectors {word}"})
    return {"moved": moved, "skipped": skipped, "applied": True, "manifest": done["manifest_path"]}


def off_note(home: Path) -> str:
    """One line for a new session: which connectors are off and how to get one back. "" when none."""
    names = sorted(parked(home))
    if not names:
        return ""
    return (f"{CLI_NAME}: these connectors are switched off to keep sessions light: {', '.join(names)}. If the "
            f"task needs one, tell the user to run `{CLI_NAME} connectors on NAME --apply` and start a new "
            "session. Do not edit settings files for this yourself.")
