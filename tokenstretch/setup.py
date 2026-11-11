"""`setup`, `status`, and `report`: the automated, explained path through doctor + installer.

Core only: every tool-specific path comes from an adapter (`setup_items`, `live_sessions`,
`effective_models`). Nothing here touches the network. The optional pinned installs go
through `installer.install`, which runs fetch steps only after a separate confirmation.
"""
from __future__ import annotations

import html
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .adapters import REGISTRY
from .adapters.base import SetupItem
from .context import Context
from .findings import SEVERITY_ORDER, Finding
from .identity import CLI_NAME, PRODUCT_NAME, STATE_DIRNAME, VERSION
from .installer import _now_stamp, apply_changes, kit_root, sha256_file
from .mask import mask_finding_evidence, mask_text


def _short_project(name: str) -> str:
    from .watch import _short_project as short
    return short(name)


LONG_HISTORY = 150_000
INSTALL_TOOL_IDS = ("rtk-oss", "caveman-skill")


# ---- doctor + plan ------------------------------------------------------------------

def detected_adapters(ctx: Context) -> list:
    return [ad for ad in REGISTRY if ad.detect(ctx)]


def collect_findings(ctx: Context, adapters=None) -> list[Finding]:
    findings: list[Finding] = []
    for ad in adapters if adapters is not None else detected_adapters(ctx):
        findings += ad.checks(ctx) + ad.privacy_checks(ctx)
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.id))
    for f in findings:
        f.evidence = mask_finding_evidence(f.evidence)
    return findings


def build_plan(ctx: Context, adapters=None) -> tuple[list[SetupItem], list[str]]:
    """Safe local fixes from every detected adapter. Two items never edit the same file."""
    items: list[SetupItem] = []
    notes: list[str] = []
    touched: set[str] = set()
    for ad in adapters if adapters is not None else detected_adapters(ctx):
        for item in ad.setup_items(ctx) or []:
            keys = []
            for ch in item.changes:
                p = Path(ch["path"])
                try:
                    keys.append(str(p.resolve()))
                except OSError:
                    keys.append(str(p))
            if any(k in touched for k in keys):
                notes.append(f"Skipped '{item.title}': another step already edits that file (same file or a link to it).")
                continue
            touched.update(keys)
            items.append(item)
    return items, notes


def install_offer(ctx: Context) -> list[dict]:
    """The optional pinned installs (network). Described from the catalog; nothing runs here."""
    from .catalog import clean_tools
    return [
        {"id": t.id, "name": t.name, "version": t.version, "commit": t.commit, "license": t.license_id,
         "telemetry": t.telemetry}
        for t in clean_tools() if t.id in INSTALL_TOOL_IDS
    ]


def _short(path: str, home: Path) -> str:
    try:
        return "~/" + str(Path(path).relative_to(home))
    except ValueError:
        return str(path)


def format_findings_summary(findings: list[Finding], limit: int = 8) -> list[str]:
    if not findings:
        return ["Doctor found nothing to fix."]
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    order = sorted(counts, key=lambda s: SEVERITY_ORDER.get(s, 9))
    lines = ["Doctor found " + ", ".join(f"{counts[s]} {s}" for s in order) + ":"]
    for f in findings[:limit]:
        lines.append(f"  {f.severity.upper():5} {f.id} [{f.adapter_id}] {mask_text(f.message)}")
    if len(findings) > limit:
        lines.append(f"  ... and {len(findings) - limit} more (run `{CLI_NAME} doctor` for all of them)")
    return lines


def format_plan(ctx: Context, items: list[SetupItem], notes: list[str] | None = None) -> list[str]:
    if not items:
        return ["Plan: nothing to change. Your setup already matches the safe defaults."]
    lines = [f"Plan ({len(items)} change{'s' if len(items) != 1 else ''}, all local, every file backed up first):"]
    for n, item in enumerate(items, 1):
        lines.append(f"  {n}. {item.title}")
        lines.append(f"     Why: {item.why}")
        for ch in item.changes:
            verb = "create" if ch.get("kind") == "create" else "edit"
            lines.append(f"     Changes: {verb} {_short(ch['path'], ctx.home)} (key: {ch.get('key', '-')}; {ch.get('reason', '')})")
        lines.append(f"     Undo: `{item.undo}`")
        for note in item.notes:
            lines.append(f"     Note: {note}")
    for note in notes or []:
        lines.append(f"  - {note}")
    return lines


