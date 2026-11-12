"""Weekly usage roll-up from local logs: `tokenstretch report --weekly` and the dashboard.

Core only: records come from each adapter's `read_usage_logs`, sessions from `watch_sessions` /
`live_sessions`, and waste spots from watch's `session_warnings`. Nothing here touches the network
or writes files. Cost is shown only where the logs themselves report it; there is no price table,
so nothing is estimated.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path

from .context import Context
from .findings import UsageRecord
from .identity import state_dir

TOKEN_KEYS = ("input_uncached", "cache_read", "cache_write", "output", "reasoning")
NOT_REPORTED = "not reported"
OBSERVATIONAL = ("Observational: measured from your own logs before and after setup. Not a controlled "
                 "bench; a different mix of work changes these numbers.")


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _local(dt: datetime, tz: tzinfo | None) -> datetime:
    return dt.astimezone(tz) if tz else dt.astimezone()


def week_start(dt: datetime, tz: tzinfo | None = None) -> str:
    """ISO date of the Monday that starts dt's local week."""
    d = _local(dt, tz).date()
    return (d - timedelta(days=d.weekday())).isoformat()


def _tokens(r: UsageRecord) -> dict[str, int]:
    return {"input_uncached": r.input_tokens, "cache_read": r.cache_read_tokens,
            "cache_write": r.cache_write_tokens, "output": r.output_tokens, "reasoning": r.reasoning_tokens}


class Bucket:
    """Token sums by type, plus cost only from records that report it."""

    def __init__(self) -> None:
        self.tokens = dict.fromkeys(TOKEN_KEYS, 0)
        self.records = 0
        self.cost = 0.0
        self.cost_records = 0
        self.sessions: set[str] = set()

    def add(self, r: UsageRecord) -> None:
        for k, v in _tokens(r).items():
            self.tokens[k] += int(v or 0)
        self.records += 1
        if r.cost_usd is not None:
            self.cost += float(r.cost_usd)
            self.cost_records += 1
        if r.session_id:
            self.sessions.add(f"{r.adapter_id}:{r.session_id}")

    def to_json(self) -> dict:
        return {"tokens": dict(self.tokens), "total_tokens": sum(self.tokens.values()), "records": self.records,
                "sessions": len(self.sessions), "cost": cost_field(self.cost, self.cost_records, self.records)}


def cost_field(total: float, with_cost: int, records: int) -> dict:
    """{usd, label, reported, records}. usd is None unless at least one record reported a cost."""
    if not with_cost:
        return {"usd": None, "label": NOT_REPORTED, "reported": 0, "records": records}
    label = "reported by the logs" if with_cost == records else f"reported for {with_cost} of {records} steps"
    return {"usd": round(total, 6), "label": label, "reported": with_cost, "records": records}


def read_records(ctx: Context, *, days: int = 35, adapters=None, cache=None) -> list[UsageRecord]:
    """Usage records from the last `days`, through the incremental log cache (files older than the
    window are skipped by mtime before they are opened)."""
    from .adapters import REGISTRY
    from .logcache import LogCache
    cache = cache if cache is not None else LogCache()
    return cache.records(ctx, adapters if adapters is not None else REGISTRY, since_days=days)


def aggregate(records: list[UsageRecord], *, weeks: int = 4, now: datetime | None = None,
              tz: tzinfo | None = None) -> dict:
    """Per-week buckets (newest first, empty weeks included) plus today / this week / by model."""
    now = now or datetime.now(timezone.utc)
    this_week = week_start(now, tz)
    first = datetime.fromisoformat(this_week).date() - timedelta(weeks=weeks - 1)
    keys = [(first + timedelta(weeks=i)).isoformat() for i in range(weeks)]
    by_week = {k: Bucket() for k in keys}
    by_week_model: dict[str, dict[str, Bucket]] = {k: {} for k in keys}
    today, undated = Bucket(), 0
    today_key = _local(now, tz).date()
    from .logcache import rec_epoch
    tzkey = repr(tz)
    for r in records:
        ep = rec_epoch(r)
        if ep is None:
            undated += 1
            continue
        cached = r.__dict__.get("_day")
        if cached is None or cached[0] != tzkey:  # local day + week, computed once per record
            day = _local(datetime.fromtimestamp(ep, timezone.utc), tz).date()
            cached = r.__dict__["_day"] = (tzkey, day, (day - timedelta(days=day.weekday())).isoformat())
        _, day, wk = cached
        if wk not in by_week:
            continue
        by_week[wk].add(r)
        by_week_model[wk].setdefault(r.model or "unknown", Bucket()).add(r)
        if day == today_key:
            today.add(r)
    weeks_out = []
    for k in reversed(keys):
        row = by_week[k].to_json()
        row["week"] = k
        row["models"] = sorted(({"model": m, **b.to_json()} for m, b in by_week_model[k].items()),
                               key=lambda x: -x["total_tokens"])
        weeks_out.append(row)
    return {"weeks": weeks_out, "today": today.to_json(), "this_week": weeks_out[0], "undated_records": undated}


