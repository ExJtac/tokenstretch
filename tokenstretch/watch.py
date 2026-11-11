"""`tokenstretch watch`: a read-only loop over local session logs that warns about credit waste.

Core only: sessions, per-turn records, and model names come from adapters (`watch_sessions`,
`is_top_model`, `model_tiers`, `model_switch_hint`). Nothing here touches the network. The
optional `--notify` runs the OS's own local notifier (osascript / notify-send) if present.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .context import Context
from .identity import CLI_NAME, PRODUCT_NAME


@dataclass
class WatchConfig:
    max_context: int = 150_000      # (a) history re-sent per step
    simple_turns: int = 3           # (b) this many recent top-model steps ...
    simple_output: int = 400        #     ... each with output under this ...
    simple_tools: int = 1           #     ... and at most this many tool calls
    max_reads: int = 3              # (c) same file read this many times
    growth_tokens: int = 60_000     # (d) history growth ...
    growth_window_s: int = 300      #     ... within this window
    active_s: int = 2 * 3600        # only sessions written within this window


@dataclass
class WatchWarning:
    key: tuple
    adapter: str
    session: str
    project: str
    what: str
    why: str
    fix: str

    def lines(self) -> list[str]:
        return [f"⚠ [{self.adapter} {self.session} · {self.project[-40:]}] {self.what}",
                f"   Why it costs credits: {self.why}",
                f"   Fix: {self.fix}"]


def _k(n: int) -> str:
    return f"{round(n / 1000)}K"


def _epoch(ts) -> float | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _short(path: str, home: Path) -> str:
    try:
        return "~/" + str(Path(path).relative_to(home))
    except ValueError:
        return path


def session_warnings(ctx: Context, ad, s: dict, cfg: WatchConfig) -> list[WatchWarning]:
    """Every waste condition one session shows right now (deduping happens in Watcher)."""
    turns = s.get("turns") or []
    if not turns:
        return []
    out: list[WatchWarning] = []
    base = (s["adapter"], s["path"])
    tag = dict(adapter=s["adapter"], session=s["session"], project=s.get("project", ""))
    last = turns[-1]
    clear = s.get("clear_hint", "start a fresh session")
    compact = s.get("compact_hint")

    # (a) long history
    if last["history_tokens"] > cfg.max_context:
        alt = f" or {compact} (keeps a short summary)" if compact else ""
        out.append(WatchWarning(base + ("context",), **tag,
                           what=f"history is ~{_k(last['history_tokens'])} tokens (over {_k(cfg.max_context)}).",
                           why="every step re-sends the whole conversation, so each reply now costs at least that "
                               "much input.",
                           fix=f"finish this step, then type {clear} (fresh start){alt}."))

    # (b) top model on simple steps
    recent = turns[-cfg.simple_turns:]
    if len(recent) >= cfg.simple_turns and all(ad.is_top_model(t["model"]) for t in recent) and all(
            t["output_tokens"] < cfg.simple_output and (t["tool_calls"] is None or t["tool_calls"] <= cfg.simple_tools)
            for t in recent):
        mid = ad.model_tiers().get("mid")
        switch = ad.model_switch_hint(mid) if mid else ad.model_switch_hint("")
        fix = (f"type `{switch}` for routine steps; switch back for hard design or debugging."
               if mid else f"use `{switch}` to pick a smaller model for routine steps.")
        out.append(WatchWarning(base + ("top-model",), **tag,
                           what=f"the top model ({recent[-1]['model']}) ran the last {len(recent)} steps, "
                                "and they look simple (short replies, few tool calls).",
                           why="the top model costs several times more per step than the mid tier for the same work.",
                           fix=fix + f" `{CLI_NAME} model suggest \"<task>\"` helps pick."))

    # (c) the same file read again and again (paths only)
    counts: dict[str, int] = {}
    for t in turns:
        for p in t.get("reads") or []:
            counts[p] = counts.get(p, 0) + 1
    hot = sorted(((n, p) for p, n in counts.items() if n >= cfg.max_reads), reverse=True)
    if hot:
        names = ", ".join(f"{_short(p, ctx.home)} ×{n}" for n, p in hot[:3])
        more = f" and {len(hot) - 3} more" if len(hot) > 3 else ""
        out.append(WatchWarning(base + ("rereads",), **tag,
                           what=f"the same file was read {hot[0][0]}+ times: {names}{more}.",
                           why="each re-read adds the file to the history again, and the history is re-sent on "
                               "every later step.",
                           fix=f"ask for line ranges or a short note instead of full re-reads; {clear} also clears them."))

    # (d) fast growth, measured against the session's own newest timestamp
    end = _epoch(last.get("ts"))
    if end is not None:
        window = [t for t in turns if (e := _epoch(t.get("ts"))) is not None and end - e <= cfg.growth_window_s]
        if len(window) >= 2:
            grew = window[-1]["history_tokens"] - window[0]["history_tokens"]
            if grew >= cfg.growth_tokens:
                mins = max(1, round((end - _epoch(window[0]["ts"])) / 60))
                out.append(WatchWarning(base + ("growth",), **tag,
                                   what=f"history grew ~{_k(grew)} tokens in {mins} min ({len(window)} steps).",
                                   why="fast growth usually means whole-file reads or long command output, and "
                                       "every added token is re-sent on each later step.",
                                   fix="search first and read line ranges, trim noisy output, and start fresh "
                                       "at the next task break."))
    return out


def _notify(title: str, message: str) -> None:
    """Local desktop notification only, if the OS tool exists. Never raises."""
    try:
        if sys.platform == "darwin" and shutil.which("osascript"):
            argv = ["osascript", "-e", f"display notification {json.dumps(message)} with title {json.dumps(title)}"]
        elif shutil.which("notify-send"):
            argv = ["notify-send", title, message]
        else:
            return
        subprocess.run(argv, check=False, capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return


class Watcher:
    """Remembers what it already said, so each condition is reported once per session."""

    def __init__(self, ctx: Context, cfg: WatchConfig | None = None, adapters=None,
                 now: Callable[[], float] = time.time, notifier: Callable[[str, str], None] | None = None):
        from .setup import detected_adapters
        self.ctx = ctx
        self.cfg = cfg or WatchConfig()
        self.adapters = adapters if adapters is not None else detected_adapters(ctx)
        self.now = now
        self.notifier = notifier
        self.seen: set[tuple] = set()

    def coverage(self) -> tuple[list[str], list[str]]:
        """(watched tool names, detected but not supported yet)."""
        yes, no = [], []
        for ad in self.adapters:
            if ad.id == "host":
                continue
            (yes if ad.watch_sessions(self.ctx, limit=0) is not None else no).append(ad.display_name)
        return yes, no

    def scan(self) -> list[WatchWarning]:
        new: list[WatchWarning] = []
        cutoff = self.now() - self.cfg.active_s
        for ad in self.adapters:
            for s in ad.watch_sessions(self.ctx) or []:
                if (s.get("last_write") or 0) < cutoff:
                    continue
                for w in session_warnings(self.ctx, ad, s, self.cfg):
                    if w.key in self.seen:
                        continue
                    self.seen.add(w.key)
                    new.append(w)
                    if self.notifier:
                        self.notifier(f"{PRODUCT_NAME}: {w.adapter} {w.session}", f"{w.what} Fix: {w.fix}")
        return new


def run_watch(ctx: Context, cfg: WatchConfig | None = None, *, interval: float = 10.0, once: bool = False,
              notify: bool = False, out: Callable[[str], None] = print, sleep: Callable[[float], None] = time.sleep,
              adapters=None) -> int:
    w = Watcher(ctx, cfg, adapters=adapters, notifier=_notify if notify else None)
    yes, no = w.coverage()
    every = "one pass" if once else f"every {interval:g}s; Ctrl-C to stop"
    out(f"{PRODUCT_NAME} watch ({every}). Read-only: local session logs only, nothing leaves this machine.")
    out("Watching: " + (", ".join(yes) or "no supported tool detected"))
    for name in no:
        out(f"{name}: live watch not supported yet.")
    total = 0
    try:
        while True:
            for warning in w.scan():
                total += 1
                out("")
                for line in warning.lines():
                    out(line)
            if once:
                break
            sleep(interval)
    except KeyboardInterrupt:
        out("")
        out(f"Stopped watching. {total} warning(s) shown.")
        return 0
    if total == 0:
        out("No waste spotted in active sessions.")
    return 0
