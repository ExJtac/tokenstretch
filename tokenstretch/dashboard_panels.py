"""The extra dashboard panels: pace, team queue, cost, redo work, start-up size and watch.

Each builder takes what it needs (context, adapters, a clock, a `scrub` function for text that may
hold secrets or home paths) and returns plain JSON-able data. Every one reads local files through
the same code as its command (`pace`, `queue list`, `model cost`, `model rework`, `startup`) and
never writes. A panel with nothing to show returns `empty`: one line saying why and what to run,
never a row of zeros. `DashboardState` runs each in isolation, so one failure hides one card.

Numbers that are estimates or heuristics carry a note saying so; none of them is a bill.
"""
from __future__ import annotations

from typing import Callable

from .identity import CLI_NAME

AUTO_NAMES = ("auto-5h", "auto-week")          # windows the status line fills in by itself
SESSION_LIMIT = 30                             # newest sessions read for cost, redo and start-up
PACE_PAD_S = 86400                             # same read margin the watcher uses
TASK_SHOW = 12


def _sessions(ctx, adapters, limit: int = SESSION_LIMIT) -> list[dict]:
    out: list[dict] = []
    for ad in adapters:
        if getattr(ad, "id", "") == "host":
            continue
        out += ad.watch_sessions(ctx, limit=limit) or []
    return out


def _local(epoch, fmt="%b %d %I:%M %p"):
    from datetime import datetime
    return datetime.fromtimestamp(epoch).astimezone().strftime(fmt) if epoch else ""


# ---- pace -----------------------------------------------------------------------------------

def _window_status(w: dict) -> tuple[str, str]:
    if w["finished"]:
        return "finished", "This window has ended. Store the new one."
    if not w["calibrated"]:
        return "needs-reading", "Not enough to estimate yet: it needs a reading and some local usage inside the window."
    if w["est_used"] > w["line"]:
        return "over", "Over the even-pace line. Pause or slow agents until it drops below."
    return "ok", "Under the even-pace line."


def build_pace(ctx, adapters, now: float) -> dict:
    from . import pace as pc
    explain = ("How much of your plan's window you have probably used against how much of the window has passed. "
               "The even-pace line is the share of time gone minus a small reserve. Estimates from local logs and "
               "your last reading, not a bill.")
    if not pc.state_path(ctx.home).exists():
        return {"explain": explain, "windows": [], "gate": None,
                "empty": f"No plan window stored. Run `{CLI_NAME} pace auto` (the status line fills it in), "
                         f"or `{CLI_NAME} pace set --window weekly --used PCT --resets ISO_TIME --length 7d`."}
    state = pc.load_state(ctx.home)
    if not state["windows"]:
        return {"explain": explain, "windows": [], "gate": None,
                "empty": f"No plan window stored. Run `{CLI_NAME} pace auto`, or `{CLI_NAME} pace set ...`."}
    starts = [pc.parse_time(w["resets"]) - w["length_s"] for w in state["windows"].values()]
    sessions: list[dict] = []
    for ad in adapters:
        sessions += ad.window_sessions(ctx, limit=500, max_age_s=max(0.0, now - min(starts)) + PACE_PAD_S) or []
    wins = pc.evaluate(state, sessions, now)
    rows = []
    for w in wins:
        status, todo = _window_status(w)
        rows.append({
            "window": w["window"], "source": "automatic" if w["window"] in AUTO_NAMES else "typed by hand",
            "status": status, "todo": todo, "finished": w["finished"], "calibrated": w["calibrated"],
            "est_used": w["est_used"], "line": w["line"], "over_by": w["over_by"],
            "time_share": w["time_share"], "pace_ratio": w["pace_ratio"], "days_left": w["days_left"],
            "reading_used": w["reading_used"], "reading_at": w["reading_at"], "resets": w["resets"],
            "models": w["models"], "method": w["method"]})
    g = pc.gate(wins, now)
    gate = {"open": bool(g["ok"]), "wait_seconds": int(g["wait_seconds"] or 0),
            "wait": pc.dur(g["wait_seconds"]) if g["wait_seconds"] else "", "window": g["window"], "note": g["note"]}
    tight = pc.tightest(wins)
    return {"explain": explain, "windows": rows, "gate": gate, "tightest": tight["window"] if tight else None,
            "todo": ("A window is over its line: " + f"run `{CLI_NAME} pace --gate` before starting an agent."
                     if any(r["status"] == "over" for r in rows) else ""),
            "note": pc.NOT_A_BILL}