# ---- sessions and waste spots ---------------------------------------------------------

def sessions(ctx: Context, adapters=None, limit: int = 10, max_age_s: float | None = None) -> list[dict]:
    """Newest sessions across tools: watch_sessions when supported, else live_sessions."""
    from .setup import detected_adapters
    rows: list[dict] = []
    for ad in adapters if adapters is not None else detected_adapters(ctx):
        ws = ad.watch_sessions(ctx, limit=limit, max_age_s=max_age_s)
        if ws is not None:
            for s in ws:
                last = (s.get("turns") or [{}])[-1]
                rows.append({"adapter": s["adapter"], "tool": ad.display_name, "session": s["session"],
                             "project": s.get("project", ""), "model": last.get("model") or "",
                             "history_tokens": int(last.get("history_tokens") or 0),
                             "last_write": s.get("last_write"), "clear_hint": s.get("clear_hint", ""),
                             "auto_compact": s.get("auto_compact")})
            continue
        for s in ad.live_sessions(ctx, limit=limit, max_age_s=max_age_s) or []:
            rows.append({"adapter": s.get("adapter", ad.id), "tool": ad.display_name, "session": s.get("session", ""),
                         "project": s.get("project", ""), "model": s.get("model") or "",
                         "history_tokens": int(s.get("history_tokens") or 0),
                         "last_write": s.get("last_write"), "clear_hint": s.get("clear_hint", ""),
                             "auto_compact": s.get("auto_compact")})
    rows.sort(key=lambda r: r.get("last_write") or 0, reverse=True)
    return rows[:limit]


def waste_spots(ctx: Context, adapters=None, cfg=None, limit: int = 20) -> list[dict]:
    """Every watch condition in the newest sessions (not only active ones), newest session first."""
    from .setup import detected_adapters
    from .watch import WatchConfig, _short_project, session_warnings
    cfg = cfg or WatchConfig()
    out = []
    for ad in adapters if adapters is not None else detected_adapters(ctx):
        for s in ad.watch_sessions(ctx, limit=limit) or []:
            for w in session_warnings(ctx, ad, s, cfg):
                out.append({"kind": w.key[-1], "tool": ad.display_name, "session": w.session,
                            "project": _short_project(w.project), "what": w.what, "why": w.why, "fix": w.fix,
                            "last_write": s.get("last_write")})
    out.sort(key=lambda x: x.get("last_write") or 0, reverse=True)
    return out[:limit]


# ---- since setup (observational) and bench results ---------------------------------------

def setup_date(ctx: Context) -> datetime | None:
    """When the earliest still-recorded setup/install run happened (from manifests), or None."""
    from .setup import applied_changes
    times = [_parse(m.get("created_at")) for m in applied_changes(ctx, limit=500)]
    times = [t for t in times if t]
    return min(times) if times else None


def since_setup(records: list[UsageRecord], when: datetime | None, *, now: datetime | None = None,
                is_top=None) -> dict | None:
    """Per-step averages in equal windows before and after `when`. Measured log data, not a bench claim."""
    if when is None:
        return None
    now = now or datetime.now(timezone.utc)
    span = max(now - when, timedelta(days=1))
    span = min(span, timedelta(days=28))

    def side(lo: datetime, hi: datetime) -> dict:
        rs = [r for r in records if (d := _parse(r.ts)) and lo <= d < hi]
        n = len(rs)
        tot_in = sum(r.total_input() for r in rs)
        out = sum(r.output_tokens for r in rs)
        top = sum(1 for r in rs if is_top and is_top(r))
        costs = [r.cost_usd for r in rs if r.cost_usd is not None]
        return {"from": lo.isoformat(), "to": hi.isoformat(), "steps": n,
                "avg_input_per_step": round(tot_in / n) if n else None,
                "avg_output_per_step": round(out / n) if n else None,
                "top_model_share": round(top / n, 3) if n and is_top else None,
                "cost": cost_field(sum(costs), len(costs), n)}

    return {"setup_at": when.isoformat(), "before": side(when - span, when), "after": side(when, now),
            "window_days": round(span.total_seconds() / 86400, 1), "label": OBSERVATIONAL}


