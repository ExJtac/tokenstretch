"""`tokenstretch model rework`: how much spend went to redo work, per model and effort.

Reads the raw Claude Code session logs (read-only, local) because the per-step records the adapters
keep carry only token counts, a tool-call count and Read paths. Edit paths, command text, command
results (pass or fail) and user prompts exist only in the raw log lines, so this module walks them.
Other tools' logs do not expose those fields here, so their sessions are listed as not readable
rather than guessed.

Signals, each a heuristic and each named in the output:
  after-fail  a file edited again after a failing test or build command in the same session
  3x-edit     the same file edited 3 or more times (the third and later edits)
  correction  steps that follow a user prompt carrying a correction marker (see CORRECTION_MARKERS)
  fail-fix    steps after the first failing check, up to the next passing one

A step counts once however many signals hit it. Units are price weighted (see cost.py), an estimate
and not a bill. Models without a price ratio are counted in steps only, never guessed.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import cost

# Plain words that usually mean the last result was not accepted. Matched as whole words,
# case-insensitive, only in short prompts (a long prompt is a new task, not a correction).
CORRECTION_MARKERS = ("still", "again", "wrong", "not what", "redo", "broke", "broken", "doesn't work",
                      "didn't work", "that's not", "undo", "revert")
CORRECTION_MAX_CHARS = 300
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
REPEAT_EDITS = 3
_CHECK_CMD = re.compile(
    r"\b(pytest|unittest|tox|nox|jest|vitest|mocha|rspec|phpunit|tsc|mypy|ruff|flake8|pylint|eslint|"
    r"make|cmake|gradle|mvn|cargo\s+(test|build|check)|go\s+(test|build|vet)|dotnet\s+(test|build)|"
    r"(npm|yarn|pnpm)\s+(run\s+)?(test|build|lint|check)|python3?\s+-m\s+(pytest|unittest|build)|swift\s+(test|build))\b")
_MARKER_RE = re.compile(r"(?<!\w)(" + "|".join(re.escape(m) for m in CORRECTION_MARKERS) + r")(?!\w)", re.I)
_SIGNALS = ("after-fail", "3x-edit", "correction", "fail-fix")
_EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
TAIL_BYTES = 4_000_000          # same window the adapter reads, so steps line up with its turns
MIN_FOR_COMPARE = 3

HEURISTIC_NOTE = ("These are heuristics from local logs, not measurements: a file edited twice may be good work, "
                  "and a correction word may not be a correction. Units are an estimate, not a bill.")
NOT_READABLE = ("Not readable from the logs, so left out: which test failed and why, whether a redo was avoidable, "
                "and any signal for tools other than Claude Code (their logs do not expose edit paths or command results here).")


def is_check_command(cmd: str) -> bool:
    return bool(_CHECK_CMD.search(cmd or ""))


def is_correction(text: str) -> bool:
    text = (text or "").strip()
    return 0 < len(text) <= CORRECTION_MAX_CHARS and bool(_MARKER_RE.search(text))


def _user_text(obj: dict) -> str | None:
    """Text of a real user prompt, or None for tool results, notices and anything else."""
    if obj.get("isMeta") or obj.get("isSidechain"):
        return None
    msg = obj.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
        text = " ".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    else:
        return None
    text = text.strip()
    if not text or text.startswith("<") or isinstance(obj.get("origin"), dict):
        return None
    return text


def scan_log(path: Path, tail_bytes: int = TAIL_BYTES) -> dict | None:
    """Events of one Claude Code log: {"steps": n, "events": [...]} with step indexes in turn order."""
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - tail_bytes))
            chunk = fh.read()
    except OSError:
        return None
    lines = chunk.splitlines()
    if size > tail_bytes and lines:
        lines = lines[1:]
    step_of: dict[str, int] = {}
    pending: dict[str, tuple[int, str]] = {}     # tool_use id -> (step, command) for check commands
    events: list[tuple] = []
    for n, raw in enumerate(lines):
        if b'"assistant"' not in raw and b'"user"' not in raw:
            continue
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(obj, dict):
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        if obj.get("type") == "assistant":
            model = str(msg.get("model") or "")
            if not model or model.startswith("<"):
                continue
            key = str(msg.get("id") or f"line{n}")
            step = step_of.setdefault(key, len(step_of))
            for b in msg.get("content") or []:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                name = b.get("name")
                if name in EDIT_TOOLS:
                    fp = inp.get("file_path") or inp.get("notebook_path")
                    if isinstance(fp, str) and fp:
                        events.append(("edit", step, fp))
                elif name == "Bash" and is_check_command(str(inp.get("command") or "")):
                    pending[str(b.get("id"))] = (step, str(inp.get("command")))
        elif obj.get("type") == "user":
            text = _user_text(obj)
            if text is not None:
                events.append(("prompt", len(step_of), text))
                continue
            content = msg.get("content")
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result" and str(b.get("tool_use_id")) in pending:
                    step, _ = pending.pop(str(b.get("tool_use_id")))
                    events.append(("check", step, bool(b.get("is_error"))))
    return {"steps": len(step_of), "events": events}


def redo_steps(steps: int, events: list[tuple]) -> dict[int, set[str]]:
    """Step index -> the signals that marked it. Pure function of the events; see module docstring."""
    marks: dict[int, set[str]] = {}

    def mark(i: int, sig: str) -> None:
        if 0 <= i < steps:
            marks.setdefault(i, set()).add(sig)

    edits: dict[str, int] = {}
    failed_since: dict[str, bool] = {}      # file -> a check failed since its last edit
    failing_open = False
    fail_step = -1
    anyfail = 0                             # failing checks seen so far (for after-fail)
    last_edit_fail_count: dict[str, int] = {}
    for ev in events:
        kind = ev[0]
        if kind == "edit":
            _, step, fp = ev
            edits[fp] = edits.get(fp, 0) + 1
            if fp in last_edit_fail_count and anyfail > last_edit_fail_count[fp]:
                mark(step, "after-fail")
            last_edit_fail_count[fp] = anyfail
            if edits[fp] >= REPEAT_EDITS:
                mark(step, "3x-edit")
        elif kind == "check":
            _, step, failed = ev
            if failed:
                anyfail += 1
                if not failing_open:
                    failing_open, fail_step = True, step
            elif failing_open:
                for i in range(fail_step + 1, step + 1):
                    mark(i, "fail-fix")
                failing_open = False
    # correction: steps after a correcting prompt until the next prompt
    prompts = [(ev[1], ev[2]) for ev in events if ev[0] == "prompt"]
    for k, (at, text) in enumerate(prompts):
        if is_correction(text):
            end = prompts[k + 1][0] if k + 1 < len(prompts) else steps
            for i in range(at, end):
                mark(i, "correction")
    return marks


def turn_units(t: dict, first: bool) -> float | None:
    """Price-weighted units of one step, the same weighting `model cost` uses; None when unpriced."""
    model = t.get("model") or ""
    read, write = int(t.get("cache_read") or 0), int(t.get("cache_write") or 0)
    again = write if (not first and write >= cost.RESAVE_MIN and write > read) else 0
    sent = int(t.get("history_tokens") or 0)
    resave = min(again, write)
    u = cost.units(model, new=max(0, sent - read - write), reread=read, write=write - resave,
                   resave=resave, output=int(t.get("output_tokens") or 0))
    return None if u is None else sum(u.values())


def rework_view(sessions: list[dict]) -> dict:
    rows: dict[tuple, dict] = {}
    unreadable = skipped = 0
    for s in sessions:
        path = s.get("path")
        turns = s.get("turns") or []
        if s.get("adapter") != "claude-code" or not path:
            unreadable += 1
            continue
        scan = scan_log(Path(path))
        if scan is None or scan["steps"] != len(turns):
            skipped += 1
            continue
        marks = redo_steps(len(turns), scan["events"])
        seen = set()
        for i, t in enumerate(turns):
            key = (t.get("model") or "", t.get("effort") or "")
            row = rows.setdefault(key, {"model": key[0], "effort": key[1], "sessions": 0, "steps": 0, "redo_steps": 0,
                                        "units": 0.0, "redo_units": 0.0, "priced": True,
                                        "signals": {sig: 0 for sig in _SIGNALS}})
            if key not in seen:
                seen.add(key)
                row["sessions"] += 1
            row["steps"] += 1
            u = turn_units(t, i == 0)
            if u is None:
                row["priced"] = False
            else:
                row["units"] += u
            if i in marks:
                row["redo_steps"] += 1
                if u is not None:
                    row["redo_units"] += u
                for sig in marks[i]:
                    row["signals"][sig] += 1
    out = sorted(rows.values(), key=lambda r: (-r["sessions"], r["model"], r["effort"]))
    for r in out:
        ok = r["priced"] and r["units"] > 0
        r["redo_share"] = (r["redo_units"] / r["units"]) if ok else None
        r["units_per_session"] = (r["units"] / r["sessions"]) if r["priced"] else None
        r["units_per_session_no_redo"] = ((r["units"] - r["redo_units"]) / r["sessions"]) if r["priced"] else None
    return {"read": len(sessions), "unreadable_sessions": unreadable, "skipped_sessions": skipped, "rows": out,
            "comparison": compare(out), "signals": list(_SIGNALS), "markers": list(CORRECTION_MARKERS),
            "note": HEURISTIC_NOTE, "left_out": NOT_READABLE}


def compare(rows: list[dict]) -> str:
    """One plain sentence comparing efforts of one model, only when two efforts each have enough sessions."""
    by_model: dict[str, list[dict]] = {}
    for r in rows:
        if r["priced"] and r["effort"] in _EFFORT_ORDER and r["sessions"] >= MIN_FOR_COMPARE and r["units"] > 0:
            by_model.setdefault(r["model"], []).append(r)
    for model, rs in sorted(by_model.items()):
        if len(rs) >= 2:
            rs.sort(key=lambda r: _EFFORT_ORDER.index(r["effort"]))
            lo, hi = rs[0], rs[-1]
            return (f"On {model}, {hi['effort']} sessions put {round(100 * hi['redo_share'])}% of units into redo "
                    f"and cost ~{_n(hi['units_per_session'])} units each; {lo['effort']} sessions put "
                    f"{round(100 * lo['redo_share'])}% and cost ~{_n(lo['units_per_session'])} units each "
                    f"(heuristic, small samples).")
    return "Sample too small to compare efforts: it needs 3 or more sessions at each of two efforts on one model."


def _n(x: float) -> str:
    return cost._n(x)


def format_rework(view: dict) -> list[str]:
    rows = view["rows"]
    lines = [f"Redo work in the {view['read']} newest conversations (local logs, read-only).", view["note"],
             "Signals: after-fail = file edited again after a failing test/build; 3x-edit = same file edited 3+ times; "
             "correction = steps after a prompt with a marker word (" + ", ".join(view["markers"]) + "); "
             "fail-fix = steps from a failing check to the next passing one."]
    if not rows:
        lines.append("No readable Claude Code sessions with steps were found.")
    else:
        lines += ["", f"{'model':<26}{'effort':<9}{'sess':>5}{'steps':>7}{'redo':>6}{'redo%':>7}"
                      f"{'units/sess':>12}{'w/o redo':>10}   signals (steps)"]
        for r in rows:
            share = f"{round(100 * r['redo_share'])}%" if r["redo_share"] is not None else "n/a"
            ups = _n(r["units_per_session"]) if r["units_per_session"] is not None else "no price"
            nor = _n(r["units_per_session_no_redo"]) if r["units_per_session_no_redo"] is not None else "no price"
            sig = " ".join(f"{k}={v}" for k, v in r["signals"].items())
            lines.append(f"{(r['model'] or 'unknown')[:25]:<26}{(r['effort'] or 'n/a'):<9}{r['sessions']:>5}"
                         f"{r['steps']:>7}{r['redo_steps']:>6}{share:>7}{ups:>12}{nor:>10}   {sig}")
        lines += ["", "A session stands in for a finished task: units/sess is the cost per task."]
    if view["unreadable_sessions"]:
        lines.append(f"{view['unreadable_sessions']} session(s) from other tools skipped: their logs do not expose "
                     "edit paths or command results here.")
    if view["skipped_sessions"]:
        lines.append(f"{view['skipped_sessions']} session(s) skipped: the raw log could not be matched to its steps.")
    lines += [view["left_out"],
              "Only the newest part of a long log is read (about the last 4 MB), and edits made through shell "
              "commands are not seen, so the counts are a floor.", "", view["comparison"]]
    return lines
