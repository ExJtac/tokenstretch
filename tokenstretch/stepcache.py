"""Every step of each local log, kept between runs so a sum over a plan window stays cheap.

`pace` adds up a week of logs on each call, and a team asks it before every task. Parsing the whole
week again each time takes far too long, so this cache keeps, per log file, the steps already read
(time, model, effort, token counts) and the byte offset reached. An unchanged file is not opened;
a file that grew is read from the saved offset; a file that shrank or was replaced is read again
from the start. The cache is one local JSON file in the state folder and holds no prompt or reply
text. A missing or damaged cache is rebuilt.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from .logcache import read_lines_from, stat_sig

VERSION = 1
KEEP_IDS = 64           # newest step ids kept per file, so a step split over the offset is not counted twice
FIELDS = ("ts", "model", "effort", "history_tokens", "output_tokens", "cache_read", "cache_write")

# feed(lines, seen) -> [(step id, [values in FIELDS order])] for the steps first seen in these lines.
Feed = Callable[[list, set], list]


class StepCache:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.files: dict[str, dict] = {}
        self.touched: set[str] = set()
        self.dirty = False
        if path is None:
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("v") == VERSION and isinstance(data.get("files"), dict):
                self.files = data["files"]
        except (OSError, ValueError):
            self.files = {}

    def steps(self, log: Path, feed: Feed) -> list[dict]:
        """The steps of one log as dicts keyed by FIELDS, reading only what was added since last time."""
        key = str(log)
        self.touched.add(key)
        sig = stat_sig(log)
        if sig is None:
            return []
        e = self.files.get(key)
        ok = (isinstance(e, dict) and isinstance(e.get("sig"), list) and len(e["sig"]) == 3
              and isinstance(e.get("rows"), list) and isinstance(e.get("ids"), list))
        if ok and e["sig"] == list(sig):
            return [dict(zip(FIELDS, r)) for r in e["rows"]]
        grew = ok and e["sig"][0] == sig[0] and sig[1] >= e["sig"][1] and sig[1] >= int(e.get("offset") or 0)
        if not grew:
            e = {"sig": None, "offset": 0, "ids": [], "rows": []}
        seen = set(e["ids"])
        fresh: list = []
        try:
            e["offset"], _ = read_lines_from(log, int(e["offset"]), lambda lines: fresh.extend(feed(lines, seen)),
                                             final=False)
        except OSError:
            return [dict(zip(FIELDS, r)) for r in e["rows"]]
        e["rows"] += [row for _id, row in fresh]
        e["ids"] = (e["ids"] + [i for i, _row in fresh])[-KEEP_IDS:]
        e["sig"] = list(sig)
        self.files[key] = e
        self.dirty = True
        return [dict(zip(FIELDS, r)) for r in e["rows"]]

    def save(self) -> bool:
        """Write the cache if it changed, keeping only the logs asked for in this run. Never raises."""
        if self.path is None or not self.dirty:
            return False
        keep = {k: v for k, v in self.files.items() if k in self.touched}
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps({"v": VERSION, "files": keep}, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            return False
        self.dirty = False
        return True