def bench_results(ctx: Context) -> list[dict]:
    """Saved `bench --measure` results (benches/*/result.json). Empty when no bench was measured."""
    root = state_dir(ctx.home) / "benches"
    if not root.is_dir():
        return []
    out = []
    for p in sorted(root.glob("*/result.json"), reverse=True)[:10]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            out.append({"id": p.parent.name, "before": data.get("before") or {}, "after": data.get("after") or {},
                        "savings": data.get("savings") or {}, "after_model": data.get("after_model"),
                        "mode": data.get("mode")})
    return out


def _top_checker(adapters):
    by_id = {a.id: a for a in adapters}

    def is_top(r: UsageRecord) -> bool:
        ad = by_id.get(r.adapter_id)
        return bool(ad and r.model and ad.is_top_model(r.model))
    return is_top


def build_weekly(ctx: Context, *, weeks: int = 4, now: datetime | None = None, tz: tzinfo | None = None,
                 records: list[UsageRecord] | None = None) -> dict:
    from .adapters import REGISTRY
    from .setup import detected_adapters
    adapters = detected_adapters(ctx)
    now = now or datetime.now(timezone.utc)
    if records is None:
        records = read_records(ctx, days=weeks * 7 + 7)
    agg = aggregate(records, weeks=weeks, now=now, tz=tz)
    return {**agg,
            "top_sessions": sorted(sessions(ctx, adapters, limit=20), key=lambda s: -s["history_tokens"])[:5],
            "waste": waste_spots(ctx, adapters),
            "since_setup": since_setup(records, setup_date(ctx), now=now, is_top=_top_checker(REGISTRY)),
            "bench": bench_results(ctx)}


# ---- HTML ----------------------------------------------------------------------------------

def _n(x) -> str:
    return f"{int(x or 0):,}"


def _usd(c: dict) -> str:
    return NOT_REPORTED if c.get("usd") is None else f"${c['usd']:.2f} ({c['label']})"


