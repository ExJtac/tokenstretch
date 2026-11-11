"""Local file helpers used by adapters. Never opens a network connection."""
from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from .util import SKIP_DIRS, frontmatter, read_json, read_toml

# Re-export so adapters have one import site.
__all__ = ["SKIP_DIRS", "frontmatter", "read_json", "read_toml", "count_lines_bytes",
           "walk_named", "walk_glob_names", "text_if_small", "import_refs", "env_truthy",
           "is_git_work_tree", "git_ls_files", "clear_git_ls_cache", "set_toml_root_string"]


def count_lines_bytes(path: Path, cap: int | None = 4 * 1024 * 1024) -> tuple[int, int, str] | None:
    """Return (lines, bytes, text) or None if missing/too large. `cap` is the skip threshold."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if cap is not None and size > cap:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return text.count("\n") + (0 if text.endswith("\n") or text == "" else 1), size, text


# Cache of git ls-files results: resolved root -> list of relative paths, or None if not a git work tree.
_GIT_LS_CACHE: dict[str, list[str] | None] = {}

_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "1"}


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env.update(_GIT_ENV)
    return env


def is_git_work_tree(root: Path) -> bool:
    """True when `git -C root rev-parse --is-inside-work-tree` prints true. Local only."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, check=False, timeout=15, env=_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def git_ls_files(root: Path) -> list[str] | None:
    """Relative paths from one `git ls-files -z --cached --others --exclude-standard`.

    Returns None when root is not a git work tree or git is missing. Cached per root.
    """
    try:
        key = str(root.resolve())
    except OSError:
        key = str(root)
    if key in _GIT_LS_CACHE:
        cached = _GIT_LS_CACHE[key]
        return None if cached is None else list(cached)
    if not is_git_work_tree(root):
        _GIT_LS_CACHE[key] = None
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "."],
            capture_output=True, check=False, timeout=60, env=_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        _GIT_LS_CACHE[key] = None
        return None
    if result.returncode != 0:
        _GIT_LS_CACHE[key] = None
        return None
    rels: list[str] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        rel = os.fsdecode(raw)
        if rel in {".", "./"} or rel.startswith("../") or os.path.isabs(rel):
            continue
        if rel.startswith("./"):
            rel = rel[2:]
        rels.append(rel.replace("\\", "/"))
    _GIT_LS_CACHE[key] = rels
    return list(rels)


def clear_git_ls_cache() -> None:
    _GIT_LS_CACHE.clear()


def _skip_vendor_rel(rel: str) -> bool:
    return any(part in SKIP_DIRS for part in Path(rel).parts)


def _walk_from_git(root: Path, names: set[str], max_depth: int):
    listing = git_ls_files(root)
    if listing is None:
        return None
    out = []
    for rel in listing:
        parts = Path(rel).parts
        if not parts or parts[-1] not in names:
            continue
        if len(parts) - 1 > max_depth:
            continue
        if _skip_vendor_rel(rel):
            continue
        path = root / rel
        if path.is_file() or path.is_symlink():
            out.append(path)
    return out


def walk_named(root: Path, names: set[str], max_depth: int = 6):
    if not root.is_dir():
        return
    indexed = _walk_from_git(root, names, max_depth)
    if indexed is not None:
        yield from indexed
        return
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        depth = len(Path(dirpath).parts) - root_depth
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS] if depth < max_depth else []
        for f in filenames:
            if f in names:
                yield Path(dirpath) / f


def walk_glob_names(root: Path, filename: str, max_depth: int = 8):
    if not root.is_dir():
        return
    indexed = _walk_from_git(root, {filename}, max_depth)
    if indexed is not None:
        yield from indexed
        return
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        depth = len(Path(dirpath).parts) - root_depth
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS] if depth < max_depth else []
        if filename in filenames:
            yield Path(dirpath) / filename


_AT_IMPORT = re.compile(r"(?m)^@(?!@)([^\s]+)")


def import_refs(text: str, base: Path) -> list[Path]:
    """Resolve `@path` mentions that point at files (memory-file includes)."""
    out = []
    for m in _AT_IMPORT.finditer(text or ""):
        raw = m.group(1).strip().strip("'\"")
        p = Path(raw)
        cand = p if p.is_absolute() else (base.parent / raw)
        if cand.is_file():
            out.append(cand.resolve())
    return out


def env_truthy(value: str | None) -> bool:
    return bool(value) and value.strip().lower() not in {"0", "false", "no", "off", ""}


def text_if_small(path: Path, cap: int = 4 * 1024 * 1024) -> str | None:
    c = count_lines_bytes(path, cap)
    return None if c is None else c[2]


def set_toml_root_string(text: str, key: str, value: str) -> str:
    """Set a root-level TOML string key. Only lines before the first [table] are searched, and a new
    key goes before that table, so `model` inside [profiles.x] or [agents.y] is never touched."""
    lines = text.splitlines(keepends=True)
    first_table = next((i for i, ln in enumerate(lines) if re.match(r"[ \t]*\[", ln)), len(lines))
    pat = re.compile(rf"^[ \t]*{re.escape(key)}[ \t]*=")
    new_line = f"{key} = {json.dumps(value)}\n"
    for i in range(first_table):
        if pat.match(lines[i]):
            lines[i] = new_line
            return "".join(lines)
    if first_table and not lines[first_table - 1].endswith("\n"):
        lines[first_table - 1] += "\n"
    lines.insert(first_table, new_line + ("\n" if first_table == 0 and lines else ""))
    return "".join(lines)
