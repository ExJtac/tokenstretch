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



def _short_project(name: str, width: int = 40) -> str:
    """Readable project label: drop the home prefix (encoded `-Users-me-` or a real `/Users/me/` path),
    then keep the tail, cut on a '/' (paths) or '-' (encoded names) boundary."""
    import re
    name = name or ""
    if name.startswith("/"):
        name = re.sub(r"^/(Users|home)/[^/]+(?=/|$)", "~", name)
        sep = "/"
    else:
        name = re.sub(r"^-(Users|home)-[^-]+-", "~-", name)
        sep = "/" if name.startswith("~/") else "-"
    if len(name) <= width:
        return name
    tail = name[-width:]
    cut = tail.find(sep)
    return "…" + (tail[cut + 1:] if 0 <= cut < width - 8 else tail)

@dataclass
class WatchConfig:
    max_context: int = 150_000      # (a) history re-sent per step
    simple_turns: int = 3           # (b) this many recent top-model steps ...
    simple_output: int = 400        #     ... each with output under this ...
    simple_tools: int = 1           #     ... and at most this many tool calls
    max_reads: int = 3              # (c) same file read this many times
    growth_tokens: int = 60_000     # (d) history growth ...
    growth_window_s: int = 300      #     ... within this window
    cache_min_gap_s: int = 300      # (e) a pause at least this long (the shortest cache lifetime) ...
    cache_min_history: int = 20_000  #     ... in a session with at least this much history ...
    cache_miss_share: float = 0.5   #     ... where the next step re-wrote this share of it
    max_start: int = 40_000         # (f) history on a session's first step, before any work
    helper_min_sent: int = 1_000_000  # (g) helpers re-sent at least this much in total ...
    helper_ratio: float = 1.0       #     ... and at least this many times the main conversation
    wake_min: int = 3               # (h) finished helpers woke the conversation at least this often ...
    wake_min_sent: int = 300_000    #     ... and those extra steps re-sent at least this much
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
    short: str = ""                 # one-line form for `statusline`

    def lines(self) -> list[str]:
        return [f"⚠ [{self.adapter} {self.session} · {_short_project(self.project)}] {self.what}",
                f"   Why it costs credits: {self.why}",
                f"   Fix: {self.fix}"]


def _k(n: int) -> str:
    return f"{round(n / 1000)}K"


def _m(n: int) -> str:
    return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else _k(n)


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


def cache_misses(turns: list[dict], cfg: WatchConfig) -> list[tuple[float, int]]:
    """[(idle seconds, tokens re-written)] for each step where the logs show the cache had expired.

    Read from the tool's own counts, not guessed: after a pause, the same model re-wrote most of
    the history to the cache and read almost none of it back. A compaction (history shrank) or a
    model switch is not counted; those rebuild the cache for a different reason.
    """
    out = []
    for prev, cur in zip(turns, turns[1:]):
        if "cache_read" not in cur or "cache_write" not in cur or cur["model"] != prev["model"]:
            continue
        a, b = _epoch(prev.get("ts")), _epoch(cur.get("ts"))
        if a is None or b is None or b - a < cfg.cache_min_gap_s:
            continue
        hist = cur["history_tokens"]
        if hist < cfg.cache_min_history or hist < prev["history_tokens"] * 0.5:
            continue
        if cur["cache_write"] >= hist * cfg.cache_miss_share and cur["cache_read"] <= hist * 0.2:
            out.append((b - a, cur["cache_write"]))
    return out


AUTO_COMPACT_MARGIN = 1.15   # automatic compaction fires near its window, not exactly at it


def long_history_tip(history: int, limit: int, clear: str = "", auto: int | None = None) -> str:
    """The one-line tip for a session list, or "" when the session is not long.

    With automatic summaries on (`auto`), a session is only long once it is well past that size,
    and the tip is then to summarize once, never to clear by hand.
    """
    if auto:
        return f"over {_k(auto)}: /compact once (automatic summary did not run)" if history > auto * AUTO_COMPACT_MARGIN else ""
    return f"over {_k(limit)}: {clear or 'start fresh'} at the next task break" if history > limit else ""


