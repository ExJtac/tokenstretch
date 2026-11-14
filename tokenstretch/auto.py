"""`tokenstretch auto`: keep sessions short without the user typing anything.

One hook entry for three events. The tool calls it with JSON on stdin; `hook_event_name` picks
the action:

- PreCompact: print what the summary must keep (goal, done, state, decisions, next step, open
  questions) and what to drop, so an automatic compaction leaves a note you could resume from.
- PostCompact: save that summary to `.claude/handoff-auto.md` in the project, so a new chat can
  pick the work up too. The text comes from the tool on stdin; no log is read. The note it replaces
  is kept as `.claude/handoff-auto.prev.md`.
- SessionStart (a new chat): if a recent handoff note exists in the project, tell the agent where
  it is, and to read it and carry on with its next step, without asking for a go-ahead, unless the
  first message asks for something else. A hook cannot start the first turn of an interactive
  session, so the user still sends one message; any message will do.
- SessionStart (after a compaction): tell the agent to carry on with the summary's next step in
  the same turn, without stopping to report the summary or ask for a go-ahead.

The notes are working files, so they are kept out of commits: one pattern is added to the clone's
own `.git/info/exclude` (never to a tracked `.gitignore`). See `keep_out_of_git`.

Core only. No network, no model calls. Always exits 0 and prints nothing on any failure: a hook
must never block a session, a compaction, or a prompt.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Callable

from .adapters.claude_paths import AUTO_HANDOFF_NOTE as AUTO_NOTE   # written here after a compaction
from .adapters.claude_paths import HANDOFF_GIT_PATTERN as GIT_PATTERN
from .adapters.claude_paths import HANDOFF_NOTE as NOTE              # written by the agent (/handoff)
from .identity import PRODUCT_NAME
from .statusline import parse_payload, read_raw

GIT_COMMENT = (f"# {PRODUCT_NAME}: handoff notes are working files, kept out of commits in this clone only. "
               "To commit them, put # in front of the next line.")
FRESH_S = 72 * 3600                              # older notes are not offered
MAX_NOTE_BYTES = 60_000

KEEP = (
    "Write the summary as a handoff note someone could resume from, under 40 lines:\n"
    "- Goal: one sentence.\n"
    "- Done: what is finished, and how it was checked.\n"
    "- State: files changed (paths, line numbers where they matter), the branch, anything uncommitted or half-done.\n"
    "- Decisions: choices made and why, and what was tried and ruled out.\n"
    "- Next step: the exact next action.\n"
    "- Open questions: anything waiting on the user.\n"
    "Leave out file contents, command output, search results and step-by-step history: they can be read again if needed."
)

CARRY_ON = (
    f"{PRODUCT_NAME}: the conversation was just summarized to keep it short. If work was under way, carry on "
    "with the summary's next step now, in this turn. Do not stop to tell the user about the summary, and do "
    "not ask them to confirm or to say go. Anything the summary lists as waiting on the user still waits, and "
    "so does anything that needs confirmation under your other rules. If nothing was under way, say nothing "
    "about this."
)


def _project(payload: dict) -> Path | None:
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    p = Path(cwd)
    return p if p.is_absolute() and p.is_dir() else None


def _age(seconds: float) -> str:
    if seconds < 5400:
        return f"{max(1, round(seconds / 60))} min"
    return f"{round(seconds / 3600)} h"


def prev_path(note: Path) -> Path:
    """`handoff.md` -> `handoff.prev.md`: where the note that is about to be replaced is kept."""
    return note.with_name(f"{note.stem}.prev{note.suffix}")


def keep_previous(note: Path) -> Path | None:
    """Copy `note` to its `.prev` name before it is replaced. One copy is kept, the newest.

    Returns the copy's path, or None when there was nothing to keep. Never follows a link, and an
    empty note never replaces a kept copy.
    """
    prev = prev_path(note)
    try:
        if note.is_symlink() or prev.is_symlink() or not note.is_file():
            return None
        data = note.read_bytes()[:MAX_NOTE_BYTES + 400]
        if not data.strip():
            return None
        prev.write_bytes(data)
        return prev
    except OSError:
        return None


def _git_exclude(root: Path) -> Path | None:
    """The `info/exclude` file of the git clone `root` is in, or None. Reads no file but `.git` pointers."""
    for folder in (root, *root.parents):
        dot = folder / ".git"
        if dot.is_symlink():
            return None
        if dot.is_dir():
            return dot / "info" / "exclude"
        if dot.is_file():  # a worktree or submodule: `.git` is a one-line pointer to the real folder
            line = dot.read_text(encoding="utf-8", errors="replace")[:4096].strip()
            if not line.startswith("gitdir:"):
                return None
            gitdir = (folder / line[len("gitdir:"):].strip()).resolve()
            common = gitdir / "commondir"  # a worktree shares the main clone's `info/exclude`
            if common.is_file():
                gitdir = (gitdir / common.read_text(encoding="utf-8", errors="replace")[:4096].strip()).resolve()
            return gitdir / "info" / "exclude" if gitdir.is_dir() else None
    return None


def keep_out_of_git(root: Path) -> bool:
    """Add one ignore pattern for the handoff notes to the clone's own `.git/info/exclude`.

    That file is local to the clone and is never committed, so nothing the user tracks is edited.
    The pattern is added once. If the file already has it in any form, including commented out,
    nothing is written: a `#` in front of the line is how the user turns this off. Returns True
    when a line was added. A file that git already tracks stays tracked; an ignore rule cannot
    change that.
    """
    try:
        path = _git_exclude(root)
        if path is None or path.is_symlink():
            return False
        old = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        if GIT_PATTERN in old:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        gap = "" if not old or old.endswith("\n") else "\n"
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{gap}{GIT_COMMENT}\n{GIT_PATTERN}\n")
        return True
    except OSError:
        return False


def save_summary(payload: dict, now: float) -> Path | None:
    root, text = _project(payload), payload.get("compact_summary")
    if root is None or not isinstance(text, str) or not text.strip():
        return None
    path = root / AUTO_NOTE
    if path.is_symlink():
        return None  # never write through a link
    path.parent.mkdir(parents=True, exist_ok=True)
    keep_previous(path)
    keep_out_of_git(root)
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now))
    body = text.strip().encode("utf-8")[:MAX_NOTE_BYTES].decode("utf-8", errors="ignore")
    path.write_text(f"<!-- saved by {PRODUCT_NAME} after a compaction, {stamp}; safe to delete -->\n{body}\n",
                    encoding="utf-8")
    return path


def newest_note(root: Path, now: float, fresh_s: float = FRESH_S) -> tuple[Path, float] | None:
    best = None
    for rel in (NOTE, AUTO_NOTE):
        p = root / rel
        try:
            if p.is_file() and not p.is_symlink() and p.stat().st_size:
                age = now - p.stat().st_mtime
                if 0 <= age <= fresh_s and (best is None or age < best[1]):
                    best = (rel, age)
        except OSError:
            continue
    return best


def session_start(payload: dict, now: float, home: Path | None = None) -> dict | None:
    reply = _session_start(payload, now)
    source = payload.get("source")
    if source in ("startup", "clear", "resume", "compact"):
        from .dashlink import reminder_text
        # A compaction summarizes the first reminder away, so it is given again then.
        extra = reminder_text(home if home is not None else Path.home(), payload, now, again=source == "compact")
        if extra:
            ctx = reply["hookSpecificOutput"]["additionalContext"] + "\n" if reply else ""
            reply = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx + extra}}
    if source in ("startup", "clear", "compact"):
        from .connectors import off_note
        note = off_note(home if home is not None else Path.home())
        if note:
            ctx = reply["hookSpecificOutput"]["additionalContext"] + "\n" if reply else ""
            reply = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx + note}}
    return reply


def _session_start(payload: dict, now: float) -> dict | None:
    if payload.get("source") == "compact":
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": CARRY_ON}}
    if payload.get("source") not in ("startup", "clear"):
        return None  # resume and fork already carry their own context
    root = _project(payload)
    found = newest_note(root, now) if root else None
    if not found:
        return None
    keep_out_of_git(root)  # covers a note the agent wrote for `/handoff`, which no hook saw being written
    rel, age = found
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": (
        f"{PRODUCT_NAME}: a handoff note from an earlier session in this project is at `{rel.as_posix()}` "
        f"(saved {_age(age)} ago). If the user's first message continues that work, or is a greeting or an "
        "open question such as \"continue\" or \"what's happening?\", read the note once before anything else, "
        "say where things stand in at most three lines, and do its next step in the same turn. Do not ask the "
        "user to confirm or to say go first. Anything the note lists as waiting on the user still waits, and "
        "so does anything that needs confirmation under your other rules. If the first message asks for "
        "something else, ignore the note and do not read it.")}}


def run_auto(stdin=None, out: Callable[[str], None] = print, now: Callable[[], float] = time.time,
             home: Path | None = None) -> int:
    """Always exits 0. Any failure prints nothing."""
    try:
        payload = parse_payload(read_raw(sys.stdin if stdin is None else stdin))
        event = payload.get("hook_event_name")
        if event == "PreCompact":
            out(KEEP)
        elif event == "PostCompact":
            save_summary(payload, now())
        elif event == "SessionStart":
            reply = session_start(payload, now(), home)
            if reply:
                out(json.dumps(reply))
    except Exception:  # noqa: BLE001 - a hook must never get in the way
        return 0
    return 0
