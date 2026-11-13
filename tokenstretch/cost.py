"""Where recent sessions' credits went, per model and reasoning effort, weighted by price.

Token counts alone hide the price: an output token costs several times an input token, a re-read
from the cache costs a fraction of one, and the top model multiplies all of it. This module weighs
each kind of token by published list-price ratios, so a session on the top model at the highest
effort can be compared with one on the mid model at a low effort.

Everything here is an estimate from local logs and public price ratios. It is not a bill, and a
subscription plan counts usage its own way.
"""
from __future__ import annotations

import re

from .identity import CLI_NAME

PRICES_READ = "2026-11"

# One unit = one new (uncached) input token on the mid model at list price.
OUTPUT_X = 5.0          # an output token costs this many input tokens on the same model
CACHE_WRITE_X = 1.25    # saving history to the cache (short lifetime; the one-hour kind costs 2x)

# (model-name pattern, input price relative to the mid model, cache re-read as a share of input).
# First match wins, so the specific versions come first. Models that match nothing are not priced.
_RATES: tuple[tuple[str, float, float], ...] = (
    (r"(fable|mythos)-5[-.]2", 4.0, 0.025),
    (r"(fable|mythos)-5[-.]1", 5.0, 0.025),
    (r"(fable|mythos)-5", 5.0, 0.1),
    (r"opus-5[-.]5", 2.0, 0.05),
    (r"opus-(5|4[-.][5-8])", 2.5, 0.1),
    (r"sonnet-5[-.]5", 1.0, 0.05),
    (r"sonnet-5", 1.0, 0.1),
    (r"sonnet-4[-.][5-6]", 1.5, 0.1),
    (r"haiku-5[-.]5", 0.05, 0.1),
    (r"haiku-4[-.]5", 0.5, 0.1),
)
_COMPILED = tuple((re.compile(p, re.I), i, r) for p, i, r in _RATES)

MID_INPUT, MID_READ = 1.0, 0.05     # the current mid model, the yardstick for "the same tokens on mid"

# "new" is content seen for the first time (files read, command output, prompts), most of it saved
# to the cache as it arrives. "resave" is old history saved a second time after the cache lapsed.
BUCKETS = ("reread", "resave", "new", "output")
_BUCKET_LABEL = {
    "reread": "history re-read",
    "resave": "history saved again",
    "new": "new content",
    "output": "output and thinking",
}
RESAVE_MIN = 20_000     # a later step that saves this much, and more than it re-reads, lost its cache
_HOT_EFFORTS = ("high", "xhigh", "max")


def rate(model: str) -> tuple[float, float] | None:
    """(input price relative to mid, cache re-read share of input) for a model name, or None."""
    for pat, inp, read in _COMPILED:
        if pat.search(model or ""):
            return inp, read
    return None


def reread_weight(model: str) -> float | None:
    """What re-reading one token of history costs on this model, relative to the mid model."""
    r = rate(model)
    return None if r is None else (r[0] * r[1]) / (MID_INPUT * MID_READ)


def startup_price(start: int, model: str, steps: int = 50) -> dict | None:
    """What re-reading a start-up block of `start` tokens costs over `steps` steps on one model."""
    r = rate(model)
    if r is None or not start:
        return None
    return {"model": model, "steps": steps, "units": start * steps * r[0] * r[1],
            "weight": reread_weight(model)}


def resaved(turns: list[dict]) -> int:
    """Tokens of old history saved to the cache a second time: later steps that save more than they re-read."""
    total = 0
    for t in turns[1:]:
        write, read = int(t.get("cache_write") or 0), int(t.get("cache_read") or 0)
        if write >= RESAVE_MIN and write > read:
            total += write
    return total


def units(model: str, *, new: int = 0, reread: int = 0, write: int = 0, resave: int = 0,
          output: int = 0) -> dict | None:
    """Price-weighted units per kind of token for one model, or None when the model is not priced.

    `new` is input that was not cached at all, `write` is new content saved to the cache as it
    arrived, `resave` is old history saved again. The first two are reported together as "new".
    """
    r = rate(model)
    if r is None:
        return None
    inp, read = r
    return {"reread": reread * inp * read, "resave": resave * inp * CACHE_WRITE_X,
            "new": new * inp + write * inp * CACHE_WRITE_X, "output": output * inp * OUTPUT_X}