# ---- apply ---------------------------------------------------------------------------

@dataclass
class SetupResult:
    mode: str  # "dry-run" | "applied" | "declined"
    run_id: str = ""
    items: list[dict] = field(default_factory=list)
    manifests: list[str] = field(default_factory=list)
    installs: dict | None = None
    report: str = ""


def apply_plan(ctx: Context, items: list[SetupItem], run_id: str, progress: Callable[[str], None]) -> list[str]:
    """Apply each item through the installer's backup+manifest writer, one manifest per step."""
    manifests = []
    for n, item in enumerate(items, 1):
        stamp = f"{run_id}-{n:02d}"
        applied = apply_changes(ctx, item.changes, stamp=stamp, profile="setup",
                                extra_manifest={"setup_run": run_id, "setup_item": item.id, "title": item.title})
        manifests.append(applied["manifest_path"])
        for rec in applied["manifest"]["files"]:
            where = _short(rec["path"], ctx.home)
            if rec.get("backup"):
                progress(f"✔ {item.done}, backed up {where} → {_short(str(Path(applied['backup_dir']) / rec['backup']), ctx.home)}")
            else:
                progress(f"✔ {item.done}, created {where} (undo deletes it)")
    return manifests


def run_installs(ctx: Context, run_id: str, progress: Callable[[str], None], runner=None) -> dict:
    from .installer import install
    from .runner import default_runner
    progress("… running pinned installs (rtk-oss with RTK_EDITION=oss, Caveman skill only); this uses the network")
    result = install(ctx, apply=True, runner=runner or default_runner(), tool_ids=set(INSTALL_TOOL_IDS),
                     manifest_stamp=f"{run_id}-90-installs",
                     extra_manifest={"setup_run": run_id, "setup_item": "pinned-installs",
                                     "title": "Pinned installs (rtk-oss, Caveman skill)"})
    if result.ok:
        progress(f"✔ pinned installs done; wrote {len(result.written)} file(s), all backed up")
    else:
        progress("✘ pinned installs stopped: " + "; ".join(result.messages[-2:]))
    return {"ok": result.ok, "exit_code": result.exit_code, "manifest": result.manifest_path,
            "messages": result.messages, "written": result.written}


def _ask(prompt: str, input_fn, out) -> bool:
    out(prompt)
    try:
        answer = input_fn()
    except EOFError:
        return False
    return answer.strip().lower() in {"y", "yes"}


