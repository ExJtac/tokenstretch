"""Minimal YAML/JSON loader for task packs. Stdlib only; no network."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return parse_yaml(text)


def parse_yaml(text: str) -> Any:
    lines = []
    for raw in text.splitlines():
        if raw.strip().startswith("#") or not raw.strip():
            continue
        # strip inline comments that are preceded by a space
        if " #" in raw and not _in_quotes(raw, raw.index(" #")):
            raw = raw[: raw.index(" #")]
        lines.append(raw.rstrip())
    items, _ = _parse_block(lines, 0, 0)
    return items


def _in_quotes(s: str, idx: int) -> bool:
    q = None
    for i, ch in enumerate(s):
        if i >= idx:
            break
        if ch in "\"'" and (i == 0 or s[i - 1] != "\\"):
            q = None if q == ch else (ch if q is None else q)
    return q is not None


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip(" "))


def _scalar(s: str) -> Any:
    s = s.strip()
    if s in {"", "~", "null", "Null"}:
        return None
    if s in {"true", "True", "yes"}:
        return True
    if s in {"false", "False", "no"}:
        return False
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    try:
        if s.startswith("0") and s not in {"0"} and not s.startswith("0."):
            return s
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def _parse_block(lines: list[str], i: int, indent: int) -> tuple[Any, int]:
    if i >= len(lines):
        return {}, i
    # list?
    if lines[i].lstrip().startswith("- "):
        items: list[Any] = []
        while i < len(lines):
            ind = _indent(lines[i])
            if ind < indent:
                break
            if not lines[i].lstrip().startswith("- "):
                break
            rest = lines[i].lstrip()[2:]
            if rest and ":" in rest and not rest.startswith(("'", '"')):
                # inline mapping on the dash line, plus possible nested block
                key, _, val = rest.partition(":")
                node: dict[str, Any] = {}
                if val.strip():
                    node[key.strip()] = _scalar(val)
                    i += 1
                else:
                    child, i = _parse_block(lines, i + 1, ind + 2)
                    node[key.strip()] = child
                # more keys at nested indent
                while i < len(lines) and _indent(lines[i]) >= ind + 2 and not lines[i].lstrip().startswith("- "):
                    k, _, v = lines[i].lstrip().partition(":")
                    if v.strip():
                        node[k.strip()] = _scalar(v)
                        i += 1
                    else:
                        child, i = _parse_block(lines, i + 1, _indent(lines[i]) + 2)
                        node[k.strip()] = child
                items.append(node)
            elif rest:
                items.append(_scalar(rest))
                i += 1
            else:
                child, i = _parse_block(lines, i + 1, ind + 2)
                items.append(child)
        return items, i
    # mapping
    node = {}
    while i < len(lines):
        ind = _indent(lines[i])
        if ind < indent:
            break
        if lines[i].lstrip().startswith("- "):
            break
        key, _, val = lines[i].lstrip().partition(":")
        if val.strip():
            node[key.strip()] = _scalar(val)
            i += 1
        else:
            child, i = _parse_block(lines, i + 1, ind + 2)
            node[key.strip()] = child
    return node, i