def _on_mid(new: int, reread: int, write: int, resave: int, output: int) -> float:
    return (reread * MID_INPUT * MID_READ + (write + resave) * MID_INPUT * CACHE_WRITE_X
            + new * MID_INPUT + output * MID_INPUT * OUTPUT_X)


def _blank(model: str, effort: str, kind: str) -> dict:
    return {"model": model, "effort": effort, "kind": kind, "steps": 0,
            "tokens": {b: 0 for b in BUCKETS}, "units": {b: 0.0 for b in BUCKETS},
            "total": 0.0, "on_mid": 0.0, "priced": True}


def _add(row: dict, model: str, steps: int, sent: int, reread: int, saved: int, resave: int, output: int) -> None:
    resave = min(resave, saved)
    write = saved - resave
    new = max(0, sent - reread - saved)
    row["steps"] += steps
    for b, n in zip(BUCKETS, (reread, resave, new + write, output)):
        row["tokens"][b] += n
    u = units(model, new=new, reread=reread, write=write, resave=resave, output=output)
    if u is None:
        row["priced"] = False
        return
    for b in BUCKETS:
        row["units"][b] += u[b]
    row["total"] += sum(u.values())
    row["on_mid"] += _on_mid(new, reread, write, resave, output)


def cost_view(sessions: list[dict]) -> dict:
    """Rows of price-weighted units, one per (model, effort) for main conversations and for helpers."""
    rows: dict[tuple, dict] = {}
    for s in sessions:
        for i, t in enumerate(s.get("turns") or []):
            model, effort = t.get("model") or "", t.get("effort") or ""
            read, write = int(t.get("cache_read") or 0), int(t.get("cache_write") or 0)
            again = write if (i and write >= RESAVE_MIN and write > read) else 0
            row = rows.setdefault((model, effort, "main"), _blank(model, effort, "main"))
            _add(row, model, 1, int(t.get("history_tokens") or 0), read, write, again,
                 int(t.get("output_tokens") or 0))
        for h in s.get("helpers") or []:
            model, effort = h.get("model") or "", h.get("effort") or ""
            row = rows.setdefault((model, effort, "helper"), _blank(model, effort, "helper"))
            _add(row, model, int(h.get("steps") or 0), int(h.get("sent") or 0), int(h.get("cache_read") or 0),
                 int(h.get("cache_write") or 0), int(h.get("resaved") or 0), int(h.get("output") or 0))
    out = sorted(rows.values(), key=lambda r: (-r["total"], -r["steps"]))
    grand = sum(r["total"] for r in out)
    for r in out:
        r["share"] = (r["total"] / grand) if grand else 0.0
        r["biggest"] = max(BUCKETS, key=lambda b: r["units"][b]) if r["total"] else ""
        r["lever"] = _lever(r)
    return {"read": len(sessions), "total": grand, "rows": out, "prices_read": PRICES_READ}


def _lever(r: dict) -> str:
    """The one change that removes the most from this row, from its largest kind of token."""
    if not r["total"]:
        return ""
    helper = r["kind"] == "helper"
    b = r["biggest"]
    if b == "reread":
        return ("each helper re-reads its own history on every step: give a helper one narrow task and "
                f"a step limit (`{CLI_NAME} brief` writes that prompt)." if helper else
                "the conversation is long and every step re-reads it: start fresh at the next task break "
                f"(`{CLI_NAME} watch` says when).")
    if b == "resave":
        return ("old history was saved to the cache a second time: a pause longer than the cache lifetime, or "
                "a model switch mid-conversation, re-saves all of it at a premium. Finish a task in one "
                "sitting, and switch model at a fresh start.")
    if b == "output":
        if r["effort"] in _HOT_EFFORTS:
            return (f"thinking is billed as output, and this ran at {r['effort']}: step the effort down for "
                    f"routine steps (`{CLI_NAME} model suggest \"<task>\"` suggests one per task).")
        return ("replies are the largest part: ask for shorter replies"
                + (f" (`{CLI_NAME} brief` sets a reply limit for a helper)." if helper else
                   ", and for changed lines, not whole files."))
    return ("new content on each step (files read, command output) is the largest part: search first, read "
            "line ranges, and keep command output short.")