def build_weekly_html(ctx: Context, data: dict) -> str:
    from .identity import PRODUCT_NAME, VERSION
    from .setup import _CSS, _e
    from .watch import _short_project
    now = datetime.now().astimezone().strftime("%b %d, %Y %I:%M %p %Z")
    h = [f"<!doctype html><html><head><meta charset='utf-8'><title>{_e(PRODUCT_NAME)} weekly report</title>",
         f"<style>{_CSS}</style></head><body>", f"<h1>{_e(PRODUCT_NAME)} weekly report</h1>",
         f"<p class='muted'>Generated {_e(now)} by {_e(PRODUCT_NAME)} {_e(VERSION)} from local logs only. "
         "Cost is shown only where the logs report it; nothing is estimated. This page loads nothing from the internet.</p>",
         "<h2>Tokens by week</h2><table><tr><th>Week of</th><th>Uncached input</th><th>Cache read</th>"
         "<th>Cache write</th><th>Output</th><th>Reasoning</th><th>Total</th><th>Steps</th><th>Cost</th></tr>"]
    for w in data["weeks"]:
        t = w["tokens"]
        h.append(f"<tr><td>{_e(w['week'])}</td>" + "".join(f"<td>{_n(t[k])}</td>" for k in TOKEN_KEYS)
                 + f"<td><b>{_n(w['total_tokens'])}</b></td><td>{_n(w['records'])}</td><td>{_e(_usd(w['cost']))}</td></tr>")
    h.append("</table>")
    if data.get("undated_records"):
        h.append(f"<p class='muted'>{_n(data['undated_records'])} log record(s) had no timestamp and are not in any week.</p>")
    h.append("<h2>By model (each week)</h2>")
    for w in data["weeks"]:
        if not w["models"]:
            continue
        h.append(f"<h3>Week of {_e(w['week'])}</h3><table><tr><th>Model</th><th>Total tokens</th><th>Steps</th><th>Cost</th></tr>")
        for m in w["models"]:
            h.append(f"<tr><td><code>{_e(m['model'])}</code></td><td>{_n(m['total_tokens'])}</td>"
                     f"<td>{_n(m['records'])}</td><td>{_e(_usd(m['cost']))}</td></tr>")
        h.append("</table>")
    h.append("<h2>Biggest session histories</h2>")
    if data["top_sessions"]:
        h.append("<table><tr><th>Tool</th><th>Project</th><th>Model</th><th>History re-sent per step</th></tr>")
        for s in data["top_sessions"]:
            flag = " <span class='warn'>over 150K</span>" if s["history_tokens"] > 150_000 else ""
            h.append(f"<tr><td>{_e(s['tool'])}</td><td>{_e(_short_project(s['project']))}</td><td>{_e(s['model'])}</td>"
                     f"<td>{_n(s['history_tokens'])}{flag}</td></tr>")
        h.append("</table>")
    else:
        h.append("<p>No local session logs found.</p>")
    h.append("<h2>Waste spots</h2>")
    if data["waste"]:
        h.append("<ul>")
        for w in data["waste"]:
            h.append(f"<li><b>{_e(w['tool'])} {_e(w['session'])} · {_e(w['project'])}</b>: {_e(w['what'])}"
                     f"<br><span class='muted'>Why: {_e(w['why'])}</span><br>Fix: {_e(w['fix'])}</li>")
        h.append("</ul>")
    else:
        h.append("<p>No waste spotted in the newest sessions.</p>")
    h.append("<h2>Since setup</h2>")
    ss = data.get("since_setup")
    if ss:
        h.append(f"<p class='warn'>{_e(ss['label'])}</p><table><tr><th></th><th>Steps</th><th>Avg input per step</th>"
                 "<th>Avg output per step</th><th>Top-model share</th><th>Cost</th></tr>")
        for name in ("before", "after"):
            s = ss[name]
            share = "-" if s["top_model_share"] is None else f"{s['top_model_share'] * 100:.0f}%"
            h.append(f"<tr><td>{name.title()} ({_e(s['from'][:10])} to {_e(s['to'][:10])})</td><td>{_n(s['steps'])}</td>"
                     f"<td>{_e('-' if s['avg_input_per_step'] is None else _n(s['avg_input_per_step']))}</td>"
                     f"<td>{_e('-' if s['avg_output_per_step'] is None else _n(s['avg_output_per_step']))}</td>"
                     f"<td>{_e(share)}</td><td>{_e(_usd(s['cost']))}</td></tr>")
        h.append("</table>")
    else:
        h.append("<p>No setup run recorded yet, so there is nothing to compare.</p>")
    h.append("<h2>Bench results</h2>")
    if data["bench"]:
        h.append(bench_table_html(data["bench"]))
    else:
        h.append("<p>No measured bench on this machine (run <code>tokenstretch bench --prepare</code>, then "
                 "<code>--measure</code>).</p>")
    h.append("</body></html>\n")
    return "\n".join(h)


def bench_table_html(rows: list[dict]) -> str:
    from .setup import _e
    h = ["<table><tr><th>Bench</th><th>Side</th><th>Input (uncached)</th><th>Cache read</th><th>Output</th><th>Cost</th></tr>"]
    for b in rows:
        for side in ("before", "after"):
            s = b.get(side) or {}
            cost = s.get("cost_usd")
            h.append(f"<tr><td>{_e(b['id'])}</td><td>{side}</td><td>{_n(s.get('input_uncached'))}</td>"
                     f"<td>{_n(s.get('cache_read'))}</td><td>{_n(s.get('output'))}</td>"
                     f"<td>{_e(NOT_REPORTED if cost is None else f'${cost:.4f}')}</td></tr>")
    h.append("</table>")
    return "".join(h)


def write_weekly(ctx: Context, dest: Path | None = None, *, weeks: int = 4) -> Path:
    from .installer import _now_stamp, kit_root
    if dest is None:
        d = kit_root(ctx) / "reports"
        d.mkdir(parents=True, exist_ok=True)
        dest = d / f"weekly-{_now_stamp()}.html"
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(build_weekly_html(ctx, build_weekly(ctx, weeks=weeks)), encoding="utf-8")
    return dest