def run_setup(ctx: Context, *, yes: bool = False, dry_run: bool = False, installs: bool | None = None,
              interactive: bool | None = None, input_fn=input, out: Callable[[str], None] = print,
              runner=None) -> SetupResult:
    """Doctor → plain-English plan → one confirmation → apply with backups → live progress."""
    if interactive is None:
        interactive = sys_stdin_isatty()
    adapters = detected_adapters(ctx)
    out(f"{PRODUCT_NAME} {VERSION} setup. Nothing leaves this machine; files are only changed after you say yes.")
    out("Detected: " + (", ".join(a.display_name for a in adapters) or "nothing"))
    out("")
    for line in format_findings_summary(collect_findings(ctx, adapters)):
        out(line)
    out("")
    items, notes = build_plan(ctx, adapters)
    for line in format_plan(ctx, items, notes):
        out(line)
    offer = install_offer(ctx)
    if offer and installs is not False:
        out("")
        out("Optional, asked separately because it downloads (network): pinned installs")
        for t in offer:
            out(f"  - {t['name']} {t['version']} [{t['license']}] commit {str(t['commit'] or '')[:7]}; telemetry: {t['telemetry']}")
    out("")
    result = SetupResult(mode="dry-run", items=[i.to_json() for i in items])
    if dry_run or (not yes and not interactive):
        why = "--dry-run" if dry_run else "no terminal to ask in, and no --yes"
        out(f"Dry run ({why}): nothing was changed. Run `{CLI_NAME} setup` in a terminal, or add --yes.")
        return result
    if items and not yes:
        if not _ask(f"Apply these {len(items)} change(s)? [y/N] ", input_fn, out):
            out("No changes made.")
            result.mode = "declined"
            return result
    run_id = _now_stamp()
    result.run_id = run_id
    result.mode = "applied"
    if items:
        result.manifests = apply_plan(ctx, items, run_id, out)
    do_installs = False
    if offer and installs is not False:
        if installs is True:
            do_installs = True
        elif interactive and not yes:
            do_installs = _ask("Also download and install the pinned tools above? This uses the network. [y/N] ", input_fn, out)
    if do_installs:
        result.installs = run_installs(ctx, run_id, out, runner=runner)
        if result.installs.get("manifest"):
            result.manifests.append(result.installs["manifest"])
    out("")
    if result.manifests:
        out(f"Done. To undo everything this setup changed: `{CLI_NAME} undo`")
    else:
        out("Nothing needed changing.")
    for item in items:
        for note in item.notes:
            out(f"Tip: {note}")
    out(f"See what's on now: `{CLI_NAME} status`. Full write-up: `{CLI_NAME} report`.")
    return result


def sys_stdin_isatty() -> bool:
    import sys
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


# ---- status -----------------------------------------------------------------------------

def _manifest_state(rec: dict) -> str:
    actual = sha256_file(Path(rec["path"]))
    if actual == rec.get("sha256_after"):
        return "applied"
    if rec.get("action") == "created" and actual is None:
        return "undone"
    if rec.get("action") == "modified" and actual == rec.get("sha256_before"):
        return "undone"
    return "changed since"


