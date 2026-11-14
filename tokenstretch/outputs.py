"""Largest tool outputs in recent Claude Code sessions: which tools put the most text into history.

Every tool result (a file read, a command's output, a search, a connector's reply) is added to the
conversation. This module reads the local session logs and adds up the text of each result as it was
logged, per tool, and lists the largest single ones. It names the tool and, for a file tool, the
file; for a shell command only the program's name, never its arguments or its output.

Sizes are characters of logged text; tokens are that / 4, an estimate. Images and other non-text
parts are not counted. Read-only: nothing here changes a file.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .identity import CLI_NAME

DAYS = 7
MAX_BYTES = 2 * 1024 * 1024 * 1024   # stop reading logs after this much; the report then says so
_PATH_KEYS = ("file_path", "path", "notebook_path")
_TIPS = {
    "Read": "ask for the part you need (a line range) instead of the whole file",
    "Bash": "ask for the end or a count of long output (for example `| tail -40`), not all of it",
    "Grep": "narrow the pattern or the folder, or ask for file names only",
    "Glob": "narrow the pattern or the folder",
    "WebFetch": "ask the page a narrow question instead of fetching all of it",
    "Agent": "ask the helper for a short answer, not its notes",
    "Task": "ask the helper for a short answer, not its notes",
}


def _target(name: str, inp, home: Path) -> str:
    """A safe short label for what the call touched: a file path, or a command's first word."""
    if not isinstance(inp, dict):
        return ""
    for key in _PATH_KEYS:
        val = inp.get(key)
        if isinstance(val, str) and val:
            try:
                return "~/" + Path(val).relative_to(home).as_posix()
            except ValueError:
                return val
    cmd = inp.get("command")
    if isinstance(cmd, str) and cmd.strip():
        word = cmd.strip().split()[0]
        return Path(word).name[:40] if "=" not in word else "(command)"
    return ""


def _text_len(content) -> int:
    if isinstance(content, str):
        return len(content)
    n = 0
    for part in content if isinstance(content, list) else []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            n += len(part["text"])
    return n


def scan(home: Path, now: float | None = None, days: int = DAYS, max_bytes: int = MAX_BYTES, top: int = 10) -> dict:
    """Add up tool output text in the last `days` days of Claude Code logs.

    Returns {"tools": [{name, calls, chars, tokens, largest}], "largest": [{tool, target, chars,
    tokens, day}], "chars", "sessions", "days", "complete"}. `complete` is False when the byte limit
    stopped the read early; the totals then cover the newest sessions only.
    """
    now = time.time() if now is None else now
    files = []
    base = home / ".claude" / "projects"
    for dirpath, _dirs, names in os.walk(base) if base.is_dir() else []:
        for n in names:
            if n.endswith(".jsonl"):
                p = Path(dirpath) / n
                try:
                    st = p.stat()
                except OSError:
                    continue
                if now - st.st_mtime <= days * 86400 and not p.is_symlink():
                    files.append((st.st_mtime, st.st_size, p))
    since = time.strftime("%Y-%m-%d", time.gmtime(now - days * 86400))
    tools: dict[str, dict] = {}
    biggest: list[dict] = []
    read, complete, sessions = 0, True, 0
    for _mt, size, p in sorted(files, reverse=True):
        if read + size > max_bytes:
            complete = False
            break
        read += size
        sessions += 1
        calls: dict[str, tuple[str, str]] = {}
        try:
            with p.open(encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    is_use, is_result = '"tool_use"' in line, '"tool_result"' in line
                    if not (is_use or is_result):
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    msg = obj.get("message") if isinstance(obj, dict) else None
                    content = msg.get("content") if isinstance(msg, dict) else None
                    for b in content if isinstance(content, list) else []:
                        if not isinstance(b, dict):
                            continue
                        if b.get("type") == "tool_use" and isinstance(b.get("name"), str):
                            calls[str(b.get("id"))] = (b["name"], _target(b["name"], b.get("input"), home))
                        elif b.get("type") == "tool_result":
                            name, target = calls.pop(str(b.get("tool_use_id")), ("", ""))
                            chars = _text_len(b.get("content"))
                            day = obj.get("timestamp")
                            day = day[:10] if isinstance(day, str) else ""
                            if not name or not chars or (day and day < since):
                                continue                      # an old step in a session that was resumed
                            t = tools.setdefault(name, {"name": name, "calls": 0, "chars": 0, "largest": 0})
                            t["calls"] += 1
                            t["chars"] += chars
                            t["largest"] = max(t["largest"], chars)
                            if len(biggest) < top or chars > biggest[-1]["chars"]:
                                biggest.append({"tool": name, "target": target, "chars": chars, "tokens": chars // 4,
                                                "day": day})
                                biggest.sort(key=lambda r: -r["chars"])
                                del biggest[top:]
        except OSError:
            continue
    rows = sorted(tools.values(), key=lambda t: (-t["chars"], t["name"]))
    for t in rows:
        t["tokens"] = t["chars"] // 4
    return {"tools": rows, "largest": biggest, "chars": sum(t["chars"] for t in rows), "sessions": sessions,
            "days": days, "complete": complete}


def _k(n: int) -> str:
    return f"{n / 1000:.0f}k" if n >= 10_000 else f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def _brief(target: str, width: int = 60) -> str:
    parts = target.split("/")
    return target if len(target) <= width or len(parts) <= 3 else ".../" + "/".join(parts[-3:])


def format_report(d: dict, rows: int = 8) -> list[str]:
    out = [f"Tool outputs: text that tools put into your sessions in the last {d['days']} days of Claude Code logs "
           f"on this computer ({d['sessions']} session file{'s' if d['sessions'] != 1 else ''})."]
    if not d["tools"]:
        return out + ["  No tool output found in the logs for that time."]
    if not d["complete"]:
        out.append("  The logs were too large to read in full: these totals cover the newest sessions only.")
    total = d["chars"] or 1
    out.append("    share   ~tokens    calls  largest  tool")
    for t in d["tools"][:rows]:
        out.append(f"   {100 * t['chars'] / total:>5.0f}%  {_k(t['tokens']):>8}  {t['calls']:>7}  {_k(t['largest'] // 4):>7}  "
                   f"{t['name']}")
    rest = d["tools"][rows:]
    if rest:
        out.append(f"   {100 * sum(t['chars'] for t in rest) / total:>5.0f}%  {_k(sum(t['tokens'] for t in rest)):>8}  "
                   f"{sum(t['calls'] for t in rest):>7}        -  {len(rest)} other tool{'s' if len(rest) != 1 else ''}")
    if d["largest"]:
        out.append("  Largest single outputs:")
        for r in d["largest"]:
            what = f"{r['tool']} {_brief(r['target'])}".strip()
            out.append(f"   {_k(r['tokens']):>8} tokens  {r['day'] or '-':<10}  {what}")
    for t in d["tools"][:3]:
        if t["name"] in _TIPS:
            out.append(f"  {t['name']}: {_TIPS[t['name']]}.")
    out.append("  What this is: the size of each result as logged, in characters / 4, an estimate. A result stays in "
               "the conversation for the steps after it, so a large one is paid for more than once. Whether a "
               f"smaller one would still hold what the task needs is your call. `{CLI_NAME} watch` warns while "
               "a session runs.")
    return out
