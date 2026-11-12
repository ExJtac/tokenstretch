"""`tokenstretch startup`: what a session carries before you type anything.

Every session starts with instruction files, skill and helper listings, and tool and connector
definitions. That block is re-sent on every step, and a fresh start does not remove it. This
command lists the parts that can be sized from local files, largest first, next to the measured
size of a recent session's first step, and says what to do about each part.

Core only. Adapters supply the parts (`startup_parts`) and the sessions (`watch_sessions`). Sizes
are bytes / 4, so they are estimates. Read-only, no network, no model calls.
"""
from __future__ import annotations

from .context import Context
from .identity import CLI_NAME


def _k(n: int) -> str:
    return f"{n / 1000:.1f}K" if n < 10_000 else f"{round(n / 1000)}K"


def measured_starts(ctx: Context, ad, limit: int = 5) -> list[int]:
    """First-step sizes of recent sessions that recorded one, newest first."""
    try:
        sessions = ad.watch_sessions(ctx, limit=12, max_age_s=None) or []
    except Exception:  # noqa: BLE001 - a missing or odd log must not break the report
        return []
    return [int(s["start_tokens"]) for s in sessions if s.get("start_tokens")][:limit]


def build(ctx: Context, adapters=None) -> list[dict]:
    from .setup import detected_adapters
    out = []
    for ad in adapters if adapters is not None else detected_adapters(ctx):
        parts = ad.startup_parts(ctx)
        if parts is None:
            continue
        sized = sorted((p for p in parts if p.get("tokens")), key=lambda p: -p["tokens"])
        unsized = [p for p in parts if not p.get("tokens")]
        known = sum(p["tokens"] for p in sized)
        starts = measured_starts(ctx, ad)
        start = starts[0] if starts else None
        out.append({
            "adapter": ad.id, "name": ad.display_name, "measured_start": start, "recent_starts": starts, "known": known,
            "rest": max(0, start - known) if start else None, "parts": sized, "unsized": unsized,
        })
    return out


def format_report(data: list[dict], top: int = 12) -> list[str]:
    if not data:
        return ["No supported tool found, so there is nothing to size."]
    out: list[str] = []
    for d in data:
        out.append(f"{d['name']}: what every session carries before you type")
        if d["measured_start"]:
            out.append(f"  Measured: the newest session's first step was ~{_k(d['measured_start'])} tokens."
                       + (f" Recent sessions: {', '.join(_k(n) for n in d['recent_starts'])}."
                          if len(d["recent_starts"]) > 1 else ""))
        else:
            out.append("  Measured: no recent session with a clean first step was found.")
        out.append(f"  Sized from local files: ~{_k(d['known'])} tokens (approx., bytes / 4).")
        for p in d["parts"][:top]:
            out.append(f"    ~{_k(p['tokens']):>6}  {p['kind']}: {p['name']}")
            if p.get("fix"):
                out.append(f"             {p['fix']}")
        if len(d["parts"]) > top:
            more = d["parts"][top:]
            out.append(f"    ~{_k(sum(p['tokens'] for p in more)):>6}  {len(more)} smaller parts (--all lists them)")
        if d["rest"] is not None:
            out.append(f"  The rest, ~{_k(d['rest'])} tokens, can't be sized from local files: the tool's own "
                       "system prompt and built-in tools, connector tool lists, and plugin listings.")
            out.append("  To size one of those: turn it off, start a new session, and run this again. "
                       "The measured number shows the difference.")
        for p in d["unsized"]:
            out.append(f"    size unknown  {p['kind']}: {p['name']}" + (f". {p['fix']}" if p.get("fix") else ""))
        out.append("")
    out.append(f"This block is re-sent on every step. `{CLI_NAME} trim` shortens instruction files; "
               "the other fixes are listed with each part.")
    return out
