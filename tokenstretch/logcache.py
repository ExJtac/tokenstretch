"""Incremental, read-only reading of local JSONL logs (core; no tool paths here).

Each file is cached by (path, inode, size, mtime). Unchanged files are skipped. Files that only
grew are parsed from the saved byte offset (a partial last line waits for the next pass). A file
that shrank or was replaced is parsed again from the start. Adapters supply the paths and an
incremental parser (`usage_log_parser`); the cache never interprets log contents itself.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable

from .context import Context
from .findings import UsageRecord


def stat_sig(path: Path) -> tuple[int, int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def split_complete(data: bytes) -> tuple[list[bytes], int]:
    """(complete lines, bytes consumed). A trailing partial line is left unless it is valid JSON."""
    cut = data.rfind(b"\n") + 1
    lines = data[:cut].splitlines()
    rest = data[cut:]
    if rest.strip():
        try:
            json.loads(rest)
        except ValueError:
            return lines, cut
        lines.append(rest)
        return lines, len(data)
    return lines, len(data)


CHUNK = 2 << 20


def read_lines_from(path: Path, offset: int, feed: Callable[[list[bytes]], None], *, final: bool = True) -> tuple[int, int]:
    """Stream complete lines from `offset` to EOF in 2 MB chunks into feed(). Returns (new offset, bytes read).

    A trailing partial line is not fed (unless it is already valid JSON) and the offset stops before
    it, so the next pass picks it up once the writer finishes it.
    """
    read = 0
    with open(path, "rb") as fh:
        fh.seek(offset)
        carry = b""
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            read += len(block)
            data = carry + block
            cut = data.rfind(b"\n") + 1
            if cut:
                feed(data[:cut].splitlines())
                offset += cut
                carry = data[cut:]
            else:
                carry = data
        if carry.strip() and final:
            lines, used = split_complete(carry)
            if lines:
                feed(lines)
                offset += used
    return offset, read


def loads(raw: bytes):
    """json.loads for one log line; tolerates invalid UTF-8 like the text readers did."""
    try:
        return json.loads(raw)
    except UnicodeDecodeError:
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            return None
    except ValueError:
        return None


# ---- small stat-keyed memo (watch / live sessions) ------------------------------------------

_MEMO: dict[tuple, tuple] = {}
_MEMO_LOCK = threading.Lock()
_MEMO_MAX = 8192


def memo_by_stat(path: Path, tag: str, fn: Callable[[], object]):
    """fn() result for this file, recomputed only when its (inode, size, mtime) changes."""
    sig = stat_sig(path)
    key = (str(path), tag)
    with _MEMO_LOCK:
        hit = _MEMO.get(key)
    if hit is not None and sig is not None and hit[0] == sig:
        return hit[1]
    value = fn()
    with _MEMO_LOCK:
        if len(_MEMO) >= _MEMO_MAX:
            _MEMO.pop(next(iter(_MEMO)))
        _MEMO[key] = (sig, value)
    return value


def rec_epoch(r: UsageRecord) -> float | None:
    """Record timestamp as epoch seconds, parsed once and kept on the record object."""
    d = r.__dict__
    if "_epoch" not in d:
        from .usage import _ts_to_epoch
        d["_epoch"] = _ts_to_epoch(r.ts) if r.ts else None
    return d["_epoch"]


# ---- incremental usage records ---------------------------------------------------------------

class _Entry:
    __slots__ = ("sig", "offset", "parser", "records")

    def __init__(self) -> None:
        self.sig = None
        self.offset = 0
        self.parser = None
        self.records: list[UsageRecord] = []


class LogCache:
    """Per-file cache of parsed usage records. One instance per long-running process (dashboard)."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], _Entry] = {}
        self.finished: dict[str, tuple] = {}
        self.stats = {"skipped_old": 0, "unchanged": 0, "appended": 0, "full": 0, "bytes_read": 0}
        self._lock = threading.Lock()

    def _update(self, ad, path: Path, sig) -> _Entry:
        key = (ad.id, str(path))
        e = self.entries.get(key)
        if e is not None and e.sig == sig:
            self.stats["unchanged"] += 1
            return e
        grew = (e is not None and e.parser is not None and e.sig is not None
                and e.sig[0] == sig[0] and sig[1] >= e.sig[1] and sig[1] >= e.offset)
        if not grew:
            e = _Entry()
            e.parser = ad.usage_log_parser(path)
            self.entries[key] = e
            if e.parser is None:  # e.g. compressed archives: whole-file parse, cached by stat
                e.records = list(ad.parse_log(path) or [])
                e.sig = sig
                self.stats["full"] += 1
                return e
            self.stats["full"] += 1
        else:
            self.stats["appended"] += 1
        try:
            e.offset, n = read_lines_from(path, e.offset, e.parser.feed)
        except OSError:
            return e
        self.stats["bytes_read"] += n
        e.records = e.parser.records
        e.sig = sig
        return e

    def records(self, ctx: Context, adapters, *, since_days: int = 63) -> list[UsageRecord]:
        """Usage records in the window, like read_usage_logs, but incremental across calls."""
        cutoff = time.time() - since_days * 86400
        out: list[UsageRecord] = []
        with self._lock:
            for ad in adapters:
                if not getattr(ad, "incremental_logs", False):  # no incremental parser: plain read
                    try:
                        out += list(ad.read_usage_logs(ctx, since_days=since_days) or [])
                    except (OSError, ValueError):
                        pass
                    continue
                used, keys = [], []
                for path in ad.default_log_paths(ctx):
                    sig = stat_sig(path)
                    if sig is None:
                        continue
                    if sig[2] / 1e9 < cutoff:  # nothing in it can be newer than its mtime
                        self.stats["skipped_old"] += 1
                        continue
                    used.append(self._update(ad, path, sig))
                    keys.append((str(path), sig))
                # Cross-file post-processing is cached until a file changes or the window moves a lot.
                fkey = (tuple(keys), int(cutoff // 600))
                hit = self.finished.get(ad.id)
                if hit is not None and hit[0] == fkey:
                    out += hit[1]
                    continue
                mine = []
                for e in used:
                    for r in e.records:
                        ep = rec_epoch(r)
                        if ep is None or ep >= cutoff:
                            mine.append(r)
                done = ad.finish_usage_records(mine)
                self.finished[ad.id] = (fkey, done)
                out += done
        return out
