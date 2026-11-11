"""`tokenstretch dashboard`: a read-only local web page with live usage, warnings, and doctor results.

Security model (see docs/DETAILS.md):
  * binds 127.0.0.1 only; any other --host is refused;
  * every request needs the random per-run token in the URL path, else 403;
  * the Host header must be 127.0.0.1 or localhost (stops DNS rebinding), else 403;
  * no CORS headers, a strict Content-Security-Policy, and no external assets: one inline page.
Read-only: it never writes a file. Data comes from adapters through the same code as
status / watch / doctor / report --weekly. Nothing here opens an outbound connection.
"""
from __future__ import annotations

import hmac
import json
import secrets
import socket
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .context import Context
from .identity import CLI_NAME, PRODUCT_NAME, VERSION
from .setup import LONG_HISTORY

LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8765
FOOTER = "Runs only on this machine. Nothing is sent anywhere."


def _local(epoch: float | None) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch).astimezone().strftime("%b %d %I:%M %p %Z")


class DashboardState:
    """Builds the JSON the page polls, in a background thread; requests only read the last snapshot.

    Three parts refresh on their own clocks: live sessions + watch warnings (every pass),
    usage/setup/bench (every usage_ttl s, incremental log reads), and doctor (every doctor_ttl s,
    or on request). Each finished part is published at once, so the first poll is never blocked.
    """

    def __init__(self, ctx: Context, *, adapters=None, now: Callable[[], float] = time.time,
                 doctor_ttl: float = 300.0, usage_ttl: float = 30.0, tz=None, weeks: int = 8,
                 interval: float = 5.0) -> None:
        from .logcache import LogCache
        from .setup import detected_adapters
        from .watch import Watcher
        self.ctx = ctx
        self.adapters = adapters if adapters is not None else detected_adapters(ctx)
        self.now = now
        self.doctor_ttl = doctor_ttl
        self.usage_ttl = usage_ttl
        self.tz = tz
        self.weeks = max(1, int(weeks))
        self.interval = interval
        self.cache = LogCache()
        self.watcher = Watcher(ctx, adapters=self.adapters, now=now)
        self.feed: list[dict] = []
        self.live: dict | None = None
        self.usage: dict | None = None
        self._doctor: dict | None = None
        self._doctor_at = 0.0
        self._usage_at = 0.0
        self.errors: dict[str, str] = {}
        self.started_at = now()
        self._latest: dict | None = None
        self._latest_json: bytes | None = None
        self._pub_lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._doctor_wanted = False
        self._thread: threading.Thread | None = None

    # ---- helpers ----------------------------------------------------------------
    def _scrub(self, text) -> str:
        from .mask import mask_text
        s = mask_text(str(text if text is not None else ""))
        home = str(self.ctx.home)
        return s.replace(home + "/", "~/").replace(home, "~") if home not in ("", "/") else s

    def _short(self, path: str) -> str:
        from .setup import _short
        return _short(path, self.ctx.home)

    # ---- parts (worker thread, or snapshot() in tests) -------------------------
    def build_doctor(self) -> dict:
        from .setup import collect_findings, install_offer
        t = self.now()
        findings = collect_findings(self.ctx, self.adapters)
        counts = {k: 0 for k in ("high", "med", "low", "info")}
        rows = []
        for f in findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
            rows.append({"severity": f.severity, "id": f.id, "tool": f.adapter_id, "category": f.category,
                         "message": self._scrub(f.message), "fix": self._scrub(f.fix)})
        tools = []
        for ad in self.adapters:
            tools += [{**x, "note": self._scrub(x.get("note", ""))} for x in ad.token_tools(self.ctx) or []]
        self._doctor = {"refreshed_at": t, "refreshed_local": _local(t), "counts": counts, "findings": rows,
                        "tools": tools, "telemetry_findings": [r for r in rows if r["id"] in ("P01", "P02")],
                        "install_offer": install_offer(self.ctx)}
        self._doctor_at = t
        return self._doctor

    def doctor(self, force: bool = False) -> dict:
        if force or self._doctor is None or self.now() - self._doctor_at >= self.doctor_ttl:
            self.build_doctor()
        return self._doctor

    def build_live(self) -> dict:
        from .watch import _short_project
        from .weekly import sessions
        t = self.now()
        active = self.watcher.cfg.active_s
        sess = []
        for s in sessions(self.ctx, self.adapters, limit=10, max_age_s=active):
            hist = s["history_tokens"]
            sess.append({"tool": s["tool"], "session": s["session"], "project": _short_project(s["project"]),
                         "model": s["model"], "history_tokens": hist,
                         "pct": round(min(100.0, 100.0 * hist / LONG_HISTORY), 1), "long": hist > LONG_HISTORY,
                         "last_write": s["last_write"], "last_write_local": _local(s["last_write"]),
                         "active": (s["last_write"] or 0) >= t - active, "clear_hint": s.get("clear_hint", "")})
        for w in self.watcher.scan():
            self.feed.insert(0, {"seen_at": t, "seen_local": _local(t), "tool": w.adapter, "session": w.session,
                                 "project": _short_project(w.project), "kind": w.key[-1],
                                 "what": self._scrub(w.what), "why": w.why, "fix": self._scrub(w.fix)})
        del self.feed[200:]
        self.live = {"sessions": sess, "warnings": list(self.feed[:50]), "at": t}
        return self.live

    def build_usage(self) -> dict:
        from .adapters import REGISTRY
        from .setup import applied_changes, build_plan
        from .weekly import _top_checker, aggregate, bench_results, read_records, setup_date, since_setup
        t = self.now()
        now_dt = datetime.fromtimestamp(t, timezone.utc)
        recs = read_records(self.ctx, days=self.weeks * 7 + 7, cache=self.cache)
        agg = aggregate(recs, weeks=self.weeks, now=now_dt, tz=self.tz)
        runs = []
        for m in applied_changes(self.ctx):
            runs.append({"created_at": m.get("created_at"), "title": m.get("title"),
                         "files": [{"path": self._short(f["path"]), "state": f["state"], "reason": f["reason"]}
                                   for f in m["files"]]})
        undo = any(f["state"] == "applied" for r in runs for f in r["files"])
        items, _ = build_plan(self.ctx, self.adapters)
        self.usage = {"agg": agg, "records": len(recs),
                      "setup": {"runs": runs[:10], "undo_available": undo, "undo_command": f"{CLI_NAME} undo",
                                "pending": [i.title for i in items], "setup_command": f"{CLI_NAME} setup"},
                      "since_setup": since_setup(recs, setup_date(self.ctx), now=now_dt, is_top=_top_checker(REGISTRY)),
                      "bench": bench_results(self.ctx), "at": t}
        self._usage_at = t
        return self.usage

    # ---- compose + publish -----------------------------------------------------
    def compose(self) -> dict:
        t = self.now()
        live, usage, doc = self.live, self.usage, self._doctor
        pending = [name for name, part in (("live sessions", live), ("usage logs", usage), ("doctor", doc))
                   if part is None]
        sess = live["sessions"] if live else []
        biggest = max(sess, key=lambda s: s["history_tokens"], default=None)
        agg = usage["agg"] if usage else None
        week = agg["this_week"] if agg else None
        updated = max([p["at"] for p in (live, usage) if p] + ([doc["refreshed_at"]] if doc else []), default=None)
        return {
            "product": PRODUCT_NAME, "version": VERSION, "generated_at": t, "generated_local": _local(t),
            "ready": not pending, "pending": pending, "warming_up": bool(pending),
            "updated_at": updated, "updated_local": _local(updated) if updated else "",
            "doctor_refreshing": self._doctor_wanted, "errors": dict(self.errors),
            "limit": LONG_HISTORY, "detected": [a.display_name for a in self.adapters if a.id != "host"],
            "overview": {
                "tokens_today": agg["today"]["total_tokens"] if agg else None,
                "tokens_week": week["total_tokens"] if week else None,
                "cost_today": agg["today"]["cost"] if agg else None, "cost_week": week["cost"] if week else None,
                "active_sessions": sum(1 for s in sess if s["active"]) if live else None,
                "biggest_history": ({"tokens": biggest["history_tokens"], "tool": biggest["tool"],
                                     "project": biggest["project"]} if biggest else None),
                "warnings": len(self.feed),
            },
            "sessions": sess if live else None,
            "warnings": live["warnings"] if live else None,
            "models": week["models"] if week else None,
            "weekly": ([{"week": w["week"], "total_tokens": w["total_tokens"], "tokens": w["tokens"],
                         "records": w["records"], "cost": w["cost"]} for w in reversed(agg["weeks"])]
                       if agg else None),
            "doctor": {k: doc[k] for k in ("refreshed_local", "counts", "findings")} if doc else None,
            "tools": ({"installed": doc["tools"], "telemetry_findings": doc["telemetry_findings"],
                       "install_offer": doc["install_offer"]} if doc else None),
            "setup": usage["setup"] if usage else None,
            "since_setup": usage["since_setup"] if usage else None,
            "bench": usage["bench"] if usage else None,
            "footer": FOOTER,
        }

    def publish(self) -> dict:
        state = self.compose()
        blob = json.dumps(state, default=str).encode()
        with self._pub_lock:
            self._latest, self._latest_json = state, blob
        return state

    def latest_json(self) -> bytes:
        """The last published snapshot. Never builds anything (safe on the HTTP thread)."""
        with self._pub_lock:
            blob = self._latest_json
        return blob if blob is not None else json.dumps(self.compose(), default=str).encode()

    def _run(self, name: str, fn) -> None:
        import gc
        # Parsing creates many long-lived objects; a full GC pass over them holds the GIL long
        # enough to stall the HTTP thread. Pause GC while building, then freeze what survived.
        was = gc.isenabled()
        gc.disable()
        try:
            fn()
            self.errors.pop(name, None)
        except Exception as exc:  # keep serving the last good part
            self.errors[name] = f"{type(exc).__name__}: {exc}"
        finally:
            if was:
                gc.enable()
            gc.freeze()

    def refresh(self, *, force_doctor: bool = False) -> None:
        """One worker pass: live every time, usage and doctor when due. Publishes after each part."""
        with self._build_lock:
            self._run("live sessions", self.build_live)
            self.publish()
            if self.usage is None or self.now() - self._usage_at >= self.usage_ttl:
                self._run("usage logs", self.build_usage)
                self.publish()
            want = force_doctor or self._doctor_wanted
            if want or self._doctor is None or self.now() - self._doctor_at >= self.doctor_ttl:
                self._doctor_wanted = False
                self._run("doctor", self.build_doctor)
                self.publish()

    def snapshot(self, *, force_doctor: bool = False) -> dict:
        """Synchronous full build (tests, static render). The server never calls this."""
        self.refresh(force_doctor=force_doctor)
        with self._pub_lock:
            return self._latest

    # ---- background worker -------------------------------------------------------
    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="tokenstretch-dashboard", daemon=True)
            self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._wake.wait(self.interval)
            self._wake.clear()

    def request_doctor(self) -> None:
        self._doctor_wanted = True
        self.publish()
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()