# ---- team queue -----------------------------------------------------------------------------

def build_queue(ctx, adapters, now: float, scrub: Callable[[str], str]) -> dict:
    from . import pace as pc
    from . import queue as q
    explain = ("The shared task list that worker agents pull from, one task at a time, and whether the pace gate "
               "would let the next one start.")
    qd = q.queue_dir(ctx.root)
    rows = q.list_view(ctx.root) if qd.is_dir() else []
    if not rows:
        return {"explain": explain, "tasks": [], "counts": {}, "gate": None,
                "empty": f"The queue is empty or not set up in this folder. Add work with `{CLI_NAME} queue add`."}
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    gate = None
    if pc.state_path(ctx.home).exists():
        state = pc.load_state(ctx.home)
        if state["windows"]:
            starts = [pc.parse_time(w["resets"]) - w["length_s"] for w in state["windows"].values()]
            sessions: list[dict] = []
            for ad in adapters:
                sessions += ad.window_sessions(ctx, limit=500, max_age_s=max(0.0, now - min(starts)) + PACE_PAD_S) or []
            g = pc.run_gate(state, sessions, now, pc.DEFAULT_RESERVE, None)
            gate = {"open": bool(g["ok"]), "wait_seconds": int(g["wait_seconds"] or 0),
                    "wait": pc.dur(g["wait_seconds"]) if g["wait_seconds"] else "", "window": g["window"]}
    stuck = [r for r in rows if r["status"] == "needs-lead"]
    tasks = [{"id": r["id"], "status": r["status"], "failures": r["failures"], "agent": scrub(r["agent"]),
              "hard": r["hard"], "task": scrub(r["task"])[:140]} for r in rows[:TASK_SHOW]]
    return {"explain": explain, "tasks": tasks, "more": max(0, len(rows) - TASK_SHOW), "counts": counts, "gate": gate,
            "todo": (f"{len(stuck)} task(s) need the lead: see `{CLI_NAME} queue list`." if stuck else "")}


# ---- cost -----------------------------------------------------------------------------------

def build_cost(ctx, sessions: list[dict], scrub: Callable[[str], str]) -> dict:
    from . import cost
    explain = ("Where your recent tokens went, weighted by list-price ratios: history re-read, history saved again, "
               "new content, and output. Units are tokens priced as new mid-model input, an estimate, not a bill.")
    view = cost.cost_view(sessions)
    if not view["total"]:
        return {"explain": explain, "rows": [], "grid": [],
                "empty": "No priced steps found in the newest sessions yet. Run a session, then check back."}
    label = {"reread": "history re-read", "resave": "history saved again", "new": "new content",
             "output": "output and thinking"}
    rows = [{"model": r["model"] or "unknown", "effort": r["effort"], "kind": r["kind"], "steps": r["steps"],
             "share": r["share"], "units": r["total"], "priced": r["priced"],
             "biggest": label.get(r["biggest"], ""), "lever": scrub(r["lever"]),
             "buckets": {b: r["units"][b] for b in cost.BUCKETS}} for r in view["rows"][:8]]
    grid = [{k: g[k] for k in ("model", "effort", "steps", "units_per_step", "times_cheapest")}
            for g in cost.grid(view)[:6]]
    return {"explain": explain, "read": view["read"], "total": view["total"], "rows": rows, "grid": grid,
            "prices_read": view["prices_read"], "note": "Estimates from published price ratios, not a bill.",
            "todo": next((r["lever"] for r in rows if r["lever"]), "")}


# ---- redo work ------------------------------------------------------------------------------

def build_rework(ctx, sessions: list[dict], scrub: Callable[[str], str]) -> dict:
    from . import rework
    explain = ("Steps that look like redoing earlier work: an edit right after a failed check, a file edited three "
               "times, or you correcting the assistant. A guess from log patterns, not a measurement.")
    view = rework.rework_view(sessions)
    rows = [{"model": r["model"] or "unknown", "effort": r["effort"], "sessions": r["sessions"], "steps": r["steps"],
             "redo_steps": r["redo_steps"], "redo_share": r["redo_share"], "signals": r["signals"]}
            for r in view["rows"][:8]]
    if not rows:
        return {"explain": explain, "rows": [],
                "empty": "No Claude Code sessions with readable logs yet, so there is nothing to compare.",
                "note": view["note"]}
    return {"explain": explain, "read": view["read"], "rows": rows, "comparison": scrub(view["comparison"]),
            "note": view["note"], "left_out": view["left_out"]}


