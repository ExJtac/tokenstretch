"""`start`: the one command for someone who just wants it set up.

Checks the machine, lists what `setup` would apply by its safe defaults plus automatic plan readings
(`pace auto`), asks once, applies, and says in plain words how the plan's week is going. Without
`--yes` and without a person at a terminal it changes nothing and prints the command that applies.

Reuses `setup` (plan and apply, with backups and a manifest per step, so `undo` restores them),
`paceauto` (the switch) and `pace` (the reading). Nothing here opens a network connection, and
the optional pinned installs `setup` offers are never part of `start`.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Callable

from . import pace as pc
from . import paceauto
from .context import Context
from .identity import CLI_NAME, VERSION
from .installer import _now_stamp
from .setup import apply_plan, build_plan, detected_adapters, select_items, sys_stdin_isatty

MIN_PYTHON = (3, 11)
READING_ADAPTER = "claude-code"     # the only tool whose status line hands over the plan's limits
PACE_AUTO_ID = "pace-auto"
WEEK_MIN_S = 5 * 86400              # a stored window this long or longer counts as "the plan's week"
AHEAD_POINTS = 5.0                  # used this many points above the share of the week gone: ahead
BEHIND_POINTS = 10.0                # used this many points below it: behind


# ---- the machine ------------------------------------------------------------------------------

def python_check() -> dict:
    v = sys.version_info
    return {"version": f"{v[0]}.{v[1]}.{v[2]}", "ok": (v[0], v[1]) >= MIN_PYTHON}


def _short(path: str, home: Path) -> str:
    try:
        return "~/" + Path(path).relative_to(home).as_posix()
    except ValueError:
        return Path(path).as_posix()


def _q(text: str) -> str:
    return f'"{text}"' if any(c in text for c in " \t'\"") else text


def planned_changes(ctx: Context, items, pace_auto_missing: bool) -> list[dict]:
    """Each change as {id, title, files}: what setup would apply, then the plan readings switch."""
    rows = []
    for item in items:
        files: list[str] = []
        for ch in item.changes:
            f = _short(ch["path"], ctx.home)
            if f not in files:
                files.append(f)
        rows.append({"id": item.id, "title": item.title, "files": files})
    if pace_auto_missing:
        rows.append({"id": PACE_AUTO_ID, "title": "Switch on automatic plan readings (`pace auto`)",
                     "files": [_short(str(pc.state_path(ctx.home)), ctx.home)]})
    return rows


# ---- how the plan's week is going ---------------------------------------------------------------

def _week_windows(state: dict) -> dict:
    """Windows that look like the plan's week and hold a reading. The one for all models comes first."""
    wins = {n: w for n, w in state["windows"].items()
            if int(w.get("length_s") or 0) >= WEEK_MIN_S and w.get("readings")}
    return dict(sorted(wins.items(), key=lambda kv: (bool(kv[1].get("models")), kv[0] != "auto-week", kv[0])))


def pace_summary(ctx: Context, now: float | None = None) -> dict:
    """Plain-words reading of the week: has_reading, used_percent, week_passed_percent, status, lines."""
    now = time.time() if now is None else now
    state = pc.load_state(ctx.home)
    out = {"has_reading": False, "window": None, "used_percent": None, "week_passed_percent": None,
           "status": "no-reading", "estimated": False, "resets_in": None}
    wins = _week_windows(state)
    lines: list[str]
    if not wins:
        out["lines"] = [
            "No plan reading yet. One appears after the first reply in a Claude Code session in a terminal.",
            f"Or type one: {CLI_NAME} pace set --window weekly --used 60 --resets 2026-01-12T09:00:00 --length 7d "
            "(your plan's usage page shows both numbers).",
            "Next: use Claude Code as you normally do, then run this command again.",
        ]
        return out
    sub = {"windows": wins}
    rows = pc.evaluate(sub, pc.window_sessions(ctx, sub), now)
    live = [r for r in rows if not r["finished"]]
    if not live:
        out.update(window=rows[0]["window"], status="stale")
        out["lines"] = [
            "The last plan reading is from a week that has already ended.",
            "A new one appears after the next reply in a Claude Code session in a terminal.",
            "Next: use Claude Code as you normally do, then run this command again.",
        ]
        return out
    def _used(r):
        return r["est_used"] if r["calibrated"] else r["reading_used"]

    # Several weekly limits can be stored (all models, one model family). Report the tightest one.
    w = max(live, key=lambda r: _used(r) - r["time_share"] * 100)
    name = w["window"]
    share = w["time_share"] * 100
    used = _used(w)
    out.update(has_reading=True, window=name, used_percent=round(used), week_passed_percent=round(share),
               estimated=w["calibrated"], resets_in=pc.dur(max(0.0, pc.parse_time(w["resets"]) - now)))
    lead = "About" if w["calibrated"] else "At the last reading, about"
    which = f"your tightest weekly limit (`{name}`)" if len(live) > 1 else "this week's plan"
    first = (f"{lead} {round(used)}% of {which} is used, and {round(share)}% of the week has passed "
             f"(it resets in {out['resets_in']}).")
    # An old reading is a floor: use only grows. So "too fast" can be said from it, "fine" cannot.
    if not w["calibrated"] and used - share < AHEAD_POINTS:
        out["status"] = "unknown"
        out["lines"] = [first,
                        "Not enough usage is logged this week to say if that is on track.",
                        "Next: nothing. Run this command again after you have used Claude Code for a while."]
        return out
    diff = used - share
    if diff >= AHEAD_POINTS:
        out["status"] = "ahead"
        lines = [first,
                 "Too fast: you are using the plan faster than the week is passing, so it may run out before it resets.",
                 "Next: keep sessions short, and use a cheaper model for routine steps."]
    elif diff <= -BEHIND_POINTS:
        out["status"] = "behind"
        lines = [first,
                 "Room to spare: you are using less than an even share of the week.",
                 "Next: nothing. You can work as you normally do."]
    else:
        out["status"] = "on-track"
        lines = [first,
                 "On track: your use matches how much of the week has passed.",
                 "Next: nothing. Run this command again any time to check."]
    out["lines"] = lines
    return out