def payback_steps(history: int, start: int | None) -> int | None:
    """About how many more steps until a fresh start has paid for itself, or None when unknown.

    A fresh start costs roughly two steps at the current size (writing the note, then the reply that
    ends the session). After it, each step is smaller by (history - start), because the start-up
    size comes back in every session. A rule of thumb from token counts, not a price calculation.
    """
    if not start or history <= start:
        return None
    return max(1, -(-2 * history // (history - start)))


def session_warnings(ctx: Context, ad, s: dict, cfg: WatchConfig, now: float | None = None) -> list[WatchWarning]:
    """Every waste condition one session shows right now (deduping happens in Watcher).

    `now` turns on the one check that needs the clock: a session sitting idle past its cache
    lifetime. Without it (weekly report, old sessions) only what the logs record is reported.
    """
    turns = s.get("turns") or []
    if not turns:
        return []
    out: list[WatchWarning] = []
    base = (s["adapter"], s["path"])
    tag = dict(adapter=s["adapter"], session=s["session"], project=s.get("project", ""))
    last = turns[-1]
    clear = s.get("clear_hint", "start a fresh session")
    compact = s.get("compact_hint")
    handoff = s.get("handoff_hint")     # a note-then-clear shortcut, when the tool has one installed
    fresh = f"{handoff} then {clear}" if handoff else clear

    # (a) long history. When the tool summarizes by itself at a known size, say nothing below
    # that size (plus a margin): advice to clear by hand would contradict the automatic step.
    # Well past it, the session predates the setting or a compaction failed: one thing to type.
    auto = s.get("auto_compact")
    if auto and compact and last["history_tokens"] > cfg.max_context:
        if last["history_tokens"] > auto * AUTO_COMPACT_MARGIN:
            out.append(WatchWarning(base + ("context",), **tag,
                               what=f"history is ~{_k(last['history_tokens'])} tokens, past the ~{_k(auto)} where "
                                    "it should have been summarized automatically.",
                               why="this session probably started before automatic summaries were set up, so "
                                   "every step still re-sends the whole conversation.",
                               fix=f"type {compact} once; sessions started from now on do this by themselves.",
                               short=f"{compact} once (automatic summary did not run)"))
    elif last["history_tokens"] > cfg.max_context:
        alt = f" or {compact} (keeps a short summary)" if compact else ""
        back = payback_steps(last["history_tokens"], s.get("start_tokens"))
        pays = (f" A fresh start pays for itself after about {back} more step{'s' if back != 1 else ''} "
                f"(this session started at ~{_k(s['start_tokens'])}, and that part comes back)." if back else "")
        out.append(WatchWarning(base + ("context",), **tag,
                           what=f"history is ~{_k(last['history_tokens'])} tokens (over {_k(cfg.max_context)}).",
                           why="every step re-sends the whole conversation, so each reply now costs at least that "
                               "much input.",
                           fix=(f"finish this step, then type {handoff} (writes a short note) and {clear}; "
                                f"`{handoff} resume` picks the task up in the fresh session."
                                if handoff else f"finish this step, then type {clear} (fresh start){alt}.") + pays,
                           short=f"{fresh} at the next task break" + (f" (pays back in ~{back} steps)" if back else "")))

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
                           fix=fix + f" `{CLI_NAME} model suggest \"<task>\"` helps pick.",
                           short=f"simple steps on the top model: {switch}"))

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
                           fix=f"ask for line ranges or a short note instead of full re-reads; {clear} also clears them.",
                           short=f"same file read {hot[0][0]}+ times"))

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
                                       "at the next task break.",
                                   short=f"history +{_k(grew)} in {mins} min: read narrower"))

    # (g) helpers (subagents) that re-sent more than the main conversation
    helpers = s.get("helpers") or []
    sent = sum(h["sent"] for h in helpers)
    main_sent = sum(t["history_tokens"] for t in turns)
    if helpers and sent >= cfg.helper_min_sent and sent >= main_sent * cfg.helper_ratio:
        top = max(helpers, key=lambda h: h["sent"])
        times = f"{sent / main_sent:.1f}x" if main_sent else "more than"
        n = len(helpers)
        out.append(WatchWarning(base + ("helpers",), **tag,
                           what=f"{n} helper{'s' if n != 1 else ''} in this session re-sent ~{_m(sent)} tokens, "
                                f"{times} the main conversation's ~{_m(main_sent)}. The longest ran "
                                f"{top['steps']} steps and reached ~{_k(top['peak'])}.",
                           why="each helper is its own conversation: it starts with its own overhead and re-sends "
                               "its whole history on every step, so a long helper costs like a long session.",
                           fix="give each helper one narrow task, name the files or notes to work from, say what "
                               "not to look up again, and ask for a short reply. Split a long job into two helpers.",
                           short=f"helpers re-sent ~{_m(sent)} ({times} main): narrower helper tasks"))

    # (h) each background helper that finishes starts a new step here, at the full history size
    wakes = [t for t in turns if t.get("woken")]
    wake_sent = sum(t["history_tokens"] for t in wakes)
    if len(wakes) >= cfg.wake_min and wake_sent >= cfg.wake_min_sent:
        out.append(WatchWarning(base + ("wakeups",), **tag,
                           what=f"helpers finishing woke this conversation {len(wakes)} times, and those "
                                f"{len(wakes)} extra steps re-sent ~{_m(wake_sent)} tokens "
                                f"(the largest ~{_k(max(t['history_tokens'] for t in wakes))}).",
                           why="a helper that finishes in the background starts a new step here, and every step "
                               "re-sends the whole conversation, even when the step only says it is still waiting.",
                           fix="start helpers while this conversation is still short, start the ones that belong "
                               "together in one message, and run them in the foreground where the tool allows it, so "
                               "their results arrive in one step. If this conversation reports to another one, send "
                               "one final report, not one per helper.",
                           short=f"helpers woke this chat {len(wakes)}x (~{_m(wake_sent)} re-sent): start them early, "
                                 "together"))

    # (f) heavy start: what the session carried on its first step, before any work
    start = s.get("start_tokens")
    if start and start > cfg.max_start:
        out.append(WatchWarning(base + ("startup",), **tag,
                           what=f"this session started at ~{_k(start)} tokens before doing any work "
                                f"(over {_k(cfg.max_start)}).",
                           why="that is instruction files, tool and connector definitions, and skill listings. "
                               "It is in every session and is re-sent on every step, so a fresh start does not "
                               "remove it.",
                           fix=f"run `{CLI_NAME} startup` to see what it is made of, largest first, with the fix for each "
                               "part, then turn off what you don't use.",
                           short=f"starts at {_k(start)} before any work: {CLI_NAME} startup"))

    # (e) the cache expired during a pause: idle right now (needs the clock), or recorded in the logs
    ttl = ad.cache_ttl_s(ctx)
    idle = now - (s.get("last_write") or now) if now is not None else 0
    misses = cache_misses(turns, cfg)
    if ttl and idle >= ttl and last["history_tokens"] >= cfg.cache_min_history and "cache_read" in last:
        mins = round(idle / 60)
        note = f" {compact} would re-read the whole history first, so it doesn't help now." if compact else ""
        out.append(WatchWarning(base + ("cache",), **tag,
                           what=f"idle for {mins} min with ~{_k(last['history_tokens'])} tokens of history; the "
                                f"saved-context discount has probably expired (it lasts about {round(ttl / 60)} min "
                                "without activity).",
                           why="once it expires, your next message re-sends the whole conversation at full "
                               "price instead of the discounted re-read.",
                           fix=f"if that task is done, type {clear} before your next message.{note}",
                           short=f"idle {mins} min, cache probably expired: {clear} if the task is done"))
    elif misses:
        longest = round(max(g for g, _ in misses) / 60)
        total = sum(n for _, n in misses)
        times = "once" if len(misses) == 1 else f"{len(misses)} times"
        out.append(WatchWarning(base + ("cache",), **tag,
                           what=f"the saved-context discount expired {times} during a pause (longest ~{longest} "
                                f"min), and ~{_k(total)} tokens were re-sent at full price.",
                           why="after a pause longer than the cache lifetime, the next message re-pays for the "
                               "whole conversation instead of the discounted re-read.",
                           fix=f"before a break, finish the task and type {fresh}, so you come back to a small "
                               "history. Coming back to a big idle session? Start fresh instead of continuing it.",
                           short=f"cache expired after ~{longest} min idle (~{_k(total)} re-sent): {fresh} before breaks"))
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
            if ad.watch_sessions(self.ctx, limit=0) is not None:
                yes.append(ad.display_name)
            else:
                hint = getattr(ad, "watch_hint", "")
                no.append(f"{ad.display_name} ({hint})" if hint else ad.display_name)
        return yes, no

    def scan(self) -> list[WatchWarning]:
        new: list[WatchWarning] = []
        cutoff = self.now() - self.cfg.active_s
        for ad in self.adapters:
            for s in ad.watch_sessions(self.ctx, max_age_s=self.cfg.active_s) or []:
                if (s.get("last_write") or 0) < cutoff:
                    continue
                for w in session_warnings(self.ctx, ad, s, self.cfg, now=self.now()):
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