# ---- start-up size --------------------------------------------------------------------------

def build_startup(ctx, adapters, scrub: Callable[[str], str]) -> dict:
    from . import startup
    explain = ("What every new session carries before you type anything: instruction files, skill and connector "
               "listings, and the tool's own prompt. It is re-sent on every step, so it is the cheapest thing to trim.")
    data = startup.build(ctx, adapters, remember=False)
    tools = []
    for d in data:
        if not d["parts"] and not d["measured_start"]:
            continue
        tools.append({
            "tool": d["name"], "measured_start": d["measured_start"], "known": d["known"], "rest": d["rest"],
            "since_last": d["since_last"],
            "parts": [{"kind": p["kind"], "name": scrub(p["name"]), "tokens": p["tokens"],
                       "fix": scrub(p.get("fix") or "")} for p in d["parts"][:8]],
            "price": ({"model": d["price"]["model"], "steps": d["price"]["steps"], "units": d["price"]["units"]}
                      if d.get("price") else None)})
    if not tools:
        return {"explain": explain, "tools": [],
                "empty": "No supported tool with a readable start-up size found on this machine."}
    return {"explain": explain, "tools": tools,
            "note": "File sizes are approximate (bytes / 4); the measured start is from the newest session's first step.",
            "todo": f"`{CLI_NAME} trim` shortens instruction files; turn off connectors you do not use."}


# ---- watch ----------------------------------------------------------------------------------

def build_watch(feed: list[dict], cfg, scrub: Callable[[str], str]) -> dict:
    explain = ("The background check that spots credit waste while you work: long histories, repeated file reads, "
               "a cache that expired, a plan window over its line. Warnings also show in your session through the "
               "prompt hook.")
    limits = {"history_tokens": getattr(cfg, "max_context", None), "repeat_reads": getattr(cfg, "max_reads", None),
              "pace_points": getattr(cfg, "pace_points", None)}
    if not feed:
        return {"explain": explain, "kinds": [], "limits": limits,
                "empty": "No warnings since the dashboard started. That is the good case."}
    kinds: dict[str, dict] = {}
    for w in feed:
        k = kinds.setdefault(w["kind"], {"kind": w["kind"], "count": 0, "last_what": "", "last_fix": "",
                                         "last_seen": w.get("seen_local", "")})
        k["count"] += 1
        if not k["last_what"]:
            k["last_what"], k["last_fix"] = scrub(w["what"]), scrub(w["fix"])
    ordered = sorted(kinds.values(), key=lambda k: -k["count"])
    return {"explain": explain, "kinds": ordered, "limits": limits, "total": len(feed),
            "todo": ordered[0]["last_fix"]}


# ---- page pieces ----------------------------------------------------------------------------
# Inline HTML cards and the JS that fills them. Injected into the dashboard page by render_page;
# the JS uses the page's own helpers (el, clear, table, bar, fmt, wait) and builds nodes with
# textContent only, so nothing from a log or a task ever becomes markup.

# Doctor sits beside Pace; the team queue takes the old Doctor spot lower on the page.
QUEUE_CARD = '<section class="card c7"><h2>Team queue</h2><div id="p-queue"></div></section>'

PANEL_CARDS = """<section class="card c6"><h2>Pace</h2><div id="p-pace"></div></section>
__DOCTOR_CARD__
<section class="card c7"><h2>Cost by model and effort</h2><div id="p-cost"></div></section>
<section class="card c5"><h2>Redo work</h2><div id="p-rework"></div></section>
<section class="card c7"><h2>Start-up size</h2><div id="p-startup"></div></section>
<section class="card c5"><h2>Watch</h2><div id="p-watch"></div></section>"""

