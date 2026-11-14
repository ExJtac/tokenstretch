"""Read one key from a JSON file that can hold other things, such as an API key.

`~/.claude/settings.json` can hold keys under `env`, and a `.mcp.json` can hold them inside a
connector's entry. A command that only needs to know which plugins are on, or how many connectors
a plugin has, should not carry the rest of the file around. These helpers parse the file, take
the one thing asked for, and return only that: the parsed file goes out of scope on return, and
nothing from it is stored, logged or printed here.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_key(path: Path, *keys: str) -> Any:
    """The value at `keys` (a path of object keys) in the JSON file, or None. Nothing else is returned."""
    try:
        node: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for key in keys:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def count_key(path: Path, *keys: str) -> int:
    """How many entries the object at `keys` has: a number only, no name and no entry. 0 when absent."""
    node = read_key(path, *keys)
    return len(node) if isinstance(node, dict) else 0


def commands_at(path: Path, *keys: str) -> list[str]:
    """The `command` strings of the hook groups at `keys`, and nothing else from the file."""
    groups = read_key(path, *keys)
    out: list[str] = []
    for group in groups if isinstance(groups, list) else []:
        hooks = group.get("hooks") if isinstance(group, dict) else None
        for hook in hooks if isinstance(hooks, list) else []:
            if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                out.append(hook["command"])
    return out
