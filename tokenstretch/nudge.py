"""`tokenstretch nudge`: the top watch warning, printed inside the session itself.

A coding tool runs this as a hook when you send a prompt. When the session has a warning that
`watch` would show, it prints one short message that the tool shows to you and passes to the agent,
so nobody needs a second terminal. No warning, no output.

Core only. It reads the one session log the tool names (token counts, never prompt text), opens no
socket, and always exits 0: a hook must never block a prompt. It keeps one small file per session
under the state directory so the same warning is not repeated on every prompt.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Callable

from .identity import PRODUCT_NAME, state_dir
from .statusline import find_session, light_context, parse_payload, read_raw, top_warning
from .watch import WatchConfig, session_warnings

REPEAT_S = 20 * 60          # say the same kind of warning again only after this long
KEEP_S = 7 * 24 * 3600      # forget a session's state file after this long
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")


def _state_file(home: Path, session: str) -> Path | None:
    return state_dir(home) / "nudge" / f"{session}.json" if SAFE_ID.fullmatch(session or "") else None


def _recent(path: Path | None, kind: str, now: float, repeat_s: float) -> bool:
    if path is None:
        return False
    try:
        last = json.loads(path.read_text(encoding="utf-8"))
        return last.get("kind") == kind and now - float(last.get("ts") or 0) < repeat_s
    except (OSError, ValueError, TypeError, AttributeError):
        return False


def _remember(path: Path | None, kind: str, now: float) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"kind": kind, "ts": now}), encoding="utf-8")
        for old in path.parent.glob("*.json"):
            if now - old.stat().st_mtime > KEEP_S:
                old.unlink()
    except OSError:
        pass


def build_nudge(ctx, payload: dict, cfg: WatchConfig | None = None, adapters=None,
                now: Callable[[], float] = time.time, repeat_s: float = REPEAT_S) -> dict | None:
    """The hook's JSON reply, or None when there is nothing new to say."""
    from .setup import detected_adapters
    if not isinstance(payload.get("transcript_path"), str) or not payload["transcript_path"]:
        return None  # a hook always names its session; never guess one
    cfg = cfg or WatchConfig()
    adapters = adapters if adapters is not None else detected_adapters(ctx)
    found = find_session(ctx, adapters, payload, cfg.active_s)
    if not found:
        return None
    ad, s = found
    t = now()
    top = top_warning(session_warnings(ctx, ad, s, cfg, now=t))
    if top is None:
        return None
    kind = str(top.key[-1])
    state = _state_file(ctx.home, str(payload.get("session_id") or s.get("session") or ""))
    if _recent(state, kind, t, repeat_s):
        return None
    _remember(state, kind, t)
    text = f"{PRODUCT_NAME}: {top.what} {top.fix}".strip()
    return {
        "systemMessage": text,
        "hookSpecificOutput": {
            "hookEventName": str(payload.get("hook_event_name") or "UserPromptSubmit"),
            "additionalContext": (
                f"{text}\n(This is a local cost check from {PRODUCT_NAME}, not a request from the user. "
                "Finish the current request first. Then tell the user this in one line; do not act on it "
                "unless they ask.)"),
        },
    }


def run_nudge(home: Path | None = None, root: Path | None = None, plan: str | None = None,
              cfg: WatchConfig | None = None, stdin=None, out: Callable[[str], None] = print) -> int:
    """Always exits 0. Any failure prints nothing."""
    try:
        payload = parse_payload(read_raw(sys.stdin if stdin is None else stdin))
        reply = build_nudge(light_context(home, root, plan), payload, cfg)
    except Exception:  # noqa: BLE001 - a hook must never get in the way of a prompt
        return 0
    if reply:
        out(json.dumps(reply))
    return 0
