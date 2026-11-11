"""Small shared helpers (stdlib only)."""
from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build", "__pycache__", "target", ".next"}


def read_json(path: Path):
    """Return parsed JSON, None if missing, or {'__error__': msg} if unparsable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        return {"__error__": str(e)}


def read_toml(path: Path):
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as e:
        return {"__error__": str(e)}


def walk_named(root: Path, names: set[str], max_depth: int = 6):
    """Yield files under root whose name is in `names`, skipping vendor dirs."""
    if not root.is_dir():
        return
    root_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).parts) - root_depth
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS] if depth < max_depth else []
        for f in filenames:
            if f in names:
                yield Path(dirpath) / f


FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)


def frontmatter(text: str) -> dict[str, str]:
    m = FRONTMATTER_RE.match(text)
    out: dict[str, str] = {}
    if not m:
        return out
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def is_loopback_url(url: str) -> bool:
    """True for loopback http(s) origins, including a path suffix such as /v1."""
    return bool(re.match(r"^\w+://(localhost|127\.\d+\.\d+\.\d+|\[?::1\]?|0\.0\.0\.0)(:\d+)?", url or ""))


def truthy(v) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"}