def build_state(ctx: Context, **kw) -> dict:
    return DashboardState(ctx, **kw).snapshot()


# ---- HTML -----------------------------------------------------------------------------

_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>__TITLE__</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c1f24;--mut:#69707d;--line:#e3e6ea;--acc:#3a6ff7;--ok:#1a9b5a;--warn:#c27a00;--bad:#d1384b;--bar:#e9edf3}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--card:#171b21;--fg:#e7eaee;--mut:#9aa3af;--line:#262c35;--acc:#6f94ff;--ok:#3ccf87;--warn:#f0a83a;--bad:#ff6b7d;--bar:#232a33}}
:root.light{--bg:#f6f7f9;--card:#fff;--fg:#1c1f24;--mut:#69707d;--line:#e3e6ea;--acc:#3a6ff7;--ok:#1a9b5a;--warn:#c27a00;--bad:#d1384b;--bar:#e9edf3}
:root.dark{--bg:#0f1216;--card:#171b21;--fg:#e7eaee;--mut:#9aa3af;--line:#262c35;--acc:#6f94ff;--ok:#3ccf87;--warn:#f0a83a;--bad:#ff6b7d;--bar:#232a33}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:16px 24px;border-bottom:1px solid var(--line);background:var(--card);position:sticky;top:0;z-index:2}
header h1{font-size:18px;margin:0}header .sp{flex:1}.pill{font-size:12px;padding:2px 8px;border-radius:99px;background:var(--bar);color:var(--mut)}
.live{color:var(--ok)}.live::before{content:"";display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok);margin-right:6px;vertical-align:1px}
.stale{color:var(--bad)}.stale::before{background:var(--bad)}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:4px 10px;cursor:pointer}button:hover{border-color:var(--acc)}
main{max-width:1200px;margin:0 auto;padding:20px 24px 40px;display:grid;grid-template-columns:repeat(12,1fr);gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;min-width:0}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:var(--mut);margin:0 0 12px}
.c3{grid-column:span 3}.c4{grid-column:span 4}.c5{grid-column:span 5}.c6{grid-column:span 6}.c7{grid-column:span 7}.c8{grid-column:span 8}.c12{grid-column:span 12}
@media (max-width:900px){.c3,.c4,.c5,.c6,.c7,.c8{grid-column:span 12}}
.big{font-size:26px;font-weight:650;letter-spacing:-.01em}.sub{color:var(--mut);font-size:12px}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{font-size:12px;color:var(--mut);font-weight:600}
td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
.hbar{height:8px;background:var(--bar);border-radius:99px;overflow:hidden;margin-top:4px;min-width:90px}.hbar>i{display:block;height:100%;background:var(--acc);border-radius:99px}
.hbar.over>i{background:var(--bad)}
.row{display:flex;align-items:center;gap:10px;margin:6px 0}.row .lbl{width:38%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.row .hbar{flex:1;margin:0}.row .val{width:90px;text-align:right;color:var(--mut);font-variant-numeric:tabular-nums}
.cols{display:flex;align-items:flex-end;gap:10px;height:160px;padding-top:8px}.col{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%;min-width:0}
.col i{display:block;width:100%;max-width:46px;background:var(--acc);border-radius:6px 6px 2px 2px;min-height:2px}.col span{font-size:11px;color:var(--mut);margin-top:4px;white-space:nowrap}
.feed{max-height:340px;overflow:auto}.w{border-left:3px solid var(--warn);padding:6px 10px;margin:0 0 10px;background:var(--bg);border-radius:0 8px 8px 0}
.w b{display:block}.w .fix{color:var(--ok)}
.sev{font-size:11px;font-weight:700;padding:1px 7px;border-radius:99px;text-transform:uppercase}.sev.high{background:var(--bad);color:#fff}.sev.med{background:var(--warn);color:#fff}.sev.low,.sev.info{background:var(--bar);color:var(--mut)}
.on{color:var(--bad);font-weight:600}.off{color:var(--ok);font-weight:600}
code{background:var(--bar);padding:1px 5px;border-radius:4px;font-size:12px}.muted{color:var(--mut)}.empty{color:var(--mut);font-style:italic}
.note{font-size:12px;color:var(--warn)}
footer{grid-column:span 12;text-align:center;color:var(--mut);font-size:12px;padding-top:8px}
</style></head>
<body>
<header><h1>__TITLE_TEXT__</h1><span class="pill" id="ver"></span><span class="sp"></span>
<span id="status" class="pill">loading</span><button id="theme" type="button">Theme</button></header>
<main>
<section class="card c3"><h2>Tokens today</h2><div class="big" id="o-today">-</div><div class="sub" id="o-today-c"></div></section>
<section class="card c3"><h2>Tokens this week</h2><div class="big" id="o-week">-</div><div class="sub" id="o-week-c"></div></section>
<section class="card c3"><h2>Active sessions</h2><div class="big" id="o-active">-</div><div class="sub" id="o-warn"></div></section>
<section class="card c3"><h2>Biggest history</h2><div class="big" id="o-big">-</div><div class="sub" id="o-big-s"></div></section>
<section class="card c8"><h2>Live sessions</h2><div id="sessions"></div></section>
<section class="card c4"><h2>Live warnings</h2><div class="feed" id="warnings"></div></section>
<section class="card c6"><h2>Models this week</h2><div id="models"></div></section>
<section class="card c6"><h2>Weekly trend (total tokens)</h2><div id="weekly"></div></section>
<section class="card c7"><h2>Doctor <button id="redoc" type="button" style="float:right;text-transform:none">Re-run doctor</button></h2><div class="sub" id="doc-when"></div><div id="doctor"></div></section>
<section class="card c5"><h2>Token-saving tools &amp; telemetry</h2><div id="tools"></div></section>
<section class="card c6"><h2>Setup status</h2><div id="setup"></div></section>
<section class="card c6"><h2>Since setup &amp; bench</h2><div id="since"></div><div id="bench"></div></section>
<footer>__FOOTER__ <span id="updated"></span></footer>
</main>
<script id="state" type="application/json">__STATE__</script>
<script>
(function(){
"use strict";
var INTERVAL=__INTERVAL__, STATIC=__STATIC__;
function $(id){return document.getElementById(id)}
function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=String(text);return e}
function clear(n){while(n.firstChild)n.removeChild(n.firstChild);return n}
function fmt(n){n=Number(n||0);if(n>=1e9)return(n/1e9).toFixed(2)+"B";if(n>=1e6)return(n/1e6).toFixed(2)+"M";if(n>=1e3)return Math.round(n/1e3)+"K";return String(n)}
function cost(c){if(!c||c.usd==null)return"cost not reported by the logs";return"$"+Number(c.usd).toFixed(2)+" ("+c.label+")"}
function bar(pct,over){var b=el("div","hbar"+(over?" over":""));var i=el("i");i.style.width=Math.max(0,Math.min(100,pct))+"%";b.appendChild(i);return b}
function table(head,rows){var t=el("table"),tr=el("tr");head.forEach(function(h){tr.appendChild(el("th",null,h))});t.appendChild(tr);rows.forEach(function(r){var tr=el("tr");r.forEach(function(c){var td=el("td");if(c instanceof Node)td.appendChild(c);else if(c&&c.num!=null){td.className="num";td.textContent=c.num}else td.textContent=c==null?"":String(c);tr.appendChild(td)});t.appendChild(tr)});return t}
function empty(n,msg){n.appendChild(el("div","empty",msg))}
function wait(n,msg){clear(n);n.appendChild(el("div","empty",msg||"Warming up: reading local logs..."));return n}
function render(s){
  $("ver").textContent="v"+s.version+(s.detected&&s.detected.length?" · "+s.detected.join(", "):"");
  var o=s.overview,n;
  $("o-today").textContent=o.tokens_today==null?"…":fmt(o.tokens_today);$("o-today-c").textContent=o.tokens_today==null?"reading usage logs":cost(o.cost_today);
  $("o-week").textContent=o.tokens_week==null?"…":fmt(o.tokens_week);$("o-week-c").textContent=o.tokens_week==null?"reading usage logs":cost(o.cost_week);
  $("o-active").textContent=o.active_sessions==null?"…":o.active_sessions;$("o-warn").textContent=o.warnings+" warning(s) since this page started";
  if(o.biggest_history){$("o-big").textContent=fmt(o.biggest_history.tokens);$("o-big-s").textContent=o.biggest_history.tool+" · "+o.biggest_history.project+(o.biggest_history.tokens>s.limit?" · over "+fmt(s.limit):"")}
  else{$("o-big").textContent=s.sessions==null?"…":"-";$("o-big-s").textContent=s.sessions==null?"":"no active sessions"}
  n=clear($("sessions"));
  if(s.sessions==null)wait(n);
  else if(!s.sessions.length)empty(n,"No sessions written in the last 2 hours.");
  else n.appendChild(table(["Tool","Project","Model","History re-sent per step","Last activity"],s.sessions.map(function(x){
    var c=el("div");c.appendChild(el("span",x.long?"on":null,fmt(x.history_tokens)+(x.long?" · over "+fmt(s.limit)+": "+(x.clear_hint||"start fresh")+" at the next break":"")));c.appendChild(bar(x.pct,x.long));
    return[x.tool,x.project,x.model,c,(x.active?"● ":"")+x.last_write_local]})));
  n=clear($("warnings"));
  if(s.warnings==null)wait(n);
  else if(!s.warnings.length)empty(n,"No waste spotted in active sessions.");
  else s.warnings.forEach(function(w){var d=el("div","w");d.appendChild(el("b",null,w.tool+" "+w.session+" · "+w.project));d.appendChild(el("div",null,w.what));d.appendChild(el("div","muted","Why: "+w.why));d.appendChild(el("div","fix","Fix: "+w.fix));d.appendChild(el("div","sub",w.seen_local));n.appendChild(d)});
  n=clear($("models"));
  if(s.models==null)wait(n);
  else if(!s.models.length)empty(n,"No usage logged this week.");
  else{var mx=Math.max.apply(null,s.models.map(function(m){return m.total_tokens}).concat([1]));
  s.models.forEach(function(m){var r=el("div","row");r.appendChild(el("div","lbl",m.model));r.appendChild(bar(100*m.total_tokens/mx));r.appendChild(el("div","val",fmt(m.total_tokens)));r.title=m.records+" steps; "+cost(m.cost);n.appendChild(r)})}
  n=clear($("weekly"));
  if(s.weekly==null)wait(n);
  else{var wx=Math.max.apply(null,s.weekly.map(function(w){return w.total_tokens}).concat([1]));var cols=el("div","cols");
  s.weekly.forEach(function(w){var c=el("div","col"),i=el("i");i.style.height=(100*w.total_tokens/wx)+"%";i.title=fmt(w.total_tokens)+" tokens; "+cost(w.cost);c.appendChild(el("span",null,fmt(w.total_tokens)));c.appendChild(i);c.appendChild(el("span",null,w.week.slice(5)));cols.appendChild(c)});n.appendChild(cols);
  n.appendChild(el("div","sub","Weeks start Monday. Cost appears only where the logs report it; nothing is estimated."))}
  var d=s.doctor;n=clear($("doctor"));
  if(d==null){$("doc-when").textContent="";wait(n,"Running doctor (read-only)...")}
  else{$("doc-when").textContent="Last run "+d.refreshed_local+" · "+d.counts.high+" high, "+d.counts.med+" med, "+d.counts.low+" low, "+d.counts.info+" info. Read-only."+(s.doctor_refreshing?" Re-running...":"");
  if(!d.findings.length)empty(n,"Doctor found nothing to fix.");
  else n.appendChild(table(["","Check","What it means","Fix"],d.findings.slice(0,40).map(function(f){return[el("span","sev "+f.severity,f.severity),f.id+" · "+f.tool,f.message,f.fix]})))}
  n=clear($("tools"));var t=s.tools;
  if(t==null)wait(n,"Waiting for doctor...");
  else{if(!t.installed.length)empty(n,"No third-party token savers detected.");
  else n.appendChild(table(["Tool","Telemetry","Note"],t.installed.map(function(x){return[x.name,el("span",x.telemetry==="on"?"on":(x.telemetry==="off"?"off":"muted"),x.telemetry),x.note]})));
  if(t.telemetry_findings.length){n.appendChild(el("div","note",t.telemetry_findings.length+" privacy finding(s) about telemetry or cloud sends; see Doctor."))}
  if(t.install_offer.length){var p=el("div","sub");p.textContent="Pinned installs setup can offer (you confirm; uses the network): "+t.install_offer.map(function(x){return x.name+" "+x.version}).join(", ");n.appendChild(p)}}
  n=clear($("setup"));var su=s.setup;
  if(su==null)wait(n);
  else{if(!su.runs.length)empty(n,"Nothing applied yet.");
  su.runs.forEach(function(r){n.appendChild(el("div",null,(r.created_at||"")+" · "+r.title));r.files.forEach(function(f){n.appendChild(el("div","sub","["+f.state+"] "+f.path+" — "+f.reason))})});
  var cmd=el("p");if(su.undo_available){cmd.appendChild(document.createTextNode("Undo available: run "));cmd.appendChild(el("code",null,su.undo_command))}
  if(su.pending.length){cmd.appendChild(document.createTextNode((su.undo_available?" · ":"")+su.pending.length+" safe fix(es) not applied yet: run "));cmd.appendChild(el("code",null,su.setup_command))}
  n.appendChild(cmd);n.appendChild(el("div","sub","This page never changes files; it only shows the commands."))}
  n=clear($("since"));var ss=s.since_setup;
  if(s.setup==null)wait(n);
  else if(!ss)empty(n,"No setup run recorded yet, so there is no before/after.");
  else{n.appendChild(el("div","note",ss.label));n.appendChild(table(["","Steps","Avg input/step","Avg output/step","Top-model share"],["before","after"].map(function(k){var x=ss[k];return[k,{num:x.steps},{num:x.avg_input_per_step==null?"-":fmt(x.avg_input_per_step)},{num:x.avg_output_per_step==null?"-":fmt(x.avg_output_per_step)},x.top_model_share==null?"-":Math.round(x.top_model_share*100)+"%"]})))}
  n=clear($("bench"));
  if(s.bench==null){}
  else if(!s.bench.length)empty(n,"No measured bench on this machine (tokenstretch bench --prepare, then --measure).");
  else{n.appendChild(el("div","sub","Measured bench runs (real runs on this machine):"));n.appendChild(table(["Bench","Side","Input","Cache read","Output","Cost"],[].concat.apply([],s.bench.map(function(b){return["before","after"].map(function(k){var x=b[k]||{};return[b.id,k,{num:fmt(x.input_uncached)},{num:fmt(x.cache_read)},{num:fmt(x.output)},x.cost_usd==null?"not reported":"$"+Number(x.cost_usd).toFixed(4)]})}))))}
  $("updated").textContent=s.updated_local?"Last updated "+s.updated_local+".":"Warming up.";
  var errs=Object.keys(s.errors||{});if(errs.length)$("updated").textContent+=" Could not read: "+errs.join(", ")+" (showing the last good data).";
}
function statusText(s){return s.warming_up?"warming up: "+s.pending.join(", ")+"...":"live · updated "+(s.updated_local||"").replace(/^\w+ \d+ /,"")}
function setStatus(ok,msg){var st=$("status");st.textContent=msg;st.className="pill "+(ok?"live":"live stale")}
var state=JSON.parse($("state").textContent||"null");if(state)render(state);
if(STATIC){setStatus(true,"static snapshot");$("redoc").disabled=true}
function poll(){fetch("api/state",{cache:"no-store",credentials:"same-origin"}).then(function(r){if(!r.ok)throw new Error(r.status);return r.json()}).then(function(s){render(s);setStatus(true,statusText(s))}).catch(function(){setStatus(false,"not connected: is `tokenstretch dashboard` still running?")})}
if(!STATIC){poll();setTimeout(poll,1000);setTimeout(poll,2500);setInterval(poll,INTERVAL);
  $("redoc").addEventListener("click",function(){var b=$("redoc");b.disabled=true;b.textContent="Running...";fetch("api/doctor",{method:"POST",cache:"no-store"}).then(function(r){return r.json()}).then(render).catch(function(){}).then(function(){setTimeout(function(){b.disabled=false;b.textContent="Re-run doctor"},3000)})})}
$("theme").addEventListener("click",function(){var r=document.documentElement;var dark=r.classList.contains("dark")||(!r.classList.contains("light")&&window.matchMedia("(prefers-color-scheme: dark)").matches);r.classList.remove("dark","light");r.classList.add(dark?"light":"dark")});
})();
</script>
</body></html>
"""


def _json_for_script(data) -> str:
    return (json.dumps(data, default=str).replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def render_page(state: dict | None = None, *, interval: float = 5.0, static: bool = False) -> str:
    """The whole page: inline CSS and JS, no external resources. `static` embeds state and stops polling."""
    import html
    title = f"{PRODUCT_NAME} dashboard"
    return (_PAGE.replace("__TITLE__", html.escape(title)).replace("__TITLE_TEXT__", html.escape(title))
            .replace("__INTERVAL__", str(int(interval * 1000))).replace("__STATIC__", "true" if static else "false").replace("__FOOTER__", html.escape(FOOTER))
            .replace("__STATE__", _json_for_script(state) if state is not None else "null"))


# ---- server ------------------------------------------------------------------------------

CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
       "img-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _make_handler(state: DashboardState, token: str, interval: float):
    page = render_page(None, interval=interval).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        server_version = PRODUCT_NAME
        sys_version = ""

        def log_message(self, fmt, *args):  # quiet: never echo the token-bearing path
            return

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", CSP)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _deny(self, why: str) -> None:
            self._send(403, f"403 forbidden: {why}\n".encode(), "text/plain; charset=utf-8")

        def _route(self) -> str | None:
            """Path after the token, or None after sending 403."""
            port = self.server.server_address[1]
            host = (self.headers.get("Host") or "").strip().lower()
            if host not in {f"127.0.0.1:{port}", f"localhost:{port}", "127.0.0.1", "localhost"}:
                self._deny("bad Host header")
                return None
            origin = self.headers.get("Origin")
            if origin and origin.lower() not in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}:
                self._deny("cross-origin request")
                return None
            path = self.path.split("?", 1)[0].split("#", 1)[0]
            parts = path.lstrip("/").split("/", 1)
            if not parts[0] or not hmac.compare_digest(parts[0].encode(), token.encode()):
                self._deny("missing or wrong token")
                return None
            return "/" + (parts[1] if len(parts) > 1 else "") if len(parts) > 1 else ""

        def do_GET(self):
            rest = self._route()
            if rest is None:
                return
            if rest == "":
                self.send_response(302)
                self.send_header("Location", f"/{token}/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if rest == "/":  # static page; it never builds state
                return self._send(200, page, "text/html; charset=utf-8")
            if rest == "/api/state":
                return self._send(200, state.latest_json(), "application/json")
            self._send(404, b"not found\n", "text/plain; charset=utf-8")

        do_HEAD = do_GET

        def do_POST(self):
            rest = self._route()
            if rest is None:
                return
            if rest == "/api/doctor":  # ask the worker to re-run doctor; answer with the current snapshot
                state.request_doctor()
                return self._send(200, state.latest_json(), "application/json")
            self._send(404, b"not found\n", "text/plain; charset=utf-8")

        def do_OPTIONS(self):  # no CORS preflight support, on purpose
            if self._route() is None:
                return
            self._send(405, b"method not allowed\n", "text/plain; charset=utf-8")

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def make_server(state: DashboardState, *, host: str = LOOPBACK, port: int = DEFAULT_PORT,
                token: str | None = None, interval: float = 5.0) -> tuple[_Server, str]:
    """Bind 127.0.0.1 only (ValueError otherwise). Busy port → a free one. Returns (server, token)."""
    if host not in (LOOPBACK, "localhost"):
        raise ValueError(f"refusing to bind {host!r}: the dashboard only listens on {LOOPBACK}")
    token = token or secrets.token_urlsafe(24)
    handler = _make_handler(state, token, interval)
    try:
        srv = _Server((LOOPBACK, port), handler)
    except OSError:
        srv = _Server((LOOPBACK, 0), handler)
    assert srv.server_address[0] == LOOPBACK and srv.socket.family == socket.AF_INET
    return srv, token


def run_dashboard(ctx: Context, *, host: str = LOOPBACK, port: int = DEFAULT_PORT, open_browser: bool = True,
                  interval: float = 5.0, weeks: int = 8, out: Callable[[str], None] = print) -> int:
    try:
        state = DashboardState(ctx, weeks=weeks, interval=interval)
        srv, token = make_server(state, host=host, port=port, interval=interval)
    except ValueError as exc:
        out(str(exc))
        return 2
    state.start()  # builds in the background; the page shows "warming up" until the first parts land
    bound = srv.server_address[1]
    url = f"http://{LOOPBACK}:{bound}/{token}/"
    out(f"{PRODUCT_NAME} dashboard (read-only). {FOOTER}")
    if bound != port:
        out(f"Port {port} was busy, so it's using {bound}.")
    out(f"Open: {url}")
    out("The link has a one-time key; only this run accepts it. Ctrl-C to stop.")
    if open_browser:
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:  # a missing browser is not an error
            pass
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        out("")
        out("Dashboard stopped.")
    finally:
        state.stop()
        srv.server_close()
    return 0
