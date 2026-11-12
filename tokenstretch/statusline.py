"""`tokenstretch statusline`: one line for a coding tool's status bar.

Core only: it shows the current session's history size and, when watch would warn, the most
important warning in short form. Sessions and per-turn records come from adapters
(`watch_session_at`, `watch_sessions`); the checks are watch's own `session_warnings`.

A status bar runs this on every update, so it builds a light context (no `ps`, no `crontab`) and
reads one session log. It is read-only, opens no socket, and prints nothing rather than an error:
a broken status line must never get in the way of the tool it sits in.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Callable

from .context import Context
from .watch import WatchConfig, WatchWarning, _k, session_warnings

# Most urgent first: what is costing credits on every step, then what already happened.
PRIORITY = ("context", "cache", "top-model", "growth", "rereads")
MAX_STDIN = 1_000_000


def light_context(home: Path | None = None, root: Path | None = None, plan: str | None = None) -> Context:
    """A Context with paths and environment only. Nothing is run and no file is read."""
    env = dict(os.environ)
    return Context(
        home=Path(home or Path.home()).expanduser().resolve(),
        root=Path(root or Path.cwd()).expanduser().resolve(),
        env=env,
        path_dirs=[Path(p) for p in env.get("PATH", "").split(os.pathsep) if p],
        plan_override=plan,
        platform=sys.platform,
    )


def read_payload(stream) -> dict:
    """The JSON a tool pipes to its status-line command, or {} (a terminal, no input, not JSON)."""
    try:
        if stream is None or stream.isatty():
            return {}
        data = json.loads(stream.read(MAX_STDIN) or "{}")
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def find_session(ctx: Context, adapters, payload: dict, active_s: float) -> tuple[object, dict] | None:
    """(adapter, session): the one the tool named in `transcript_path`, else the newest active one."""
    path = payload.get("transcript_path")
    if isinstance(path, str) and path:
        for ad in adapters:
            s = ad.watch_session_at(ctx, Path(path))
            if s:
                return ad, s
        return None
    best = None
    for ad in adapters:
        for s in ad.watch_sessions(ctx, limit=1, max_age_s=active_s) or []:
            if best is None or (s.get("last_write") or 0) > (best[1].get("last_write") or 0):
                best = (ad, s)
    return best


def top_warning(warnings: list[WatchWarning]) -> WatchWarning | None:
    def rank(w: WatchWarning) -> int:
        kind = w.key[-1]
        return PRIORITY.index(kind) if kind in PRIORITY else len(PRIORITY)
    return min(warnings, key=rank, default=None)


def build_line(ctx: Context, payload: dict | None = None, cfg: WatchConfig | None = None, adapters=None,
               now: Callable[[], float] = time.time) -> str:
    """The status line, or "" when there is no session to describe."""
    from .setup import detected_adapters
    cfg = cfg or WatchConfig()
    adapters = adapters if adapters is not None else detected_adapters(ctx)
    found = find_session(ctx, adapters, payload or {}, cfg.active_s)
    if not found:
        return ""
    ad, s = found
    turns = s.get("turns") or []
    if not turns:
        return ""
    last = turns[-1]
    size = f"{_k(last['history_tokens'])}/{_k(cfg.max_context)} history"
    top = top_warning(session_warnings(ctx, ad, s, cfg, now=now()))
    if top is None:
        return f"{size} · {last['model']}" if last.get("model") else size
    return f"⚠ {size} · {top.short or top.what}"


def run_statusline(home: Path | None = None, root: Path | None = None, plan: str | None = None,
                   cfg: WatchConfig | None = None, stdin=None, out: Callable[[str], None] = print) -> int:
    """Always exits 0. Any failure prints nothing."""
    try:
        payload = read_payload(sys.stdin if stdin is None else stdin)
        line = build_line(light_context(home, root, plan), payload, cfg)
    except Exception:  # noqa: BLE001 - a status line must never break the tool that runs it
        return 0
    if line:
        out(line)
    return 0
