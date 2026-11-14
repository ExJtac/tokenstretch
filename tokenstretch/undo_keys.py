"""Key-level undo for a JSON settings file that changed after setup.

`undo` normally puts the whole backup back. When the user (or another tool) edited the file since
setup, that would wipe their edits, so this module takes out only what this tool put in:

- an entry this tool added is removed: a hook or status line is matched by its own command string,
  and any other key by the copy of the file that setup wrote (kept next to the backup by versions after 0.9.3);
- a key this tool changed goes back to the value in the backup, when it still has the value setup
  gave it;
- everything else stays as it is now, including a key the user changed after setup.

Pure functions over parsed JSON. No file is read or written here, and no value is printed.
"""
from __future__ import annotations

import re
from typing import Any

from .identity import PACKAGE_NAME

MISSING: Any = type("Missing", (), {"__repr__": lambda self: "MISSING"})()
# `tokenstretch nudge`, `/path/to/tokenstretch auto`, `python -m tokenstretch statusline --wrap ...`
OWN_COMMAND = re.compile(rf"(?:^|[\s/'\"]){re.escape(PACKAGE_NAME)}['\"]?\s+[a-z]")


def _commands(node: Any, out: set[str]) -> set[str]:
    if isinstance(node, dict):
        if isinstance(node.get("command"), str):
            out.add(node["command"])
        for v in node.values():
            _commands(v, out)
    elif isinstance(node, list):
        for v in node:
            _commands(v, out)
    return out


def _lost_a_list(old: Any, new: Any) -> bool:
    """A hook group whose list of hooks emptied: nothing of the user's is left in it."""
    return (isinstance(old, dict) and isinstance(new, dict)
            and any(isinstance(v, list) and v and not new.get(k) for k, v in old.items()))


def _revert(cur: Any, before: Any, after: Any, prior: set[str]) -> Any:
    """`cur` with this tool's part taken out. `after` is MISSING when setup's copy was not kept."""
    if isinstance(cur, dict) and isinstance(cur.get("command"), str) \
            and OWN_COMMAND.search(cur["command"]) and cur["command"] not in prior:
        return before                      # our hook or status line: gone, or the user's old one back
    if after is not MISSING:
        if after == before:
            return cur                     # setup did not change this part
        if cur == after:
            return before                  # still as setup left it
    if isinstance(cur, dict):
        b = before if isinstance(before, dict) else {}
        a = after if isinstance(after, dict) else None
        out = {}
        for k, v in cur.items():
            r = _revert(v, b.get(k, MISSING), a.get(k, MISSING) if a is not None else MISSING, prior)
            if r is not MISSING:
                out[k] = r
        if a is not None:                  # a key setup removed, and nobody put back since
            out.update({k: v for k, v in b.items() if k not in a and k not in cur})
        return MISSING if cur and not out and before is MISSING else out
    if isinstance(cur, list):
        b = before if isinstance(before, list) else []
        a = after if isinstance(after, list) else None
        added = [e for e in a if e not in b] if a is not None else []
        out = []
        for e in cur:
            if e in b:
                out.append(e)
                continue
            if e in added:
                continue
            r = _revert(e, MISSING, MISSING, prior)
            if r is MISSING or _lost_a_list(e, r):
                continue
            out.append(r)
        if a is not None:
            out.extend(e for e in b if e not in a and e not in out)
        return MISSING if cur and not out and before is MISSING else out
    return cur                             # a value the user changed after setup, or one we cannot tell about


def revert_own_entries(current: dict, before: dict, after: dict | None) -> dict:
    """The settings as they are now, minus what this tool added on top of `before`.

    `after` is the file as setup wrote it, or None for a manifest from an older version. Without
    it, only hooks and a status line can be told apart (by command string); other keys stay.
    """
    prior = _commands(before, set())       # our own entries from an earlier setup run are not this run's to remove
    out = _revert(current, before, MISSING if after is None else after, prior)
    return out if isinstance(out, dict) else {}
