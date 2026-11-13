"""Store plan readings for `pace` from the limits a tool hands to its status line.

Claude Code gives a subscription plan's 5-hour and 7-day limits (percent used, reset time) to the
status line command on stdin. With `pace auto` switched on, the status line stores them as readings,
so the pace gate stays current with nobody typing a number. Off by default: the status line writes
nothing until the user turns this on. Only the percent and the reset time are stored.

The tool reports no per-model weekly limit here, so a top-model window still needs `pace set`.
"""
from __future__ import annotations

from . import pace as pc

AUTO_KEY = "auto"
WINDOWS = (("five_hour", "auto-5h", 5 * 3600), ("seven_day", "auto-week", 7 * 86400))
MIN_STEP = 1.0          # store a new reading when the percent rose by this many points
MIN_GAP_S = 1800        # or when this long has passed since the last one


def is_on(state: dict) -> bool:
    return state.get(AUTO_KEY) is True


def readings(payload: dict) -> list[tuple[str, int, float, float]]:
    """(window name, length, percent used, reset time) for each well-formed limit in the payload."""
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    out = []
    for key, name, length in WINDOWS:
        w = limits.get(key) if isinstance(limits, dict) else None
        if not isinstance(w, dict):
            continue
        used, resets = w.get("used_percentage"), w.get("resets_at")
        ok = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (used, resets))
        if ok and 0 <= used <= 100 and resets > 0:
            out.append((name, length, float(used), float(resets)))
    return out


def _due(w: dict | None, used: float, resets: float, now: float) -> bool:
    if not w or not w.get("readings"):
        return True
    if abs(resets - pc.parse_time(w["resets"])) >= pc.SAME_PERIOD_S:
        return True
    last = w["readings"][-1]
    return abs(used - last["used"]) >= MIN_STEP or now - pc.parse_time(last["at"]) >= MIN_GAP_S


def record(home, payload: dict, now: float) -> list[str]:
    """Store what the payload reports, when auto is on and a reading is due. Returns the windows written."""
    found = readings(payload)
    if not found:
        return []
    state = pc.load_state(home)
    if not is_on(state):
        return []
    wrote = []
    for name, length, used, resets in found:
        if resets <= now or not _due(state["windows"].get(name), used, resets, now):
            continue
        pc.set_reading(state, name, used, resets, now, length_s=length)
        wrote.append(name)
    if wrote:
        pc.save_state(home, state)
    return wrote