# ---- the command ----------------------------------------------------------------------------------

def run_start(ctx: Context, *, yes: bool = False, as_json: bool = False, detail: bool = False,
              interactive: bool | None = None, input_fn=input, out: Callable[[str], None] = print,
              flags: list[str] | None = None, now: float | None = None) -> int:
    """Exit codes: 0 fine (also when nothing was changed or the question was answered no),
    1 a real failure (Python too old, or a step failed while applying)."""
    flags = list(flags or [])
    shown = " ".join(_q(f) for f in flags)
    cmd = lambda *parts: " ".join([CLI_NAME, *parts] + ([shown] if shown else []))   # noqa: E731
    if interactive is None:
        interactive = sys_stdin_isatty()
    say = (lambda line="": None) if as_json else (lambda line="": out(line))

    py = python_check()
    adapters = detected_adapters(ctx)
    tools = [a.display_name for a in adapters if a.id != "host"]
    all_items, _notes = build_plan(ctx, adapters)
    items, _optional = select_items(all_items)
    state = pc.load_state(ctx.home)
    has_reader = any(a.id == READING_ADAPTER for a in adapters)
    auto_missing = has_reader and not paceauto.is_on(state)
    changes = planned_changes(ctx, items, auto_missing)
    undo = [cmd("undo")] if items else []
    if auto_missing or paceauto.is_on(state):
        undo.append(cmd("pace", "auto", "--off"))
    result: dict = {
        "version": VERSION,
        "checks": {"python": {**py, "needs": "%d.%d" % MIN_PYTHON}, "tools": tools,
                   "setup_steps_pending": len(items), "pace_auto": paceauto.is_on(state)},
        "planned_changes": changes, "applied": False, "mode": "dry-run", "apply_command": cmd("start", "--yes"),
        "undo": undo, "pace": None, "error": None,
    }

    def finish(code: int) -> int:
        if as_json:
            print(json.dumps(result, indent=2))
        return code

    say("Checking this computer:")
    say(f"  Python {py['version']}: " + ("fine." if py["ok"] else "too old. tokenstretch needs Python 3.11 or newer."))
    say("  Coding tools found: " + (", ".join(tools) if tools else "none (Claude Code, Codex, Gemini CLI and more are supported)."))
    say("  tokenstretch setup: " + (f"{len(items)} change(s) not applied yet." if items else "already in place."))
    say("  Automatic plan readings: " + ("on." if paceauto.is_on(state) else
                                         "off." if has_reader else "not available (they come from Claude Code)."))
    say()
    if not py["ok"]:
        result["mode"], result["error"] = "python-too-old", "Python 3.11 or newer is needed"
        say("Nothing was changed. Install Python 3.11 or newer, then run this command again.")
        return finish(1)
    if not changes:
        result["mode"] = "already-set-up" if tools else "no-tool"
        say("Already set up. Nothing was changed." if tools else
            "No supported coding tool was found, so there is nothing to set up. Nothing was changed.")
        if tools:
            result["pace"] = pace_summary(ctx, now)
            say()
            for line in result["pace"]["lines"]:
                say(line)
        return _tail(ctx, flags, detail, as_json, finish)

    say("These changes would be made:" if not yes else "Making these changes:")
    for n, c in enumerate(changes, 1):
        say(f"  {n}. {c['title']}  ({', '.join(c['files'])})")
    say("Your own files are backed up first. Nothing is downloaded.")
    say()
    if not yes:
        if as_json or not interactive:
            say("Nothing was changed. To apply these changes, run:")
            say(f"  {result['apply_command']}")
            return finish(0)
        say("Apply these changes? [y/N] ")
        try:
            answer = input_fn()
        except EOFError:
            answer = ""
        if answer.strip().lower() not in {"y", "yes"}:
            result["mode"] = "declined"
            say("No changes made. To apply them later, run:")
            say(f"  {result['apply_command']}")
            return finish(0)

    done = 0
    try:
        if items:
            apply_plan(ctx, items, _now_stamp(), lambda _line: None)
            done = len(items)
        if auto_missing:
            paceauto.switch(ctx.home, True)
            done += 1
        say(f"Done: {done} change{'s' if done != 1 else ''} made. Your own files were backed up first.")
    except (OSError, RuntimeError, ValueError) as exc:
        result["error"] = str(exc)
        result["applied"] = bool(items)
        print(f"start stopped: {exc}", file=sys.stderr)
        if items:
            print(f"Some changes were made. To put them back: {cmd('undo')}", file=sys.stderr)
        return finish(1)
    result.update(applied=True, mode="applied")
    say()
    say("To undo: " + "; ".join(f"`{u}`" for u in undo) + ".")
    say()
    result["pace"] = pace_summary(ctx, now)
    for line in result["pace"]["lines"]:
        say(line)
    return _tail(ctx, flags, detail, as_json, finish)


def _tail(ctx: Context, flags: list[str], detail: bool, as_json: bool, finish) -> int:
    """`--detail`: the normal `pace` output, printed by the real command."""
    if detail and not as_json:
        from .cli import main
        print()
        main(["pace", *flags])
    return finish(0)
