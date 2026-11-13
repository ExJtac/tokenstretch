"""The dashboard's link file and the once-per-session "View your dashboard" reminder.

Two small state files live under the state directory (`~/.tokenstretch/`):

  * `dashboard.json`: written by a running `tokenstretch dashboard` (url, port, pid, start time),
    removed when it stops. A leftover file whose port no longer answers is treated as stale.
  * `dashboard-remind.json`: the reminder switch. Absent means off, so an upgrade changes nothing
    until `tokenstretch setup` (or `dashboard --remind on`) turns it on.

The reminder rides the SessionStart hook that `setup` already installs. It adds one short line to
the session's starting context, once per session, asking the assistant to end its last message with
the link. It never interrupts the task and never asks the user anything.

Only the loopback address is ever produced (http://127.0.0.1:<port>/<token>/). The token is written
to the link file and to the hook's context, nowhere else: not to logs, reports or `--json` output.
Core only: no network beyond a connect to 127.0.0.1 to see whether the dashboard is still there.
"""
from __future__ import annotations

import json
import os
import re
import socket
import time
from pathlib import Path

from .identity import CLI_NAME, PRODUCT_NAME, state_dir

LINK_FILE = "dashboard.json"
REMIND_FILE = "dashboard-remind.json"
MARK_DIR = "dashremind"
KEEP_S = 7 * 24 * 3600
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
URL_RE = re.compile(r"http://127\.0\.0\.1:(\d{1,5})/[A-Za-z0-9_-]{8,200}/")


def link_path(home: Path | str) -> Path:
    return state_dir(home) / LINK_FILE


def remind_path(home: Path | str) -> Path:
    return state_dir(home) / REMIND_FILE


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    try:
        path.chmod(0o600)  # owner-only where the platform supports it; a no-op failure on Windows
    except (OSError, NotImplementedError):
        pass


# ---- the link file ---------------------------------------------------------------------------

def write_link(home: Path | str, url: str, port: int, now: float | None = None) -> bool:
    """Record the running dashboard. Refuses anything that is not a loopback URL."""
    if not URL_RE.fullmatch(url or ""):
        return False
    try:
        _write_private(link_path(home), json.dumps({"url": url, "port": int(port), "pid": os.getpid(),
                                                    "started": now if now is not None else time.time()}))
        return True
    except OSError:
        return False


def clear_link(home: Path | str, url: str | None = None) -> None:
    """Remove the link file; with `url`, only when it still names that run (a newer run keeps its own)."""
    p = link_path(home)
    try:
        if url is not None and json.loads(p.read_text(encoding="utf-8")).get("url") != url:
            return
        p.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def _answers(port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except (OSError, ValueError, OverflowError):
        return False


def live_url(home: Path | str, check=_answers) -> str | None:
    """The running dashboard's loopback URL, or None (no file, bad file, or nothing answers its port)."""
    try:
        data = json.loads(link_path(home).read_text(encoding="utf-8"))
        url, port = data["url"], int(data["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    m = URL_RE.fullmatch(url) if isinstance(url, str) else None
    if not m or int(m.group(1)) != port or not check(port):
        return None
    return url


# ---- the reminder switch ---------------------------------------------------------------------

def remind_enabled(home: Path | str) -> bool:
    try:
        return json.loads(remind_path(home).read_text(encoding="utf-8")).get("remind") is True
    except (OSError, ValueError, AttributeError):
        return False


def set_remind(home: Path | str, on: bool) -> None:
    _write_private(remind_path(home), json.dumps({"remind": bool(on)}) + "\n")


def setup_change(home: Path | str) -> dict:
    """The change `setup` plans for the switch (core file, backed up and undone like any other)."""
    return {"path": str(remind_path(home)), "kind": "modify" if remind_path(home).is_file() else "create", "content": json.dumps({"remind": True}) + "\n",
            "reason": "lets the session-start hook ask the assistant to end with your dashboard link (once per session)",
            "key": "dashboard-remind"}


# ---- the reminder itself ---------------------------------------------------------------------

def _mark(home: Path | str, session: str) -> Path | None:
    return state_dir(home) / MARK_DIR / f"{session}.json" if SAFE_ID.fullmatch(session or "") else None


def reminder_text(home: Path | str, payload: dict, now: float, check=_answers) -> str | None:
    """One short line for this session's starting context, or None (off, no safe session id, or already given)."""
    try:
        mark = _mark(home, str(payload.get("session_id") or ""))
        if mark is None:
            return None  # no id: cannot say "once", so say nothing (and nothing is read)
        if not remind_enabled(home) or mark.exists():
            return None
        url = live_url(home, check)
        if url:
            text = (f"{PRODUCT_NAME}: when you finish the user's task, end your last message with this line: "
                    f"View your tokenstretch dashboard at {url}")
        else:
            text = (f"{PRODUCT_NAME}: when you finish the user's task, end your last message with this line: "
                    f"Run `{CLI_NAME} dashboard` to see your usage dashboard.")
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(json.dumps({"ts": now}), encoding="utf-8")
        for old in mark.parent.glob("*.json"):
            if old != mark and now - old.stat().st_mtime > KEEP_S:
                old.unlink()
        return text + " Do not stop the task for this, and do not ask the user about it."
    except OSError:
        return None
