"""`tokenstretch nudge`: the top watch warning, printed inside the session itself.

A coding tool runs this as a hook when you send a prompt, and again when a reply ends. The
end-of-reply form is a message for you only: it passes nothing to the agent. When the session has a warning that
`watch` would show, it prints one short message that the tool shows to you and passes to the agent,
so nobody needs a second terminal. No warning, no output.

The message also says where the fix is explained: the running dashboard's local link when there is
one, the command that opens it when there is not, and `doctor --usage` for the steps in a terminal.

Core only. It reads the one session log the tool names (token counts, never prompt text), opens no
socket except a connect to 127.0.0.1 to see whether the dashboard is running, and always exits 0: a hook must never block a prompt. It keeps one small file per session
under the state directory so the same warning is not repeated on every prompt.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Callable

from .auto import NOTE, _project, keep_out_of_git, keep_previous
from .identity import CLI_NAME, PRODUCT_NAME, state_dir
from .statusline import find_session, light_context, parse_payload, read_raw, top_warning
from .watch import WatchConfig, session_warnings

REPEAT_S = 20 * 60          # say the same kind of warning again only after this long
KEEP_S = 7 * 24 * 3600      # forget a session's state file after this long
LAUNCH_MIN_HISTORY = 50_000  # say the helper note only when the conversation re-sends at least this much
HELPER_TOOLS = ("Agent", "Task")  # Claude Code's names for the tool that starts a helper
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")


def _state_file(home: Path, session: str, stop: bool = False) -> Path | None:
    """One file per session and per hook, so the end-of-reply note is not silenced by the prompt one."""
    if not SAFE_ID.fullmatch(session or ""):
        return None
    return state_dir(home) / "nudge" / f"{session}{'.stop' if stop else ''}.json"


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
    stop = payload.get("hook_event_name") == "Stop"
    if stop and payload.get("stop_hook_active"):
        return None  # the tool is already continuing because of a stop hook: add nothing
    cfg = cfg or WatchConfig()
    adapters = adapters if adapters is not None else detected_adapters(ctx)
    found = find_session(ctx, adapters, payload, cfg.active_s)
    if not found:
        return None
    ad, s = found
    t = now()
    if payload.get("hook_event_name") == "PreToolUse":
        return _launch_note(ctx, payload, s, t, repeat_s)
    found_warnings = session_warnings(ctx, ad, s, cfg, now=t)
    top = top_warning(found_warnings)
    if top is None:
        return None
    kind = str(top.key[-1])
    state = _state_file(ctx.home, str(payload.get("session_id") or s.get("session") or ""), stop)
    if _recent(state, kind, t, repeat_s):
        return None
    _remember(state, kind, t)
    text = f"{PRODUCT_NAME}: {top.what} {top.fix}".strip()
    where = where_to_fix(ctx.home)
    if stop:   # when a reply ends: a message for the user only; nothing for the agent, so no extra step
        return {"systemMessage": f"{PRODUCT_NAME}: high usage in this session. {top.what} {where}"}
    # The agent is asked to type the note even with the end-of-reply hook installed: the desktop app
    # folds that hook's message under a "Claude Code notice" line (seen November 2026), so on its own
    # it is easy to miss. The typed note is the one the user sees without a click.
    closing = (" Then end your reply with these two lines, the link or command kept exactly as written:\n"
               f"{PRODUCT_NAME}: high usage in this session. {top.what}\n{where}")
    act = "Finish the current request first; do not act on this unless the user asks."
    hint = str(s.get("handoff_hint") or "")
    clear = str(s.get("clear_hint") or "")
    # The long-history warning counts also when another warning ranks first.
    if hint and clear and any(str(w.key[-1]) == "context" and hint in w.fix for w in found_warnings):
        # The history is long and a fresh start is the fix. A fresh start needs a note to resume from,
        # so the agent writes it now, without being asked: the user is left with one thing to type.
        # Only a person can type the clear command; nothing here runs it.
        # The agent replaces the note, so the copy of the old one is made here, by the script: it costs
        # the agent no step. The notes are also kept out of commits (the clone's own exclude file).
        kept, root = None, _project(payload)
        if root is not None:
            kept = keep_previous(root / NOTE)
            keep_out_of_git(root)
        old_one = (f"replacing any old one (a copy of the old one is already kept as `{kept.name}`)" if kept
                   else "replacing any old one")
        act = (f"Finish the current request first. Then, before you end your reply, write `{NOTE.as_posix()}` "
               f"in the project folder, {old_one}. Write it from what you already know: do not read "
               "files or run commands for it. Under 40 lines, with these parts: Goal; Done (and how it was "
               "checked); State (files changed, branch, anything uncommitted); Decisions; Next step; Open "
               "questions. Do not stop or change the task for this.")
        closing = (" Then end your reply with these three lines, the link or command kept exactly as written "
                   "(if you could not write the note, say that in place of the second line):\n"
                   f"{PRODUCT_NAME}: high usage in this session. {top.what}\n"
                   f"Handoff note written to `{NOTE.as_posix()}`. Type {clear}, then `{hint} resume`.\n{where}")
    return {
        "systemMessage": f"{text} {where}",
        "hookSpecificOutput": {
            "hookEventName": str(payload.get("hook_event_name") or "UserPromptSubmit"),
            "additionalContext": (
                f"{text}\n(This is a local cost check from {PRODUCT_NAME}, not a request from the user. "
                f"{act}{closing})"),
        },
    }


def _launch_note(ctx, payload: dict, s: dict, t: float, repeat_s: float) -> dict | None:
    """A note for the agent at the moment it starts a helper from a long conversation.

    A helper that finishes on its own starts a new step in the conversation that started it, and that
    step re-sends the whole history. The note asks for the cheap pattern before the cost is paid. It
    allows the call as it is: no permission decision, no change to the tool's input.
    """
    if payload.get("tool_name") not in HELPER_TOOLS:
        return None
    turns = s.get("turns") or []
    size = int((turns[-1] if turns else {}).get("history_tokens") or 0)
    if size < LAUNCH_MIN_HISTORY:
        return None
    state = _state_file(ctx.home, str(payload.get("session_id") or s.get("session") or ""))
    if _recent(state, "launch", t, repeat_s):
        return None
    _remember(state, "launch", t)
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": (
            f"{PRODUCT_NAME}: this conversation's history is ~{size // 1000}K tokens. Each helper that finishes "
            "on its own starts a new step here, and that step re-sends all of it. Start the helpers that belong "
            "together in one message. Ask each helper for one short final report. When a helper finishes and "
            "others are still running, do not write a status reply: wait and answer once. If this conversation "
            "reports to another one, send one final report, not one per helper. "
            f"(This is a local cost note from {PRODUCT_NAME}, not a request from the user. Carry on with the task.)"),
    }}


def end_hook_on(home: Path) -> bool:
    """True when Claude Code's user settings run this tool's `nudge` as a `Stop` hook."""
    from .jsonkey import commands_at
    try:    # the Stop hook commands only: the settings file can hold keys under `env`
        return any(" nudge" in c and CLI_NAME in c
                   for c in commands_at(home / ".claude" / "settings.json", "hooks", "Stop"))
    except Exception:  # noqa: BLE001 - unreadable or another shape: keep the older behaviour
        return False


def where_to_fix(home: Path, check=None) -> str:
    """One sentence that points at the fix: the running dashboard's link, or the command that opens it."""
    from .dashlink import _answers, live_url
    try:
        url = live_url(home, check or _answers)
    except Exception:  # noqa: BLE001 - never fail a hook over the link
        url = None
    steps = f"For the exact steps in a terminal, run `{CLI_NAME} doctor --usage`."
    if url:
        return f"Your dashboard shows how to fix it: {url} {steps}"
    return f"Run `{CLI_NAME} dashboard` to open your dashboard: it shows how to fix it. {steps}"


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