def _n(x: float) -> str:
    if x >= 1_000_000:
        return f"{x / 1_000_000:.1f}M"
    if x >= 1_000:
        return f"{x / 1_000:.0f}k"
    return f"{x:.0f}"


def format_cost_view(view: dict) -> list[str]:
    rows = view["rows"]
    if not rows:
        return [f"No token counts found in the {view['read']} newest conversations."]
    lines = [f"Where the credits went in the {view['read']} newest conversations (local logs, read-only).",
             "1 unit = one new input token on the mid model at list price. An estimate from published "
             f"price ratios (read {view['prices_read']}), not a bill."]
    for r in rows:
        who = "helpers" if r["kind"] == "helper" else "main conversation"
        effort = r["effort"].upper() if r["effort"] else "effort not logged"
        head = f"{r['model'] or 'unknown model'}, {effort}, {who}: {r['steps']} steps"
        if not r["priced"]:
            t = r["tokens"]
            lines += ["", head + " (no price ratio for this model, token counts only)",
                      f"    {_n(t['reread'])} re-read, {_n(t['resave'])} saved again, {_n(t['new'])} new content, "
                      f"{_n(t['output'])} output"]
            continue
        lines += ["", head + f", ~{_n(r['total'])} units ({round(100 * r['share'])}% of all)"]
        for b in BUCKETS:
            if r["tokens"][b]:
                pct = round(100 * r["units"][b] / r["total"]) if r["total"] else 0
                lines.append(f"    {_BUCKET_LABEL[b]:<20}{pct:>3}%  ({_n(r['tokens'][b])} tokens)")
        if r["on_mid"] and r["total"] >= r["on_mid"] * 1.5:
            lines.append(f"    the same tokens on the mid model: ~{_n(r['on_mid'])} units "
                         f"({r['total'] / r['on_mid']:.1f}x less). A smaller model may need more steps, "
                         "so try it on routine work first.")
        if r["lever"]:
            lines.append(f"    biggest lever: {r['lever']}")
    return lines


def grid(view: dict) -> list[dict]:
    """One line per (model, effort) for main conversations: what a step cost on average, dearest first."""
    out = []
    for r in view["rows"]:
        if r["kind"] != "main" or not r["steps"]:
            continue
        out.append({"model": r["model"], "effort": r["effort"], "steps": r["steps"], "priced": r["priced"],
                    "units_per_step": (r["total"] / r["steps"]) if r["priced"] else None,
                    "output_per_step": r["tokens"]["output"] / r["steps"],
                    "reread_per_step": r["tokens"]["reread"] / r["steps"]})
    out.sort(key=lambda g: -(g["units_per_step"] or 0))
    base = min((g["units_per_step"] for g in out if g["units_per_step"]), default=None)
    for g in out:
        g["times_cheapest"] = (g["units_per_step"] / base) if (base and g["units_per_step"]) else None
    return out


def format_grid(rows: list[dict]) -> list[str]:
    if not rows:
        return ["No main-conversation steps with token counts were found."]
    lines = ["What one step cost on average, per model and effort, in your own recent conversations.",
             "Units are price-weighted (1 unit = one new input token on the mid model). An estimate, not a bill.",
             "A dearer row is not waste by itself: compare rows that did the same kind of work.", "",
             f"{'model':<28}{'effort':<10}{'steps':>6}{'units/step':>12}{'output/step':>13}{'re-read/step':>14}  vs cheapest"]
    for g in rows:
        units = _n(g["units_per_step"]) if g["units_per_step"] is not None else "no price"
        times = f"{g['times_cheapest']:.1f}x" if g["times_cheapest"] else ""
        lines.append(f"{(g['model'] or 'unknown')[:27]:<28}{(g['effort'] or 'not logged'):<10}{g['steps']:>6}"
                     f"{units:>12}{_n(g['output_per_step']):>13}{_n(g['reread_per_step']):>14}  {times}")
    return lines
