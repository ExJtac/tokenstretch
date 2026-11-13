"""Budget governor: keep a plan window on an even burn line, so a week of full-effort agents lasts the week.

A subscription plan shows percent used per window (5 hours, the week, the week for the top model
family) and a reset time. Local logs do not record that percent. So the user types in a reading
(`pace set`), and this module calibrates how many price-weighted units (see cost.py) one percent
is, from the logs since the window began. From there it estimates the percent used now, compares
it with the even-pace line (the share of the window that has passed), and works out how many
agents can run at once on that line.

Everything is an estimate from local logs, published price ratios and the user's own readings.
Units are not dollars and this is not a bill. A model with no price ratio is never guessed: it is
left out of the units and reported. Nothing here opens a network connection; state is one file,
`pace.json`, under the tokenstretch home.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .cost import BUCKETS, cost_view
from .identity import CLI_NAME, state_dir

STATE_FILE = "pace.json"
DEFAULT_RESERVE = 3.0           # percentage points kept back below the even-pace line
WATCH_POINTS = 10.0             # `watch` warns when a window is this many points over its line
MAX_READINGS = 20
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
IDLE_GAP_S = 600                # a pause longer than this is not working time
SAME_PERIOD_S = 3600            # a reset time that moves by less than this is the same period
HISTORY_CUT = {"resave": 1.0, "reread": 0.5}    # what-if: no re-saved history, re-read halved
NOT_A_BILL = "Estimates from local logs and list-price ratios, not a bill."


# ---- time ------------------------------------------------------------------------------------

def parse_time(text: str) -> float:
    """Epoch seconds from an ISO time. A time without an offset is read as local time."""
    t = str(text).strip().replace("Z", "+00:00").replace("z", "+00:00")
    dt = datetime.fromisoformat(t)
    return dt.timestamp() if dt.tzinfo else dt.astimezone().timestamp()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def parse_length(text: str) -> int:
    """Seconds from `7d`, `5h`, `90m`, `1d12h`."""
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*([dhm])", str(text).strip().lower())
    if not parts or "".join(f"{n}{u}" for n, u in parts) != re.sub(r"\s+", "", str(text).strip().lower()):
        raise ValueError(f"cannot read window length {text!r}; use 7d, 5h, 90m or 1d12h")
    secs = sum(float(n) * {"d": 86400, "h": 3600, "m": 60}[u] for n, u in parts)
    if secs <= 0:
        raise ValueError("window length must be above zero")
    return int(secs)


def default_length(name: str) -> int:
    return 5 * 3600 if re.match(r"(5\s*-?\s*h|session)", name.strip().lower()) else 7 * 86400


def dur(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m:02d}m"
    return f"{m}m" if m else f"{s}s"


# ---- stored readings ---------------------------------------------------------------------------

def state_path(home) -> Path:
    return state_dir(home) / STATE_FILE


def load_state(home) -> dict:
    try:
        data = json.loads(state_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"windows": {}}
    if not isinstance(data, dict) or not isinstance(data.get("windows"), dict):
        return {"windows": {}}
    return data


def save_state(home, state: dict) -> Path:
    p = state_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return p


def set_reading(state: dict, name: str, used: float, resets: float, now: float, *,
                length_s: int | None = None, models: str | None = None) -> str:
    """Store one reading. Returns what happened: "new", "added", "restarted" (new period) or "corrected"."""
    if not 0 <= used <= 100:
        raise ValueError("--used is a percent between 0 and 100")
    if models:
        re.compile(models)                      # fail early on a bad pattern
    w = state["windows"].get(name)
    length = length_s or (w or {}).get("length_s") or default_length(name)
    reading = {"used": float(used), "at": iso(now)}
    if w is None:
        state["windows"][name] = {"models": models or None, "length_s": length, "resets": iso(resets),
                                  "readings": [reading]}
        return "new"
    old_resets = parse_time(w["resets"])
    new_period = abs(resets - old_resets) >= SAME_PERIOD_S or length != w["length_s"]
    w.update(length_s=length, resets=iso(resets))
    if models is not None:
        w["models"] = models or None
    if new_period:
        w["readings"] = [reading]
        return "restarted"
    rs = w["readings"]
    if rs and used < rs[-1]["used"]:            # a lower number than before: the earlier ones are stale
        w["readings"] = [reading]
        return "corrected"
    rs.append(reading)
    del rs[:-MAX_READINGS]
    return "added"


# ---- units from sessions -----------------------------------------------------------------------

def _epoch(ts) -> float | None:
    if not ts:
        return None
    try:
        return parse_time(str(ts))
    except ValueError:
        return None


def _matches(pattern: re.Pattern | None, model: str) -> bool:
    return pattern is None or bool(pattern.search(model or ""))


def _slice(sessions: list[dict], lo: float, hi: float, pattern: re.Pattern | None) -> list[dict]:
    """Sessions cut to the steps logged in [lo, hi] on models that match. A session's helpers have no
    time of their own, so they count in the same share as the main steps that fell in range."""
    out = []
    for s in sessions:
        turns = s.get("turns") or []
        keep = [t for t in turns if (e := _epoch(t.get("ts"))) is not None and lo <= e <= hi
                and _matches(pattern, t.get("model") or "")]
        if not keep:
            continue
        share = len([t for t in turns if (e := _epoch(t.get("ts"))) is not None and lo <= e <= hi]) / len(turns)
        helpers = []
        for h in s.get("helpers") or []:
            if not _matches(pattern, h.get("model") or ""):
                continue
            helpers.append({**h, **{k: (h.get(k) or 0) * share for k in
                                    ("steps", "sent", "cache_read", "cache_write", "resaved", "output")}})
        out.append({"turns": keep, "helpers": helpers})
    return out


def slice_view(sessions: list[dict], lo: float, hi: float, pattern: re.Pattern | None = None) -> dict:
    """cost_view of the steps in [lo, hi], plus the priced total and the models left out for want of a price."""
    view = cost_view(_slice(sessions, lo, hi, pattern))
    unpriced: dict[str, int] = {}
    for r in view["rows"]:
        if not r["priced"]:
            unpriced[r["model"] or "unknown model"] = unpriced.get(r["model"] or "unknown model", 0) + int(round(r["steps"]))
    view["unpriced"] = unpriced
    view["buckets"] = {b: sum(r["units"][b] for r in view["rows"]) for b in BUCKETS}
    return view


# ---- one window --------------------------------------------------------------------------------

def _window_pattern(w: dict) -> re.Pattern | None:
    try:
        return re.compile(w["models"], re.I) if w.get("models") else None
    except re.error:
        return None


def evaluate_window(name: str, w: dict, sessions: list[dict], now: float, reserve: float = DEFAULT_RESERVE) -> dict:
    length, resets = int(w["length_s"]), parse_time(w["resets"])
    start = resets - length
    pattern = _window_pattern(w)
    readings = sorted(({"used": float(r["used"]), "at": parse_time(r["at"])} for r in w.get("readings") or []),
                      key=lambda r: r["at"])
    out = {"window": name, "models": w.get("models"), "length_s": length, "start": iso(start), "resets": iso(resets),
           "finished": now >= resets, "readings": len(readings), "reserve": reserve, "calibrated": False,
           "method": None, "units_per_pct": None, "est_used": None, "line": None, "over_by": None,
           "time_share": max(0.0, min(1.0, (now - start) / length)), "pace_ratio": None, "units_used": 0.0,
           "units_left": None, "days_left": max(0.0, (resets - now) / 86400), "allowance_per_day": None,
           "reading_used": readings[-1]["used"] if readings else None,
           "reading_at": iso(readings[-1]["at"]) if readings else None, "unpriced": {}}
    out["line"] = max(0.0, out["time_share"] * 100 - reserve)
    if out["finished"] or not readings:
        return out
    cur = slice_view(sessions, start, now, pattern)
    out["units_used"], out["unpriced"] = cur["total"], cur["unpriced"]
    first, last = readings[0], readings[-1]
    u_first = slice_view(sessions, start, first["at"], pattern)["total"]
    u_last = slice_view(sessions, start, last["at"], pattern)["total"]
    upp = None
    if len(readings) >= 2 and last["used"] > first["used"] and u_last > u_first:
        upp, out["method"] = (u_last - u_first) / (last["used"] - first["used"]), "slope between readings"
    elif last["used"] > 0 and u_last > 0:
        upp, out["method"] = u_last / last["used"], "units since window start / percent used"
    if not upp:
        out["method"] = None
        return out
    est = last["used"] + (cur["total"] - u_last) / upp
    out.update(calibrated=True, units_per_pct=upp, est_used=est, over_by=est - out["line"],
               units_left=max(0.0, (100 - est) * upp))
    out["pace_ratio"] = (est / 100) / out["time_share"] if out["time_share"] > 0 else None
    out["allowance_per_day"] = out["units_left"] / max(out["days_left"], 1.0)
    return out


def evaluate(state: dict, sessions: list[dict], now: float, reserve: float = DEFAULT_RESERVE,
             only: str | None = None) -> list[dict]:
    names = [n for n in state["windows"] if only is None or n == only]
    return [evaluate_window(n, state["windows"][n], sessions, now, reserve) for n in sorted(names)]


def tightest(wins: list[dict]) -> dict | None:
    live = [w for w in wins if w["calibrated"] and not w["finished"]]
    return max(live, key=lambda w: (w["pace_ratio"] or 0, w["est_used"])) if live else None


# ---- measured steps ----------------------------------------------------------------------------

def step_stats(sessions: list[dict]) -> dict:
    """{(model, effort): {steps, units, units_per_step, steps_per_hour}} for priced models.

    Units per step counts the main conversation and its helpers. Steps per hour is measured on the
    main conversation's own timestamps: steps that followed another step within IDLE_GAP_S, over
    the time those gaps add up to.
    """
    view = cost_view(sessions)
    stats: dict[tuple, dict] = {}
    for r in view["rows"]:
        if not r["priced"] or not r["steps"]:
            continue
        s = stats.setdefault((r["model"], r["effort"]), {"steps": 0.0, "units": 0.0, "main_steps": 0, "gap_s": 0.0,
                                                          "gap_steps": 0})
        s["steps"] += r["steps"]
        s["units"] += r["total"]
        if r["kind"] == "main":
            s["main_steps"] += r["steps"]
    for sess in sessions:
        prev = None
        for t in sess.get("turns") or []:
            e = _epoch(t.get("ts"))
            if e is None:
                continue
            if prev is not None and 0 < e - prev <= IDLE_GAP_S:
                s = stats.get((t.get("model") or "", t.get("effort") or ""))
                if s is not None:
                    s["gap_s"] += e - prev
                    s["gap_steps"] += 1
            prev = e
    for s in stats.values():
        s["units_per_step"] = s["units"] / s["steps"]
        s["steps_per_hour"] = s["gap_steps"] / (s["gap_s"] / 3600) if s["gap_s"] else None
    return stats


def pick_model(stats: dict, model: str | None) -> str | None:
    if model:
        return model
    totals: dict[str, float] = {}
    for (m, _e), s in stats.items():
        totals[m] = totals.get(m, 0) + s["main_steps"]
    return max(totals, key=totals.get) if totals else None


def pick_effort(stats: dict, model: str, effort: str | None) -> tuple[str | None, str]:
    """(effort measured, note). The nearest measured effort, labelled, when the asked one has no steps."""
    have = {e: s for (m, e), s in stats.items() if m == model}
    if not have:
        return None, ""
    known = [e for e in have if e in EFFORT_ORDER]
    if effort is None:
        best = max(have, key=lambda e: have[e]["steps"])
        return best, ""
    if effort in have:
        return effort, ""
    if known and effort in EFFORT_ORDER:
        i = EFFORT_ORDER.index(effort)
        near = min(known, key=lambda e: (abs(EFFORT_ORDER.index(e) - i), -EFFORT_ORDER.index(e)))
        return near, f"no steps logged at {effort} on {model}; using the nearest measured effort, {near}"
    best = max(have, key=lambda e: have[e]["steps"])
    return best, (f"no steps logged at {effort} on {model}; using {best or 'effort not logged'}, "
                  "the only measured effort")


def steps_today(win: dict, stats: dict, model: str | None, effort: str | None) -> dict | None:
    if not win["calibrated"] or win["finished"] or not model:
        return None
    if win["models"]:
        try:
            if not re.search(win["models"], model, re.I):
                return None
        except re.error:
            return None
    e, note = pick_effort(stats, model, effort)
    if e is None:
        return None
    ups = stats[(model, e)]["units_per_step"]
    return {"model": model, "effort": e, "units_per_step": ups, "note": note,
            "steps": int(win["allowance_per_day"] / ups) if ups else None}


# ---- gate --------------------------------------------------------------------------------------

def gate(wins: list[dict], now: float) -> dict:
    """Exit-code logic: ok is False when any checked window is over its line (time share minus reserve)."""
    live = [w for w in wins if w["calibrated"] and not w["finished"]]
    if not live:
        return {"ok": True, "wait_seconds": 0, "window": None, "used": None, "line": None,
                "note": "no usable reading stored (not calibrated, finished or none): not blocking"}
    over = [w for w in live if w["est_used"] > w["line"]]
    if not over:
        w = max(live, key=lambda x: x["est_used"] - x["line"])
        return {"ok": True, "wait_seconds": 0, "window": w["window"], "used": w["est_used"], "line": w["line"],
                "note": ""}
    waits = {}
    for w in over:
        t_needed = (w["est_used"] + w["reserve"]) / 100 * w["length_s"]          # seconds into the window
        resets = parse_time(w["resets"])
        waits[w["window"]] = min(max(0.0, resets - w["length_s"] + t_needed - now), max(0.0, resets - now))
    name = max(waits, key=waits.get)
    w = next(x for x in over if x["window"] == name)
    return {"ok": False, "wait_seconds": int(round(waits[name])), "window": name, "used": w["est_used"],
            "line": w["line"], "note": ""}


def run_gate(state: dict, sessions: list[dict], now: float, reserve: float, only: str | None) -> dict:
    return gate(evaluate(state, sessions, now, reserve, only), now)


# ---- team --------------------------------------------------------------------------------------

def team_view(wins: list[dict], stats: dict, n: int, model: str | None, effort: str | None, now: float) -> dict:
    model = pick_model(stats, model)
    res = {"agents": n, "model": model, "effort_asked": effort, "effort": None, "note": "", "ok": False,
           "units_per_step": None, "steps_per_hour": None, "units_per_agent_hour": None, "windows": [],
           "agents_at_once": None, "hours_per_day": None}
    if not model:
        res["note"] = "no priced steps in the logs read, so no team estimate."
        return res
    e, note = pick_effort(stats, model, effort)
    if e is None:
        res["note"] = f"the logs hold no steps for {model}, so no team estimate."
        return res
    s = stats[(model, e)]
    res.update(effort=e, note=note, units_per_step=s["units_per_step"], steps_per_hour=s["steps_per_hour"])
    if not s["steps_per_hour"]:
        res["note"] = (note + "; " if note else "") + "steps in the logs are too far apart to measure steps per hour."
        return res
    rate = s["units_per_step"] * s["steps_per_hour"]
    res["units_per_agent_hour"] = rate
    for w in wins:
        if not w["calibrated"] or w["finished"]:
            continue
        if w["models"]:
            try:
                if not re.search(w["models"], model, re.I):
                    continue
            except re.error:
                continue
        hours_left = max((parse_time(w["resets"]) - now) / 3600, 1 / 60)
        at_once = w["units_left"] / hours_left / rate
        res["windows"].append({"window": w["window"], "agents_at_once": at_once,
                               "hours_per_day": 24 * at_once / n})
    if not res["windows"]:
        res["note"] = (res["note"] + "; " if res["note"] else "") + "no calibrated window applies to this model."
        return res
    tight = min(res["windows"], key=lambda x: x["agents_at_once"])
    res.update(ok=True, agents_at_once=tight["agents_at_once"], hours_per_day=min(24.0, tight["hours_per_day"]),
               tightest=tight["window"])
    return res


# ---- what-if -----------------------------------------------------------------------------------

def what_if(state: dict, sessions: list[dict], wins: list[dict], team: dict | None, now: float) -> dict:
    """The same window with history savings applied: old history never saved again, re-reads halved."""
    rows = []
    for w in wins:
        if not w["calibrated"] or w["finished"]:
            continue
        pattern = _window_pattern(state["windows"][w["window"]])
        view = slice_view(sessions, parse_time(w["resets"]) - w["length_s"], now, pattern)
        b, total = view["buckets"], view["total"]
        saved = sum(b[k] * cut for k, cut in HISTORY_CUT.items())
        if total <= 0:
            continue
        factor = total / (total - saved) if total > saved else None
        rows.append({"window": w["window"], "units_used": total, "units_saved": saved, "share_saved": saved / total,
                     "resave_units": b["resave"], "reread_units": b["reread"], "factor": factor,
                     "est_used": w["est_used"],
                     "est_used_with_cuts": w["est_used"] - saved / w["units_per_pct"]})
    out = {"windows": rows, "team": None}
    if team and team.get("ok"):
        f = min((r["factor"] or 1.0) for r in rows) if rows else 1.0
        tight = next((r for r in rows if r["window"] == team.get("tightest")), None)
        f = (tight or {}).get("factor") or f
        raw = 24 * team["agents_at_once"] / team["agents"]
        out["team"] = {"agents": team["agents"], "factor": f, "hours_per_day": min(24.0, raw),
                       "hours_per_day_with_cuts": min(24.0, raw * f), "agents_at_once": team["agents_at_once"],
                       "agents_at_once_with_cuts": team["agents_at_once"] * f}
    return out


# ---- text --------------------------------------------------------------------------------------

def _n(x: float) -> str:
    if x >= 1_000_000:
        return f"{x / 1_000_000:.1f}M"
    if x >= 1_000:
        return f"{x / 1_000:.0f}k"
    return f"{x:.0f}"


def _f(x: float) -> str:
    return f"{x:.2f}" if x < 1 else f"{x:.1f}"


def format_windows(wins: list[dict], today: dict[str, dict | None], tight: dict | None) -> list[str]:
    if not wins:
        return [f"No window readings stored. Read the percent used from your plan's usage page and run:",
                f"  {CLI_NAME} pace set --window weekly --used 60 --resets 2026-01-12T09:00:00 --length 7d",
                NOT_A_BILL]
    lines = ["Pace (estimates): is the plan on an even burn through each window?",
             f"Units are price-weighted, 1 unit = one new input token on the mid model. {NOT_A_BILL}"]
    for w in wins:
        lines += ["", f"{w['window']}  (length {dur(w['length_s'])}, resets {w['resets']}"
                      + (f", models /{w['models']}/" if w["models"] else "") + ")"]
        if w["finished"]:
            lines.append("    this window has finished; read the new one and run `pace set` again.")
            continue
        if not w["readings"]:
            lines.append("    no reading stored.")
            continue
        lines.append(f"    reading: {w['reading_used']:g}% used at {w['reading_at']}  ({w['readings']} stored)")
        share = round(100 * w["time_share"])
        if not w["calibrated"]:
            lines.append(f"    cannot calibrate: no priced steps logged since the window began, or 0% used. "
                         f"{share}% of the window has gone.")
        else:
            ratio = f"{w['pace_ratio']:.2f}" if w["pace_ratio"] is not None else "n/a"
            side = "over" if w["over_by"] > 0 else "under"
            lines += [f"    estimated used now: ~{w['est_used']:.0f}%   share of window gone: {share}%   "
                      f"pace ratio: {ratio}  ({side} the line by {abs(w['over_by']):.0f} points; line = time share "
                      f"minus {w['reserve']:g} reserve)",
                      f"    calibration: ~{_n(w['units_per_pct'])} units per 1% ({w['method']}); "
                      f"~{_n(w['units_left'])} units left, ~{_n(w['allowance_per_day'])} per remaining day "
                      f"({w['days_left']:.1f} days left)"]
            t = today.get(w["window"])
            if t and t["steps"] is not None:
                lines.append(f"    about {t['steps']} steps left today on {t['model']} at effort "
                             f"{t['effort'] or 'not logged'} (~{_n(t['units_per_step'])} units per step)"
                             + (f". Note: {t['note']}" if t["note"] else ""))
        if w["unpriced"]:
            lines.append("    left out, no price ratio: " + ", ".join(f"{m} ({n} steps)" for m, n in w["unpriced"].items()))
    if tight:
        lines += ["", f"Tightest window: {tight['window']} (pace ratio {tight['pace_ratio']:.2f}, "
                      f"~{tight['est_used']:.0f}% used with {round(100 * tight['time_share'])}% of the window gone)."]
    return lines


def format_gate(g: dict) -> list[str]:
    if g["window"] is None:
        return [f"pace gate: ok. {g['note']}"]
    if g["ok"]:
        return [f"pace gate: ok. {g['window']} ~{g['used']:.0f}% used (estimate), line {g['line']:.0f}%."]
    return [f"pace gate: over. {g['window']} ~{g['used']:.0f}% used (estimate), line {g['line']:.0f}%; "
            f"wait about {dur(g['wait_seconds'])} for the line to catch up."]


def format_team(t: dict) -> list[str]:
    if not t["ok"]:
        return [f"Team of {t['agents']}: {t['note']}"]
    lines = [f"Team of {t['agents']} on {t['model']} at effort {t['effort']} (estimates, not a bill):",
             f"    measured: ~{_n(t['units_per_step'])} units per step, {t['steps_per_hour']:.0f} steps per hour, "
             f"~{_n(t['units_per_agent_hour'])} units per agent-hour"]
    if t["note"]:
        lines.append(f"    note: {t['note']}")
    for w in t["windows"]:
        lines.append(f"    {w['window']}: {_f(w['agents_at_once'])} agents at once on the line, "
                     f"{_f(min(24.0, w['hours_per_day']))} h a day for {t['agents']} agents")
    lines.append(f"    so: {_f(t['agents_at_once'])} agents can run at once on the line; {t['agents']} agents can run "
                 f"about {_f(t['hours_per_day'])} hours a day (tightest window: {t['tightest']}).")
    return lines


def format_what_if(w: dict) -> list[str]:
    if not w["windows"]:
        return ["What-if: no priced steps logged in a calibrated window, nothing to compare."]
    lines = ["What if old history was never saved again and re-reads were halved? (same models, same effort; "
             "estimates, not a bill)"]
    for r in w["windows"]:
        f = f"{r['factor']:.2f}x" if r["factor"] else "n/a"
        lines.append(f"    {r['window']}: history re-saved ~{_n(r['resave_units'])} units, re-read ~{_n(r['reread_units'])} "
                     f"units; the cuts remove ~{round(100 * r['share_saved'])}% of what was used. Estimated used would be "
                     f"~{r['est_used_with_cuts']:.0f}% instead of ~{r['est_used']:.0f}%, {f} more agent-hours.")
    t = w["team"]
    if t:
        lines.append(f"    {t['agents']} agents: {_f(t['hours_per_day'])} h a day now, {_f(t['hours_per_day_with_cuts'])} h a "
                     f"day with the cuts ({_f(t['agents_at_once'])} -> {_f(t['agents_at_once_with_cuts'])} agents at once; "
                     "a day is capped at 24 h).")
    return lines


# ---- watch -------------------------------------------------------------------------------------

def watch_over(state: dict, sessions: list[dict], now: float, points: float = WATCH_POINTS) -> list[dict]:
    """Windows whose estimated use is `points` or more above their even-pace line. Silent without a reading,
    when the window cannot be calibrated, or when the reading belongs to a finished window."""
    out = []
    for w in evaluate(state, sessions, now, 0.0):
        if w["calibrated"] and not w["finished"] and w["est_used"] - w["time_share"] * 100 >= points:
            out.append(w)
    return out
