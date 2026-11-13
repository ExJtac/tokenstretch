"""`tokenstretch queue`: a file-based task queue so agents pull work with no lead steps spent on dispatch.

One JSON file per task under `<project>/.tokenstretch/queue/`. Each task holds a ready brief made by
`brief.py`. An agent runs `queue next`, does exactly that brief, then `queue done` or `queue fail`,
and ends its session: a fresh short session per task, never a long one.

Claiming is atomic: `T0001.claim` is created with an exclusive open, so two agents never get the
same task. A task whose files overlap a claimed task's files is skipped. `queue next` checks the
pace gate first (see `pace_gate`).

No network, no model calls. State lives only in the queue folder of the project you point it at.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import time
from pathlib import Path

from .brief import Brief, build, from_preset
from .identity import CLI_NAME, STATE_DIRNAME

EXIT_OVER_LINE = 3
EXIT_EMPTY = 4
MAX_FAILURES = 2        # after this many failures a task needs the lead
HARD_RULE = "This is a hard step: state your approach in one sentence before you edit anything."


def queue_dir(root: Path | str) -> Path:
    return Path(root) / STATE_DIRNAME / "queue"


# ---------------------------------------------------------------- pace gate

def pace_gate(home: Path | str | None = None) -> dict:
    """{"open": bool, "wait_seconds": int, "note": str}. A missing pace module means the gate is open.

    Runs `pace --gate --json` through the CLI when `tokenstretch.pace` exists; any failure to run or
    read it also leaves the gate open, so the queue never stalls on a broken meter."""
    try:
        import tokenstretch.pace  # noqa: F401
    except ImportError:
        return {"open": True, "wait_seconds": 0, "note": "no pace meter installed; gate open"}
    from .cli import main
    argv = ["pace", "--gate", "--json"] + (["--home", str(home)] if home else [])
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = main(argv)
    except SystemExit:
        return {"open": True, "wait_seconds": 0, "note": "pace gate could not run; gate open"}
    except Exception:  # noqa: BLE001 - a broken meter must not stall the team
        return {"open": True, "wait_seconds": 0, "note": "pace gate failed; gate open"}
    data: dict = {}
    with contextlib.suppress(ValueError):
        parsed = json.loads(buf.getvalue() or "{}")
        data = parsed if isinstance(parsed, dict) else {}
    closed = rc == EXIT_OVER_LINE or data.get("ok") is False
    return {"open": not closed, "wait_seconds": int(data.get("wait_seconds") or 0), "note": ""}


# ---------------------------------------------------------------- storage

def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_atomic(path: Path, data: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _claim_path(qd: Path, tid: str) -> Path:
    return qd / f"{tid}.claim"


def load_tasks(qd: Path) -> list[dict]:
    tasks = []
    for p in sorted(qd.glob("T[0-9]*.json")):
        t = _read(p)
        if t and t.get("id"):
            tasks.append(t)
    return tasks


def _claim_info(qd: Path, tid: str) -> dict | None:
    cp = _claim_path(qd, tid)
    try:
        st = cp.stat()
    except OSError:
        return None
    info = _read(cp) or {}
    info["mtime"] = st.st_mtime
    return info


def add_task(root: Path | str, task: str, *, files=(), done: str = "", preset: str = "", hard: bool = False,
             brief_rules=(), now: float | None = None) -> dict:
    """Write one task holding a ready brief. The id is taken with an exclusive link, so ids never collide."""
    qd = queue_dir(root)
    qd.mkdir(parents=True, exist_ok=True)
    rules = list(brief_rules) + ([HARD_RULE] if hard else [])
    given = {"task": task, "files": list(files), "done": done, "rules": rules}
    b = from_preset(preset, **given) if preset else Brief(**given)
    built = build(b)
    tier, effort = built.get("tier"), built.get("effort")
    if hard:
        tier = "top"
    seq = 1 + max((int(re.sub(r"\D", "", p.stem) or 0) for p in qd.glob("T[0-9]*.json")), default=0)
    while True:
        tid = f"T{seq:04d}"
        record = {"id": tid, "seq": seq, "created": time.time() if now is None else now, "task": task,
                  "files": list(files), "done": done, "preset": preset, "hard": bool(hard), "status": "open",
                  "failures": 0, "notes": [], "agent": "", "brief": built["brief"], "tier": tier, "effort": effort}
        tmp = qd / f".{tid}.{os.getpid()}.new"
        tmp.write_text(json.dumps(record, indent=1), encoding="utf-8")
        try:
            os.link(tmp, qd / f"{tid}.json")      # fails if another writer took this id
        except FileExistsError:
            seq += 1
            continue
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink()
        return record


# ---------------------------------------------------------------- plan parsing

_STEP = re.compile(r"^\s{0,3}(?:\*\*)?(\d+)[.)](?:\*\*)?\s+(.*\S)\s*$")
_STOP = re.compile(r"^\s{0,3}(#{1,6}\s|open questions\b|\*\*open questions)", re.I)
_CHECK = re.compile(r"\b(?:check|verify|verified by|done when|proof)\s*[:\-]\s*(.+)", re.I)
_FILES = re.compile(r"\bfiles?\s*[:\-]\s*(.+)", re.I)
_TICKED = re.compile(r"`([^`\s]+)`")
_HARD = re.compile(r"(\(hard\)|\[hard\]|\*\*hard\*\*|\bhard step\b|^hard:)", re.I)
_PATHISH = re.compile(r"[/\\]|\.\w{1,6}$")
_BARE_PATH = re.compile(r"(?<![\w./:`\\-])((?:[\w.-]+/)+[\w-][\w.-]*\.\w{1,6})(?![\w/])")   # src/a.py with no backticks


def parse_plan(text: str) -> list[dict]:
    """Split a numbered plan (the shape `brief --preset plan` asks for) into steps with files, check and hard flag."""
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if _STOP.match(line) and blocks:
            break
        m = _STEP.match(line)
        if m:
            blocks.append([m.group(2)])
        elif blocks and line.strip():
            blocks[-1].append(line.strip())
    steps = []
    for lines in blocks:
        body = " ".join(lines)
        files: list[str] = []
        for chunk in (m.group(1) for m in _FILES.finditer(body)):
            files += [f for f in _TICKED.findall(chunk)] or [f.strip(" `.,;") for f in re.split(r"[,;]| and ", chunk)]
        files += [f for f in _TICKED.findall(body) if _PATHISH.search(f)]
        files += _BARE_PATH.findall(_CHECK.sub("", body))      # a path inside the check command is not a file to edit
        seen: list[str] = []
        for f in files:
            f = f.strip()
            if f and _PATHISH.search(f) and f not in seen:
                seen.append(f)
        cm = _CHECK.search(body)
        check = cm.group(1).strip().rstrip(".") if cm else ""
        text_part = re.sub(r"\s*(?:files?|check|verify|done when)\s*[:\-].*$", "", lines[0], flags=re.I).strip() or lines[0]
        steps.append({"task": _HARD.sub("", text_part).strip(" -:") or text_part, "files": seen,
                      "done": check, "hard": bool(_HARD.search(body))})
    return steps


def add_from_plan(root: Path | str, plan_text: str, preset: str = "") -> list[dict]:
    return [add_task(root, s["task"], files=s["files"], done=s["done"], preset=preset, hard=s["hard"])
            for s in parse_plan(plan_text)]


# ---------------------------------------------------------------- claim / finish

def _norm(p: str) -> str:
    return os.path.normpath(p.strip()).rstrip("/")


def files_overlap(a, b) -> bool:
    """True when any path in a equals, contains or sits inside a path in b."""
    for x in map(_norm, a):
        for y in map(_norm, b):
            if x == y or x.startswith(y + "/") or y.startswith(x + "/"):
                return True
    return False


def _claimed(qd: Path, tasks: list[dict]) -> list[dict]:
    return [t for t in tasks if t.get("status") == "open" and _claim_path(qd, t["id"]).exists()]


def claim_next(root: Path | str, agent: str = "") -> tuple[dict | None, str]:
    """(task, "") with a claim on the oldest open task, or (None, reason) with reason "empty" or "blocked"."""
    qd = queue_dir(root)
    if not qd.is_dir():
        return None, "empty"
    blocked = False
    for t in sorted(load_tasks(qd), key=lambda t: t.get("seq", 0)):
        if t.get("status") != "open" or _claim_path(qd, t["id"]).exists():
            continue
        if any(files_overlap(t.get("files") or [], c.get("files") or []) for c in _claimed(qd, load_tasks(qd))):
            blocked = True
            continue
        try:
            fd = os.open(_claim_path(qd, t["id"]), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"agent": agent, "claimed": time.time()}, fh)
        fresh = _read(qd / f"{t['id']}.json") or t
        rival = [c for c in _claimed(qd, load_tasks(qd))
                 if c["id"] != t["id"] and c.get("seq", 0) < t.get("seq", 0)
                 and files_overlap(t.get("files") or [], c.get("files") or [])]
        if fresh.get("status") != "open" or rival:    # two agents claimed overlapping tasks at once: the older wins
            with contextlib.suppress(OSError):
                _claim_path(qd, t["id"]).unlink()
            blocked = blocked or bool(rival)
            continue
        fresh["agent"] = agent
        fresh["claimed"] = time.time()
        _write_atomic(qd / f"{t['id']}.json", fresh)
        return fresh, ""
    return None, "blocked" if blocked else "empty"


def _get(root, tid: str) -> tuple[Path, dict]:
    qd = queue_dir(root)
    t = _read(qd / f"{tid}.json")
    if t is None:
        raise KeyError(f"no task {tid}")
    return qd, t


def done_task(root, tid: str, note: str = "") -> dict:
    qd, t = _get(root, tid)
    t["status"] = "done"
    t["finished"] = time.time()
    if note:
        t["notes"].append({"kind": "done", "note": note})
    _write_atomic(qd / f"{tid}.json", t)
    with contextlib.suppress(OSError):
        _claim_path(qd, tid).unlink()
    return t


def fail_task(root, tid: str, note: str) -> dict:
    """Back to open with the note appended to the brief; after MAX_FAILURES failures: needs-lead."""
    qd, t = _get(root, tid)
    t["failures"] = int(t.get("failures") or 0) + 1
    t["notes"].append({"kind": "fail", "note": note})
    t["brief"] = t["brief"].rstrip("\n") + f"\n\nEarlier attempt {t['failures']} failed. Do not repeat it: {note.strip()}\n"
    t["status"] = "needs-lead" if t["failures"] >= MAX_FAILURES else "open"
    t["agent"] = ""
    _write_atomic(qd / f"{tid}.json", t)
    with contextlib.suppress(OSError):
        _claim_path(qd, tid).unlink()
    return t


def touch_task(root, tid: str) -> bool:
    cp = _claim_path(queue_dir(root), tid)
    try:
        os.utime(cp)
    except OSError:
        return False
    return True


def release_stale(root, minutes: float, now: float | None = None) -> list[str]:
    """Drop claims on open tasks with no update for `minutes`. The task goes back to open."""
    qd = queue_dir(root)
    now = time.time() if now is None else now
    out = []
    for t in load_tasks(qd) if qd.is_dir() else []:
        info = _claim_info(qd, t["id"])
        if info is None:
            continue
        if t.get("status") != "open" or now - info["mtime"] >= minutes * 60:
            with contextlib.suppress(OSError):
                _claim_path(qd, t["id"]).unlink()
            if t.get("status") == "open":
                out.append(t["id"])
    return out


def list_view(root) -> list[dict]:
    qd = queue_dir(root)
    rows = []
    for t in load_tasks(qd) if qd.is_dir() else []:
        claimed = t.get("status") == "open" and _claim_path(qd, t["id"]).exists()
        rows.append({"id": t["id"], "status": "claimed" if claimed else t["status"], "failures": t.get("failures", 0),
                     "agent": (t.get("agent") or "") if (claimed or t.get("status") != "open") else "", "files": t.get("files") or [], "hard": bool(t.get("hard")),
                     "task": t.get("task", "")})
    return rows


def format_list(rows: list[dict]) -> list[str]:
    if not rows:
        return ["The queue is empty."]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    lines = [", ".join(f"{n} {k}" for k, n in sorted(counts.items())), ""]
    for r in rows:
        extra = " ".join(x for x in (f"by {r['agent']}" if r["agent"] else "", f"{r['failures']} failed" if r["failures"] else "",
                                     "hard" if r["hard"] else "") if x)
        lines.append(f"{r['id']}  {r['status']:<11}{r['task'][:70]}" + (f"  ({extra})" if extra else ""))
    return lines


def next_text(t: dict) -> str:
    head = (f"Queue task {t['id']}. Do exactly this brief, run its check, then `{CLI_NAME} queue done {t['id']}` "
            f"or `{CLI_NAME} queue fail {t['id']} --note \"what went wrong\"`. Then end the session."
            f" Suggested: {t.get('tier') or 'mid'} model, {t.get('effort') or 'medium'} effort.")
    return head + "\n\n" + t["brief"]


def loop_prompt() -> str:
    c = CLI_NAME
    return (
        "You are a queue worker. Do exactly this, once, then end the session:\n"
        f"1. Run `{c} queue next --agent <your name>`.\n"
        f"2. Exit code 3 (over the pace line) or 4 (queue empty): print the message, change nothing, and stop.\n"
        "3. Otherwise do exactly the brief it printed. Touch no file outside the brief. Do not take on other work.\n"
        "4. Run the brief's check once.\n"
        f"5. Check passed: `{c} queue done <id> --note \"one line\"`. Check failed or you are stuck: "
        f"`{c} queue fail <id> --note \"what failed and why\"`.\n"
        "6. End the session now. Never start a second task in the same session: a fresh session per task "
        "keeps the history short and the cache hot."
    )