def applied_changes(ctx: Context, limit: int = 20) -> list[dict]:
    d = kit_root(ctx) / "manifests"
    if not d.is_dir():
        return []
    out = []
    for path in sorted(d.glob("*.json"), reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({
            "manifest": str(path),
            "created_at": data.get("created_at"),
            "setup_run": data.get("setup_run"),
            "title": data.get("title") or f"install ({data.get('profile', 'clean')})",
            "files": [{"path": r["path"], "action": r.get("action"), "reason": r.get("reason", ""),
                       "state": _manifest_state(r)} for r in data.get("files") or []],
        })
    return out


def current_models(ctx: Context, adapters=None) -> list[dict]:
    out = []
    for ad in adapters if adapters is not None else detected_adapters(ctx):
        models = ad.effective_models(ctx)
        if ad.id == "host" and not models:
            continue
        out.append({"adapter": ad.display_name, "models": [{"model": m, "from": w} for m, w in models]})
    return out


def live_sessions(ctx: Context, adapters=None, limit: int = 5) -> list[dict]:
    rows = []
    for ad in adapters if adapters is not None else detected_adapters(ctx):
        rows += ad.live_sessions(ctx, limit=limit) or []
    rows.sort(key=lambda r: r.get("last_write") or 0, reverse=True)
    for r in rows[:limit]:
        r["long"] = (r.get("history_tokens") or 0) > LONG_HISTORY
        r["last_write_local"] = datetime.fromtimestamp(r["last_write"]).astimezone().strftime("%b %d %I:%M %p %Z") \
            if r.get("last_write") else ""
    return rows[:limit]


def collect_status(ctx: Context) -> dict:
    adapters = detected_adapters(ctx)
    return {
        "applied": applied_changes(ctx),
        "models": current_models(ctx, adapters),
        "sessions": live_sessions(ctx, adapters),
    }


def _k(n: int | None) -> str:
    return f"{round((n or 0) / 1000)}K"


def format_status(ctx: Context, status: dict) -> list[str]:
    lines = [f"{PRODUCT_NAME} status (read-only; local files and logs only)", "", "Applied by setup/install:"]
    if not status["applied"]:
        lines.append(f"  nothing yet (run `{CLI_NAME} setup`)")
    for m in status["applied"][:10]:
        lines.append(f"  {m['created_at']}  {m['title']}")
        for f in m["files"]:
            lines.append(f"    [{f['state']}] {_short(f['path'], ctx.home)} — {f['reason']}")
    lines += ["", "Default models:"]
    if not status["models"]:
        lines.append("  no supported agent detected")
    for row in status["models"]:
        if row["models"]:
            for m in row["models"]:
                lines.append(f"  {row['adapter']}: {m['model']} (from {_short(m['from'], ctx.home)})")
        else:
            lines.append(f"  {row['adapter']}: not set (the tool picks one by plan)")
    lines += ["", "Newest sessions (history re-read on each step):"]
    if not status["sessions"]:
        lines.append("  no local session logs found")
    for s in status["sessions"]:
        flag = f"  ⚠ over {_k(LONG_HISTORY)}: {s.get('clear_hint', 'start fresh')} at the next task break" if s["long"] else ""
        lines.append(f"  {s['last_write_local']}  {_short_project(s['project'])}  {s['model']}  ~{_k(s['history_tokens'])}{flag}")
    return lines


# ---- report -------------------------------------------------------------------------------

_CSS = """
body{font:15px/1.5 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;max-width:960px;margin:2em auto;padding:0 1em;color:#1d1d1f}
h1{font-size:1.6em}h2{margin-top:1.8em;border-bottom:1px solid #ddd;padding-bottom:.2em}
table{border-collapse:collapse;width:100%}td,th{border:1px solid #ddd;padding:.35em .5em;text-align:left;vertical-align:top}
th{background:#f5f5f7}code{background:#f5f5f7;padding:0 .25em;border-radius:3px}
.high{color:#b00020;font-weight:600}.med{color:#a15c00;font-weight:600}.low,.info{color:#555}
.warn{color:#a15c00;font-weight:600}.muted{color:#666}
"""


def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def build_report_html(ctx: Context) -> str:
    adapters = detected_adapters(ctx)
    findings = collect_findings(ctx, adapters)
    items, notes = build_plan(ctx, adapters)
    status = collect_status(ctx)
    now = datetime.now().astimezone().strftime("%b %d, %Y %I:%M %p %Z")
    h = [f"<!doctype html><html><head><meta charset='utf-8'><title>{_e(PRODUCT_NAME)} report</title>",
         f"<style>{_CSS}</style></head><body>",
         f"<h1>{_e(PRODUCT_NAME)} report</h1>",
         f"<p class='muted'>Generated {_e(now)} by {_e(PRODUCT_NAME)} {_e(VERSION)}. Built from local files only; "
         "this page loads nothing from the internet.</p>",
         f"<p>Detected: {_e(', '.join(a.display_name for a in adapters) or 'nothing')}</p>",
         "<h2>What doctor found</h2>"]
    if findings:
        h.append("<table><tr><th>Severity</th><th>ID</th><th>Tool</th><th>What it means</th><th>Fix</th></tr>")
        for f in findings:
            h.append(f"<tr><td class='{_e(f.severity)}'>{_e(f.severity)}</td><td>{_e(f.id)}</td><td>{_e(f.adapter_id)}</td>"
                     f"<td>{_e(mask_text(f.message))}</td><td>{_e(f.fix)}</td></tr>")
        h.append("</table>")
    else:
        h.append("<p>Nothing to fix.</p>")
    h.append("<h2>Setup plan (not yet applied)</h2>")
    if items:
        h.append("<ol>")
        for i in items:
            h.append(f"<li><b>{_e(i.title)}</b><br>Why: {_e(i.why)}<br>")
            for ch in i.changes:
                h.append(f"Changes: <code>{_e(_short(ch['path'], ctx.home))}</code> (key {_e(ch.get('key'))}; {_e(ch.get('reason'))})<br>")
            h.append(f"Undo: <code>{_e(i.undo)}</code>")
            for n in i.notes:
                h.append(f"<br><span class='muted'>Note: {_e(n)}</span>")
            h.append("</li>")
        h.append("</ol>")
        h.append(f"<p>Apply with <code>{_e(CLI_NAME)} setup</code>.</p>")
    else:
        h.append("<p>Nothing to change.</p>")
    for n in notes:
        h.append(f"<p class='muted'>{_e(n)}</p>")
    h.append("<h2>What has been applied</h2>")
    if status["applied"]:
        h.append("<table><tr><th>When</th><th>Step</th><th>File</th><th>State</th></tr>")
        for m in status["applied"]:
            for f in m["files"]:
                h.append(f"<tr><td>{_e(m['created_at'])}</td><td>{_e(m['title'])}</td>"
                         f"<td><code>{_e(_short(f['path'], ctx.home))}</code><br><span class='muted'>{_e(f['reason'])}</span></td>"
                         f"<td>{_e(f['state'])}</td></tr>")
        h.append("</table>")
        h.append(f"<p>Undo the latest setup: <code>{_e(CLI_NAME)} undo</code>. "
                 f"Undo one step: <code>{_e(CLI_NAME)} undo PATH-TO-MANIFEST</code> (manifests are in "
                 f"<code>~/{_e(STATE_DIRNAME)}/manifests</code>).</p>")
    else:
        h.append("<p>Nothing yet.</p>")
    h.append("<h2>Status now</h2><h3>Default models</h3><ul>")
    for row in status["models"]:
        if row["models"]:
            for m in row["models"]:
                h.append(f"<li>{_e(row['adapter'])}: <code>{_e(m['model'])}</code> <span class='muted'>from {_e(_short(m['from'], ctx.home))}</span></li>")
        else:
            h.append(f"<li>{_e(row['adapter'])}: not set</li>")
    h.append("</ul><h3>Newest sessions</h3>")
    if status["sessions"]:
        h.append("<table><tr><th>Last write</th><th>Project</th><th>Model</th><th>History re-read per step</th></tr>")
        for s in status["sessions"]:
            tip = f" <span class='warn'>over {_k(LONG_HISTORY)}: {_e(s.get('clear_hint', 'start fresh'))} at the next task break</span>" if s["long"] else ""
            h.append(f"<tr><td>{_e(s['last_write_local'])}</td><td>{_e(_short_project(s['project']))}</td><td>{_e(s['model'])}</td>"
                     f"<td>~{_e(_k(s['history_tokens']))}{tip}</td></tr>")
        h.append("</table>")
    else:
        h.append("<p>No local session logs found.</p>")
    h.append("<h2>Privacy</h2><p>No telemetry. No network calls except pinned installs you confirm. "
             "Credential files are never opened. Every file change is backed up and can be undone.</p>")
    h.append("</body></html>\n")
    return "\n".join(h)


def write_report(ctx: Context, dest: Path | None = None) -> Path:
    if dest is None:
        d = kit_root(ctx) / "reports"
        d.mkdir(parents=True, exist_ok=True)
        dest = d / f"report-{_now_stamp()}.html"
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(build_report_html(ctx), encoding="utf-8")
    return dest


def setup_run_manifests(ctx: Context, latest: Path) -> list[Path]:
    """All manifests from the same setup run as `latest`, newest first (undo order)."""
    try:
        run = json.loads(latest.read_text(encoding="utf-8")).get("setup_run")
    except (OSError, ValueError):
        return [latest]
    if not run:
        return [latest]
    out = []
    for p in sorted(latest.parent.glob("*.json"), reverse=True):
        try:
            if json.loads(p.read_text(encoding="utf-8")).get("setup_run") == run:
                out.append(p)
        except (OSError, ValueError):
            continue
    return out