PANEL_JS = r"""
function pct(x){return x==null?"-":Math.round(x)+"%"}
function shell(s,key,fill){
  var n=clear($("p-"+key)),p=(s.panels||{})[key];
  if(p==null){var bad=(s.errors||{})["panel "+key];wait(n,bad?"This panel could not be read; the page keeps working without it.":"Warming up...");return}
  n.appendChild(el("div","sub",p.explain));
  if(p.empty){n.appendChild(el("div","empty",p.empty))}else{fill(n,p)}
  if(p.todo)n.appendChild(el("div","fix","What to do: "+p.todo));
  if(p.note)n.appendChild(el("div","sub",p.note))
}
function gateLine(g){if(!g)return null;return el("p",g.open?"off":"on","Pace gate: "+(g.open?"open, the next task may start":"closed, wait about "+g.wait+(g.window?" (window "+g.window+")":"")))}
function renderPanels(s){
  shell(s,"pace",function(n,p){
    n.appendChild(table(["Window","Reading","Used (est.)","Even-pace line","Resets","Status"],p.windows.map(function(w){
      var c=el("div");c.appendChild(el("span",w.status==="over"?"on":null,pct(w.est_used)));if(w.est_used!=null)c.appendChild(bar(w.est_used,w.status==="over"));
      return[w.window,w.source+(w.reading_used!=null?" · "+pct(w.reading_used):""),c,pct(w.line),(w.resets||"").replace("T"," "),w.todo]})));
    var g=gateLine(p.gate);if(g)n.appendChild(g);
    if(p.gate&&p.gate.note)n.appendChild(el("div","sub",p.gate.note))});
  shell(s,"queue",function(n,p){
    n.appendChild(el("div",null,Object.keys(p.counts).sort().map(function(k){return p.counts[k]+" "+k}).join(", ")));
    n.appendChild(table(["Task","Status","Agent","Brief"],p.tasks.map(function(t){return[t.id+(t.hard?" (hard)":""),t.status+(t.failures?" · "+t.failures+" failed":""),t.agent,t.task]})));
    if(p.more)n.appendChild(el("div","sub","+"+p.more+" more: run the queue list command."));
    var g=gateLine(p.gate);if(g)n.appendChild(g);else n.appendChild(el("div","sub","No pace window stored, so the gate is open."))});
  shell(s,"cost",function(n,p){
    n.appendChild(table(["Model","Effort","Kind","Steps","Share","Biggest part"],p.rows.map(function(r){
      var c=el("div");c.appendChild(el("span",null,Math.round(100*r.share)+"%"));c.appendChild(bar(100*r.share));
      return[r.model,r.effort||"-",r.kind,{num:r.steps},c,r.biggest]})));
    if(p.grid.length){n.appendChild(el("div","sub","What one step costs on average (units, main conversations):"));
      n.appendChild(table(["Model","Effort","Steps","Units/step","vs cheapest"],p.grid.map(function(g){return[g.model||"unknown",g.effort||"-",{num:g.steps},{num:g.units_per_step==null?"-":fmt(g.units_per_step)},g.times_cheapest==null?"-":g.times_cheapest.toFixed(1)+"x"]})))}
    n.appendChild(el("div","sub","Newest "+p.read+" session(s); prices as of "+p.prices_read+"."))});
  shell(s,"rework",function(n,p){
    n.appendChild(table(["Model","Effort","Sessions","Steps","Redo steps","Redo share"],p.rows.map(function(r){return[r.model,r.effort||"-",{num:r.sessions},{num:r.steps},{num:r.redo_steps},r.redo_share==null?"-":Math.round(100*r.redo_share)+"%"]})));
    if(p.comparison)n.appendChild(el("p",null,p.comparison));
    if(p.left_out)n.appendChild(el("div","sub",p.left_out))});
  shell(s,"startup",function(n,p){
    p.tools.forEach(function(t){
      n.appendChild(el("p",null,t.tool+": "+(t.measured_start?"newest session started at ~"+fmt(t.measured_start)+" tokens (measured). ":"no measured start. ")+"~"+fmt(t.known)+" sized from local files"+(t.rest!=null?", ~"+fmt(t.rest)+" the tool's own prompt and connector lists.":".")));
      if(t.parts.length)n.appendChild(table(["Part","Name","Approx. tokens"],t.parts.map(function(x){return[x.kind,x.name,{num:"~"+fmt(x.tokens)}]})));
      if(t.price)n.appendChild(el("div","sub","Re-read over "+t.price.steps+" steps on "+t.price.model+": about ~"+fmt(Math.round(t.price.units))+" new-input-token units (estimate)."))})});
  shell(s,"watch",function(n,p){
    n.appendChild(el("p",null,"Warns when history passes ~"+fmt(p.limits.history_tokens)+" tokens, a file is read "+p.limits.repeat_reads+"+ times, or a plan window is "+p.limits.pace_points+"+ points over its line."));
    n.appendChild(table(["Kind","Count","Latest"],p.kinds.map(function(k){return[k.kind,{num:k.count},k.last_what]})));
    })
}
"""
