"""CLI: trim dry-runs unless --apply (backed up; undo restores); dashboard is a read-only loopback page; watch is a read-only live loop; model suggest is a local heuristic and model default dry-runs unless --apply; setup explains then applies with backups; status/report are read-only; doctor is read-only; install defaults to dry-run; undo restores; bench is --from-logs, API-key --bench, or subscription --prepare/--measure."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .adapters import REGISTRY
from .context import build_context
from .findings import SEVERITY_ORDER, exit_code
from .identity import CLI_NAME, PRODUCT_NAME, STATE_DIRNAME, VERSION
from .mask import mask_finding_evidence, mask_text


def doctor(args) -> int:
    ctx = build_context(args.home, args.root, args.sys_snapshot, args.plan)
    findings = []
    from . import logcache
    if not getattr(args, "no_cache", True) and not args.privacy:   # callers without the flag: no cache
        logcache.open_disk_memo(ctx.home / STATE_DIRNAME / "cache" / "doctor-logs.json", VERSION)
    try:
        for ad in REGISTRY:
            if not ad.detect(ctx):
                continue
            findings += ad.privacy_checks(ctx) if args.privacy else ad.checks(ctx) + ad.privacy_checks(ctx)
    finally:
        logcache.save_disk_memo()
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.id))
    for f in findings:
        f.evidence = mask_finding_evidence(f.evidence)
    if args.json:
        print(json.dumps([f.to_json() for f in findings], indent=2, default=str))
    else:
        if not findings:
            print("No findings.")
        for f in findings:
            print(f"{f.severity.upper()} {f.id} [{f.adapter_id}]: {mask_text(f.message)}")
            print(f"  fix: {f.fix}\n  source: {f.source}")
    return exit_code(findings)


def cmd_install(args) -> int:
    from .installer import format_install_report, install
    from .runner import default_runner

    ctx = build_context(args.home, args.root, args.sys_snapshot, getattr(args, "plan", None))
    result = install(
        ctx,
        apply=bool(args.apply),
        runner=default_runner(),
        snapshot_dir=args.license_snapshots,
        force=bool(args.force),
        profile=getattr(args, "profile", "clean"),
        proxy=getattr(args, "proxy", None),
        bind=getattr(args, "bind", "127.0.0.1") or "127.0.0.1",
    )
    if args.json:
        print(json.dumps(result.to_json(), indent=2, default=str))
    else:
        print(format_install_report(result), end="")
    return result.exit_code


def cmd_undo(args) -> int:
    from .installer import DriftError, latest_manifest, undo_manifest
    from .setup import setup_run_manifests

    ctx = build_context(args.home, args.root, args.sys_snapshot, getattr(args, "plan", None))
    if args.manifest:
        paths = [Path(args.manifest)]
    else:
        latest = latest_manifest(ctx)
        if latest is None:
            print(f"No install manifest found under ~/{STATE_DIRNAME}/manifests.", file=sys.stderr)
            return 1
        # A setup run writes one manifest per step; undo all of them, newest first.
        paths = setup_run_manifests(ctx, latest)
    result = {"restored": [], "deleted": [], "skipped": [], "manifest": ", ".join(str(p) for p in paths)}
    for path in paths:
        try:
            one = undo_manifest(path, force=bool(args.force))
        except DriftError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except (OSError, RuntimeError, json.JSONDecodeError, KeyError) as exc:
            print(f"undo failed: {exc}", file=sys.stderr)
            return 1
        for k in ("restored", "deleted", "skipped"):
            result[k] += one[k]
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"Restored {len(result['restored'])} file(s), deleted {len(result['deleted'])} created file(s).")
        print(f"Manifest: {result['manifest']}")
        for p in result["restored"]:
            print(f"  restored {p}")
        for p in result["deleted"]:
            print(f"  deleted {p}")
        for p in result["skipped"]:
            print(f"  already undone {p}")
    return 0



def cmd_bench(args) -> int:
    from .adapters import REGISTRY
    from .bench import (
        format_from_logs,
        format_live_bench,
        from_logs,
        live_bench,
    )

    measure = getattr(args, "measure_path", False) is not False
    modes = [bool(args.from_logs), bool(args.live_bench), bool(getattr(args, "prepare", False)), bool(measure)]
    if sum(1 for m in modes if m) != 1:
        print(
            "bench requires exactly one of --from-logs, --bench (API-key automated), "
            "--prepare, or --measure (subscription). No estimates are invented.",
            file=sys.stderr,
        )
        return 2
    ctx = build_context(args.home, args.root, args.sys_snapshot, getattr(args, "plan", None))
    if args.from_logs:
        report = from_logs(ctx, since_days=args.since_days, since=args.since, until=args.until)
        if args.json:
            print(json.dumps(report.to_json(), indent=2, default=str))
        else:
            print(format_from_logs(report), end="")
        return 0
    task = {"prompt": args.prompt} if args.prompt else None
    adapters = None
    if args.adapters:
        wanted = set(args.adapters)
        adapters = [a for a in REGISTRY if a.id in wanted]
        if not adapters:
            print(f"no adapters matched: {', '.join(sorted(wanted))}", file=sys.stderr)
            return 2
    if getattr(args, "prepare", False):
        from .bench import format_prepare, prepare_bench
        dest = Path(args.prepare_dir) if getattr(args, "prepare_dir", None) else None
        report = prepare_bench(ctx, task, adapters=adapters, dest=dest, after_model=getattr(args, "after_model", None))
        if args.json:
            print(json.dumps(report.to_json(), indent=2, default=str))
        else:
            print(format_prepare(report), end="")
        return 0 if report.ok else 1
    if measure:
        from .bench import latest_prepare_dir, measure_bench
        raw = getattr(args, "measure_path", True)
        if raw is True or raw is None or raw is False:
            path = latest_prepare_dir(ctx)
            if path is None:
                print("No prepare dir found. Run bench --prepare first.", file=sys.stderr)
                return 2
        else:
            path = Path(raw)
        report = measure_bench(ctx, path)
        _save_bench_result(path, report)
        if args.json:
            print(json.dumps(report.to_json(), indent=2, default=str))
        else:
            print(format_live_bench(report), end="")
        return 0 if report.ok else 1
    report = live_bench(ctx, task, adapters=adapters, timeout=int(args.timeout or 120))
    if args.json:
        print(json.dumps(report.to_json(), indent=2, default=str))
    else:
        print(format_live_bench(report), end="")
    return 0 if report.ok else 1


def _save_bench_result(path: Path, report) -> None:
    """Keep the measured result beside its prepare manifest (tokenstretch state dir) for report/dashboard."""
    d = path if path.is_dir() else path.parent
    try:
        (d / "result.json").write_text(json.dumps(report.to_json(), indent=2, default=str), encoding="utf-8")
    except OSError:
        pass


def cmd_setup(args) -> int:
    from .setup import run_setup

    ctx = build_context(args.home, args.root, args.sys_snapshot, getattr(args, "plan", None))
    installs = True if args.with_installs else (False if args.no_installs else None)
    if args.list:
        from .setup import build_plan, format_item_list
        items, _ = build_plan(ctx)
        if args.json:
            print(json.dumps({"items": [i.to_json() for i in items]}, indent=2))
        else:
            print("\n".join(format_item_list(ctx, items)))
        return 0
    from .setup import UnknownItem
    try:
        result = run_setup(ctx, yes=bool(args.yes), dry_run=bool(args.dry_run), installs=installs,
                           only=args.only, skip=args.skip)
    except UnknownItem as e:
        print(str(e), file=sys.stderr)
        return 2
    if result.installs and not result.installs.get("ok"):
        return 1
    return 0


def cmd_status(args) -> int:
    from .setup import collect_status, format_status

    ctx = build_context(args.home, args.root, args.sys_snapshot, getattr(args, "plan", None))
    status = collect_status(ctx)
    if args.json:
        print(json.dumps(status, indent=2, default=str))
    else:
        print("\n".join(format_status(ctx, status)))
    return 0


def cmd_report(args) -> int:
    from .setup import write_report

    ctx = build_context(args.home, args.root, args.sys_snapshot, getattr(args, "plan", None))
    if getattr(args, "weekly", False):
        from .weekly import build_weekly, write_weekly
        weeks = max(1, min(int(args.weeks or 4), 52))
        if args.json:
            print(json.dumps(build_weekly(ctx, weeks=weeks), indent=2, default=str))
            return 0
        path = write_weekly(ctx, args.out, weeks=weeks)
        print(f"Weekly report written (local file, no network): {path}")
        return 0
    path = write_report(ctx, args.out)
    print(f"Report written (local file, no network): {path}")
    return 0


def cmd_watch(args) -> int:
    from .watch import WatchConfig, run_watch

    ctx = build_context(args.home, args.root, args.sys_snapshot, getattr(args, "plan", None))
    cfg = WatchConfig(max_context=args.max_context, max_reads=args.max_reads, max_start=args.max_start,
                      wake_min=max(1, args.wake_min))
    return run_watch(ctx, cfg, interval=max(1.0, args.interval), once=bool(args.once), notify=bool(args.notify))


def cmd_statusline(args) -> int:
    from .statusline import run_statusline
    from .watch import WatchConfig

    cfg = WatchConfig(max_context=args.max_context, max_reads=args.max_reads)
    return run_statusline(args.home, args.root, getattr(args, "plan", None), cfg, wrap=args.wrap)


def cmd_startup(args) -> int:
    from .startup import build, format_report

    ctx = build_context(args.home, args.root, args.sys_snapshot, getattr(args, "plan", None))
    data = build(ctx, remember=not args.no_save)
    if getattr(args, "agent", False):  # --- worker agent profile block ---
        from .startup import agent_profile, format_agent
        profiles = [agent_profile(d) for d in data]
        print(json.dumps(profiles, indent=2) if args.json else "\n".join(format_agent(profiles)))
        return 0
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        for line in format_report(data, top=10_000 if args.all else 12):
            print(line)
    return 0


def cmd_auto(args) -> int:
    from .auto import run_auto

    return run_auto()


def cmd_nudge(args) -> int:
    from .nudge import run_nudge
    from .watch import WatchConfig

    cfg = WatchConfig(max_context=args.max_context, max_reads=args.max_reads)
    return run_nudge(args.home, args.root, getattr(args, "plan", None), cfg)


def cmd_dashboard(args) -> int:
    from .dashboard import run_dashboard

    ctx = build_context(args.home, args.root, args.sys_snapshot, getattr(args, "plan", None))
    return run_dashboard(ctx, host=args.host, port=args.port, open_browser=not args.no_open,
                         interval=max(2.0, float(args.interval)), weeks=max(1, min(int(args.weeks), 52)),
                         out=lambda line: print(line, flush=True))


def cmd_model(args) -> int:
    from .models import format_suggestion, run_default, suggest_tier, tool_picks

    ctx = build_context(args.home, args.root, args.sys_snapshot, getattr(args, "plan", None))
    if args.model_cmd == "suggest":
        text = " ".join(args.task or [])
        if args.file:
            try:
                text = (text + "\n" + args.file.read_text(encoding="utf-8", errors="replace")).strip()
            except OSError as exc:
                print(f"cannot read {args.file}: {exc}", file=sys.stderr)
                return 2
        if not text.strip():
            print('describe the task: model suggest "fix the flaky login test"  (or --file prompt.txt)', file=sys.stderr)
            return 2
        res = suggest_tier(text)
        picks = tool_picks(ctx, res["tier"], effort=res["effort"])
        if args.json:
            print(json.dumps({**res, "tools": picks}, indent=2))
        else:
            print("\n".join(format_suggestion(res, picks)))
        return 0
    if args.model_cmd == "effort":
        from .models import effort_mix, format_effort_mix
        from .setup import detected_adapters
        sessions = []
        for ad in detected_adapters(ctx):
            sessions += ad.watch_sessions(ctx, limit=max(1, min(int(args.sessions), 50))) or []
        mix = effort_mix(sessions)
        lines = format_effort_mix(mix)
        if args.by_session:
            from .models import effort_by_session, format_effort_by_session
            mix["by_session"] = effort_by_session(sessions)
            lines += format_effort_by_session(mix["by_session"])
        print(json.dumps(mix, indent=2) if args.json else "\n".join(lines))
        return 0
    if args.model_cmd == "cost":
        from .cost import cost_view, format_cost_view
        from .setup import detected_adapters
        sessions = []
        for ad in detected_adapters(ctx):
            sessions += ad.watch_sessions(ctx, limit=max(1, min(int(args.sessions), 50))) or []
        view = cost_view(sessions)
        if args.grid:
            from .cost import format_grid, grid
            rows = grid(view)
            print(json.dumps(rows, indent=2) if args.json else "\n".join(format_grid(rows)))
            return 0
        print(json.dumps(view, indent=2) if args.json else "\n".join(format_cost_view(view)))
        return 0
    if args.model_cmd == "rework":  # --- rework meter block ---
        from .rework import format_rework, rework_view
        from .setup import detected_adapters
        sessions = []
        for ad in detected_adapters(ctx):
            sessions += ad.watch_sessions(ctx, limit=max(1, min(int(args.sessions), 50))) or []
        view = rework_view(sessions)
        print(json.dumps(view, indent=2) if args.json else "\n".join(format_rework(view)))
        return 0
    return run_default(ctx, args.tier, tool=args.tool, apply=bool(args.apply) and not args.dry_run)


# ---- pace: budget governor (bounded block) ----
def cmd_pace(args) -> int:
    import time as _time
    from datetime import datetime
    from . import pace as pc

    ctx = build_context(args.home, args.root, args.sys_snapshot, getattr(args, "plan", None))
    state = pc.load_state(ctx.home)
    now = pc.parse_time(args.now) if getattr(args, "now", None) else _time.time()
    out = (lambda obj, lines: print(json.dumps(obj, indent=2) if args.json else "\n".join(lines)))
    try:
        if args.pace_cmd == "set":
            if not args.window or args.used is None or not args.resets:
                print("pace set needs --window NAME --used PCT --resets ISO_TIME", file=sys.stderr)
                return 2
            at = pc.parse_time(args.at) if args.at else now
            length = pc.parse_length(args.length) if args.length else None
            what = pc.set_reading(state, args.window, args.used, pc.parse_time(args.resets), at,
                                  length_s=length, models=args.models)
            path = pc.save_state(ctx.home, state)
            w = state["windows"][args.window]
            msg = {"new": "stored", "added": "added a reading to", "restarted": "started a new period for",
                   "corrected": "replaced older readings of"}[what]
            out({"window": args.window, "result": what, "file": str(path), **w},
                [f"{msg} window {args.window}: {args.used:g}% used, resets {w['resets']}, length "
                 f"{pc.dur(w['length_s'])}" + (f", models /{w['models']}/" if w["models"] else "") + f". ({path})"])
            return 0
        if args.pace_cmd == "clear":
            if not args.window:
                print("pace clear needs --window NAME", file=sys.stderr)
                return 2
            gone = state["windows"].pop(args.window, None) is not None
            if gone:
                pc.save_state(ctx.home, state)
            out({"window": args.window, "cleared": gone},
                [f"cleared window {args.window}." if gone else f"no window named {args.window}."])
            return 0
    except (ValueError, OSError) as exc:
        print(f"pace: {exc}", file=sys.stderr)
        return 2

    reserve = pc.DEFAULT_RESERVE if args.reserve is None else float(args.reserve)
    if args.window and args.window not in state["windows"]:
        if args.gate:
            note = f"no reading stored for window {args.window}: not blocking"
            out({"ok": True, "wait_seconds": 0, "window": None, "used": None, "line": None},
                [f"pace gate: ok. {note}."])
            return 0
        print(f"no window named {args.window}; stored: {', '.join(sorted(state['windows'])) or 'none'}", file=sys.stderr)
        return 2
    if args.gate and not state["windows"]:
        note = "no reading stored: not blocking"
        out({"ok": True, "wait_seconds": 0, "window": None, "used": None, "line": None},
            [f"pace gate: ok. {note}."])
        return 0
    if not state["windows"] and not (args.team or args.what_if):
        out({"windows": []}, pc.format_windows([], {}, None))
        return 0

    from .setup import detected_adapters
    starts = [pc.parse_time(w["resets"]) - w["length_s"] for w in state["windows"].values()]
    age = max(0.0, _time.time() - min(starts)) + 86400 if starts else None   # logs older than every window are not opened
    sessions = []
    for ad in detected_adapters(ctx):
        sessions += ad.window_sessions(ctx, limit=max(1, min(int(args.sessions), 1000)), max_age_s=age) or []
    wins = pc.evaluate(state, sessions, now, reserve, args.window)

    if args.gate:
        g = pc.run_gate(state, sessions, now, reserve, args.window)
        out({k: g[k] for k in ("ok", "wait_seconds", "window", "used", "line")}, pc.format_gate(g))
        return 0 if g["ok"] else 3
    # measure a step on what ran inside the stored windows, not on older steps in the same log files
    stats = pc.step_stats(pc.cut(sessions, min(starts), now) if starts else sessions)
    result: dict = {"windows": wins, "tightest": (pc.tightest(wins) or {}).get("window"), "estimate": True,
                    "note": pc.NOT_A_BILL}
    lines: list[str] = []
    team = None
    if args.team:
        team = pc.team_view(wins, stats, max(1, int(args.team)), args.model, args.effort, now)
        result["team"] = team
    if args.what_if:
        result["what_if"] = pc.what_if(state, sessions, wins, team, now)
    if not args.team and not args.what_if:
        model = pc.pick_model(stats, args.model)
        today = {w["window"]: pc.steps_today(w, stats, model, args.effort) for w in wins}
        result["steps_today"] = today
        lines = pc.format_windows(wins, today, pc.tightest(wins))
    else:
        if args.team:
            lines += pc.format_team(team)
        if args.what_if:
            lines += pc.format_what_if(result["what_if"])
    out(result, lines)
    return 0
# ---- end pace ----


def cmd_brief(args) -> int:
    from .brief import Brief, build, format_report, from_preset, split

    text = " ".join(args.task or [])
    if args.file:
        try:
            text = (text + "\n" + args.file.read_text(encoding="utf-8", errors="replace")).strip()
        except OSError as exc:
            print(f"cannot read {args.file}: {exc}", file=sys.stderr)
            return 2
    if not text.strip():
        print('describe the task: brief "add a retry to the upload client" --files src/upload.py --done "tests pass"',
              file=sys.stderr)
        return 2
    research = True if args.research else False if args.no_research else None
    given = {"task": text, "files": list(args.files or []), "sources": list(args.sources or []), "done": args.done or "",
             "no_build": bool(args.no_build), "review": bool(args.review), "rules": list(args.rule or [])}
    # only what was typed goes in, so a preset's defaults fill the rest
    if research is not None:
        given["research"] = research
    if args.reply_words is not None:
        given["reply_words"] = max(10, args.reply_words)
    for name in ("max_searches", "max_fetches", "max_reads"):
        if getattr(args, name) is not None:
            given[name] = max(0, getattr(args, name))
    if args.helper or args.review:
        given["helper"] = True
    brief = from_preset(args.preset, **given) if args.preset else Brief(**given)
    parts = split(brief, args.split) if args.split > 0 else [brief]
    if len(parts) > 1:
        many = [build(p) for p in parts]
        if args.json:
            print(json.dumps(many, indent=2))
            return 0
        for i, (p, d) in enumerate(zip(parts, many), 1):
            print(f"--- brief {i} of {len(parts)} ({len(p.files)} files, about {d['approx_tokens']} tokens) ---")
            print(d["brief"])
        if not args.brief_only:
            print(f"Give each brief to its own helper. {len(parts)} short histories cost less than one long one.")
        return 0
    data = build(brief)
    if args.brief_only:
        print(data["brief"], end="")
        return 0
    if args.json:
        print(json.dumps(data, indent=2))
    elif args.check:
        for x in data["lint"]:
            print(f"- {x['what']} Fix: {x['fix']}")
        if not data["lint"]:
            print("Nothing missing.")
    else:
        print("\n".join(format_report(data)))
    return 1 if args.check and data["lint"] else 0


# --- team queue block ---
def cmd_queue(args) -> int:
    from . import queue as q

    root = Path(getattr(args, "root", None) or Path.cwd()).expanduser().resolve()
    home = Path(args.home).expanduser().resolve() if getattr(args, "home", None) else Path.home()
    sub = args.queue_cmd
    if sub == "add":
        preset = args.preset or ""
        if args.from_plan:
            try:
                text = args.from_plan.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                print(f"cannot read {args.from_plan}: {exc}", file=sys.stderr)
                return 2
            tasks = q.add_from_plan(root, text, preset=preset)
            if not tasks:
                print("no numbered steps found in the plan", file=sys.stderr)
                return 2
        else:
            text = " ".join(args.task or []).strip()
            if not text:
                print('describe the task: queue add "fix the retry" --files src/a.py --done "tests pass"  (or --from-plan FILE)',
                      file=sys.stderr)
                return 2
            tasks = [q.add_task(root, text, files=args.files or [], done=args.done or "", preset=preset, hard=args.hard)]
        for t in tasks:
            print(f"added {t['id']}: {t['task'][:70]}")
        return 0
    if sub == "next":
        gate = q.pace_gate(home)
        if not gate["open"]:
            from .pace import dur
            print(f"Over the even-pace line: wait about {dur(gate['wait_seconds'])}, then ask again. "
                  "Start no new task.")
            return q.EXIT_OVER_LINE
        task, why = q.claim_next(root, args.agent or "")
        if task is None:
            print("Open tasks exist but wait on files another agent holds." if why == "blocked"
                  else "The queue has no open task.")
            return q.EXIT_EMPTY
        print(q.next_text(task))
        return 0
    if sub in ("done", "fail"):
        try:
            t = q.done_task(root, args.id, args.note or "") if sub == "done" else q.fail_task(root, args.id, args.note)
        except KeyError as exc:
            print(str(exc).strip("'"), file=sys.stderr)
            return 2
        print(f"{t['id']}: {t['status']}" + (" (needs the lead: failed twice)" if t["status"] == "needs-lead" else ""))
        return 0
    if sub == "list":
        rows = q.list_view(root)
        print(json.dumps(rows, indent=2) if args.json else "\n".join(q.format_list(rows)))
        return 0
    if sub == "release":
        ids = q.release_stale(root, args.stale)
        print(f"released {len(ids)} stale claim(s)" + (": " + ", ".join(ids) if ids else ""))
        return 0
    if sub == "touch":
        return 0 if q.touch_task(root, args.id) else 2
    print(q.loop_prompt())
    return 0


def cmd_trim(args) -> int:
    from .trim import Options, run_trim

    root = args.project or args.root
    ctx = build_context(args.home, root, args.sys_snapshot, getattr(args, "plan", None))
    opts = Options(max_tokens=max(1, args.max_tokens), max_block_lines=max(1, args.max_block_lines),
                   move_blocks=bool(args.move_blocks))
    return run_trim(ctx, opts, apply=bool(args.apply) and not args.dry_run, yes=bool(args.yes),
                    as_json=bool(args.json), show_diff=not args.no_diff)


def _add_context_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--home", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--sys-snapshot", type=Path)
    parser.add_argument("--plan", choices=["subscription", "api"])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog=CLI_NAME)
    p.add_argument("--version", action="version", version=f"{PRODUCT_NAME} {VERSION}")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="read-only local audit (no network)")
    _add_context_args(d)
    d.add_argument("--json", action="store_true")
    d.add_argument("--privacy", action="store_true")
    d.add_argument("--no-cache", action="store_true",
                   help=f"re-read every session log and write nothing (default: keep per-log totals in ~/{STATE_DIRNAME}/cache)")

    inst = sub.add_parser(
        "install",
        help="plan (default) or apply a profile; Clean is default, api-proxy is opt-in; writes only with --apply",
    )
    _add_context_args(inst)
    inst.add_argument(
        "--apply",
        action="store_true",
        help="actually write files and run pinned fetch steps (default is dry-run)",
    )
    inst.add_argument(
        "--force",
        action="store_true",
        help="apply even when port 8787 is already occupied",
    )
    inst.add_argument(
        "--license-snapshots",
        type=Path,
        help="directory of license snapshots (defaults to the packaged snapshots; never fetched)",
    )
    inst.add_argument(
        "--profile",
        choices=("clean", "api-proxy"),
        default="clean",
        help="install profile (default: clean). api-proxy is opt-in and never part of Clean",
    )
    inst.add_argument(
        "--proxy",
        action="append",
        choices=("headroom", "kompressor"),
        dest="proxy",
        default=None,
        help="with --profile api-proxy, choose exactly one of headroom or kompressor (repeat is refused)",
    )
    inst.add_argument(
        "--bind",
        default="127.0.0.1",
        help="loopback address only (default 127.0.0.1); 0.0.0.0 is refused",
    )
    inst.add_argument("--json", action="store_true")

    st = sub.add_parser("setup", help="doctor + plain-English plan + one confirmation + apply with backups")
    _add_context_args(st)
    st.add_argument("--yes", "-y", action="store_true", help="apply without asking (non-interactive)")
    st.add_argument("--dry-run", action="store_true", help="show the plan only; change nothing")
    st.add_argument("--with-installs", action="store_true",
                    help="also run the pinned installs (network: rtk-oss, Caveman skill) without asking")
    st.add_argument("--no-installs", action="store_true", help="never offer the pinned installs")
    st.add_argument("--list", action="store_true",
                    help="list the steps available here with their ids; read-only, no doctor run")
    st.add_argument("--only", action="append", metavar="ID",
                    help="apply only this step (repeat, or separate ids with commas); see --list")
    st.add_argument("--skip", action="append", metavar="ID",
                    help="leave this step out of the plan (repeat, or separate ids with commas)")
    st.add_argument("--json", action="store_true", help="with --list, print the steps as JSON")

    stt = sub.add_parser("status", help="what setup applied, current models, and newest sessions (read-only)")
    _add_context_args(stt)
    stt.add_argument("--json", action="store_true")

    rep = sub.add_parser("report", help="write a self-contained local HTML report and print its path")
    _add_context_args(rep)
    rep.add_argument("--out", type=Path, help=f"output file (default ~/{STATE_DIRNAME}/reports/report-<time>.html)")
    rep.add_argument("--weekly", action="store_true",
                     help="weekly usage roll-up from local logs: tokens by type and model, waste spots, since-setup")
    rep.add_argument("--weeks", type=int, default=4, help="weeks to include with --weekly (default 4)")
    rep.add_argument("--json", action="store_true", help="with --weekly, print the data as JSON instead of writing HTML")

    und = sub.add_parser("undo", help="restore files from the latest setup run or install manifest")
    _add_context_args(und)
    und.add_argument("manifest", nargs="?", type=Path, help="manifest JSON (default: latest under --home)")
    und.add_argument("--manifest", dest="manifest_flag", type=Path, help="same as the positional manifest path")
    und.add_argument("--force", action="store_true", help="restore even if a file changed since the install")
    und.add_argument("--json", action="store_true")

    b = sub.add_parser(
        "bench",
        help="summarize local usage (--from-logs) or run a live before/after in a throwaway copy (--bench)",
    )
    _add_context_args(b)
    b.add_argument("--from-logs", action="store_true", help="read-only summary of local usage logs")
    b.add_argument("--bench", action="store_true", dest="live_bench",
                   help="API-key automated before/after in a throwaway copy (never touches real HOME/repo)")
    b.add_argument("--prepare", action="store_true",
                   help="subscription mode: build before/after sandbox repos and print official CLI commands")
    b.add_argument("--prepare-dir", type=Path, help="optional destination for --prepare")
    b.add_argument("--after-model", help="with --prepare, add this model only to the after-side commands")
    b.add_argument("--measure", nargs="?", const=True, default=False, dest="measure_path",
                   help="subscription mode: read logs for a --prepare dir (default: latest) and compare")
    b.add_argument("--since-days", type=int, default=30, help="time window for --from-logs (default 30)")
    b.add_argument("--since", help="ISO timestamp lower bound for --from-logs")
    b.add_argument("--until", help="ISO timestamp upper bound for --from-logs")
    b.add_argument("--prompt", help="task prompt for --bench / --prepare")
    b.add_argument("--timeout", type=int, default=120, help="seconds per headless run (default 120)")
    b.add_argument("--adapter", action="append", dest="adapters", help="limit to this adapter id (repeatable)")
    b.add_argument("--json", action="store_true")

    w = sub.add_parser("watch", help="read-only live loop over local session logs; warns once per waste condition")
    _add_context_args(w)
    w.add_argument("--interval", type=float, default=10.0, help="seconds between passes (default 10)")
    w.add_argument("--once", action="store_true", help="one pass, then exit")
    w.add_argument("--max-context", type=int, default=150_000, help="warn when history passes this (default 150000)")
    w.add_argument("--max-reads", type=int, default=3, help="warn when one file is read this many times (default 3)")
    w.add_argument("--max-start", type=int, default=40_000,
                   help="warn when a session's first step already carries this much history (default 40000)")
    w.add_argument("--wake-min", type=int, default=3,
                   help="warn when finished helpers woke a conversation this many times (default 3)")
    w.add_argument("--notify", action="store_true", help="also show a local desktop notification (osascript/notify-send)")

    sl = sub.add_parser("statusline",
                        help="one read-only line for a status bar: history size and the top watch warning")
    _add_context_args(sl)
    sl.add_argument("--max-context", type=int, default=150_000, help="warn when history passes this (default 150000)")
    sl.add_argument("--max-reads", type=int, default=3, help="warn when one file is read this many times (default 3)")
    sl.add_argument("--wrap", metavar="COMMAND",
                    help="share the slot: run this status-line command too (same input) and add this line after its first line")

    st = sub.add_parser("startup",
                        help="what every session carries before you type: parts sized from local files, largest first")
    _add_context_args(st)
    st.add_argument("--all", action="store_true", help="list every part, not just the largest 12")
    st.add_argument("--no-save", action="store_true",
                    help="don't remember this run (it is used to size a connector by difference next time)")
    st.add_argument("--agent", action="store_true",  # --- worker agent profile block ---
                    help="a lean profile for a worker agent: what to switch off, tokens saved, and the saving over 50 steps (prints settings; writes nothing)")
    st.add_argument("--json", action="store_true")

    sub.add_parser("auto", help="hook entry: resumable compaction summaries and note pickup in a new chat "
                                "(reads the tool's JSON on stdin)")

    ng = sub.add_parser("nudge",
                        help="hook entry: print the top watch warning for the session a tool names on stdin")
    _add_context_args(ng)
    ng.add_argument("--max-context", type=int, default=150_000, help="warn when history passes this (default 150000)")
    ng.add_argument("--max-reads", type=int, default=3, help="warn when one file is read this many times (default 3)")

    dash = sub.add_parser("dashboard", help="read-only local web page with live usage, warnings, and doctor results")
    _add_context_args(dash)
    dash.add_argument("--host", default="127.0.0.1", help="only 127.0.0.1 is accepted (loopback only)")
    dash.add_argument("--port", type=int, default=8765, help="port (default 8765; a free one is picked if busy)")
    dash.add_argument("--no-open", action="store_true", help="print the URL instead of opening the browser")
    dash.add_argument("--interval", type=float, default=5.0, help="seconds between page refreshes (default 5)")
    dash.add_argument("--weeks", type=int, default=8,
                      help="weeks of usage logs to read (default 8); older log files are skipped unopened")

    tr = sub.add_parser("trim", help="find waste in instruction files resent every turn; propose a safe trim (dry-run unless --apply)")
    _add_context_args(tr)
    tr.add_argument("--project", type=Path, help="project folder (default: the current folder; same as --root)")
    tr.add_argument("--max-tokens", type=int, default=2000, help="flag files over this many approx. tokens (default 2000)")
    tr.add_argument("--max-block-lines", type=int, default=30, help="flag fenced code blocks over this many lines (default 30)")
    tr.add_argument("--move-blocks", action="store_true",
                    help="also move big code blocks and logs from project files to docs/agent-notes/ with a pointer")
    tr.add_argument("--apply", action="store_true", help="write the proposed edit (backed up; undo restores it)")
    tr.add_argument("--dry-run", action="store_true", help="show the analysis and diff only (the default)")
    tr.add_argument("--yes", "-y", action="store_true", help="with --apply, don't ask")
    tr.add_argument("--no-diff", action="store_true", help="don't print the unified diff")
    tr.add_argument("--json", action="store_true")

    br = sub.add_parser("brief", help="turn a one-line task into a brief that keeps an agent or helper cheap (local rules, no model calls)")
    br.add_argument("task", nargs="*", help="the task, in a sentence")
    br.add_argument("--file", type=Path, help="read the task from this file")
    br.add_argument("--files", nargs="+", metavar="PATH", help="the only files the agent may change")
    br.add_argument("--from", dest="sources", nargs="+", metavar="PATH", help="notes or reference files to work from")
    br.add_argument("--done", help='the stop condition, e.g. "tests pass"')
    br.add_argument("--reply-words", type=int, default=None, help="cap on the reply length (default 80)")
    br.add_argument("--research", action="store_true", help="the task is research (default: decided from the wording)")
    br.add_argument("--no-research", action="store_true", help="forbid searching and new research")
    br.add_argument("--max-searches", type=int, default=None, help="cap searches for a research task")
    br.add_argument("--max-fetches", type=int, default=None, metavar="N",
                    help="for research: cap the number of pages opened")
    br.add_argument("--brief-only", action="store_true",
                    help="print only the brief, ready to paste or pipe (no token line, no missing list)")
    br.add_argument("--rule", action="append", metavar="TEXT",
                    help="add your own rule line to the brief; repeat for more")
    br.add_argument("--no-build", action="store_true", help="tell the agent not to run builds, tests, or installs")
    br.add_argument("--helper", action="store_true", help="word the brief for a helper (subagent)")
    br.add_argument("--review", action="store_true",
                    help="a review brief: read the named files, report problems as path:line, change nothing")
    from .brief import PRESETS as _BRIEF_PRESETS
    br.add_argument("--preset", choices=sorted(_BRIEF_PRESETS),
                    help="start from a job shape: " + "; ".join(f"{k} = {v['about']}" for k, v in sorted(_BRIEF_PRESETS.items())))
    br.add_argument("--max-reads", type=int, default=None, help="cap the number of file reads (0 = no cap line)")
    br.add_argument("--split", type=int, default=0, metavar="N",
                    help="print one brief per group of N files, each for its own helper")
    br.add_argument("--check", action="store_true", help="only list what is missing; exit 1 if anything is")
    br.add_argument("--json", action="store_true")

    # --- team queue block ---
    qp = sub.add_parser("queue", help="file-based task queue for an autonomous team: agents pull briefs, no lead steps")
    qsub = qp.add_subparsers(dest="queue_cmd", required=True)
    qa = qsub.add_parser("add", help="add a task (or one task per step of a numbered plan)")
    _add_context_args(qa)
    qa.add_argument("task", nargs="*", help="the task, in a sentence")
    qa.add_argument("--files", nargs="+", metavar="PATH", help="the only files the agent may change")
    qa.add_argument("--done", help='the check, e.g. "tests pass"')
    qa.add_argument("--preset", choices=sorted(_BRIEF_PRESETS), help="brief preset")
    qa.add_argument("--hard", action="store_true", help="mark a hard step (suggests the top model)")
    qa.add_argument("--from-plan", type=Path, metavar="FILE", help="split a numbered plan into one task per step")
    qn = qsub.add_parser("next", help="check the pace gate, claim the oldest open task, print its brief (exit 3 over line, 4 empty)")
    _add_context_args(qn)
    qn.add_argument("--agent", help="a name for the claim")
    for name, text in (("done", "mark a task done"), ("fail", "send a task back to open with a note (needs-lead after 2)")):
        qx = qsub.add_parser(name, help=text)
        _add_context_args(qx)
        qx.add_argument("id")
        qx.add_argument("--note", required=(name == "fail"))
    ql = qsub.add_parser("list", help="every task and its state")
    _add_context_args(ql)
    ql.add_argument("--json", action="store_true")
    qr = qsub.add_parser("release", help="drop claims with no update for MINUTES")
    _add_context_args(qr)
    qr.add_argument("--stale", type=float, required=True, metavar="MINUTES")
    qt = qsub.add_parser("touch", help="mark a claim as still alive")
    _add_context_args(qt)
    qt.add_argument("id")
    qsub.add_parser("loop-prompt", help="print the standing prompt for a queue worker session").add_argument(
        "--home", type=Path, help=argparse.SUPPRESS)

    m = sub.add_parser("model", help="heuristic model picker and a backed-up default-model switch")
    msub = m.add_subparsers(dest="model_cmd", required=True)
    ms = msub.add_parser("suggest", help="suggest light / mid / top for a task (local rules, no model calls)")
    _add_context_args(ms)
    ms.add_argument("task", nargs="*", help="the task description")
    ms.add_argument("--file", type=Path, help="read the task description from this file")
    ms.add_argument("--json", action="store_true")
    me = msub.add_parser("effort", help="the reasoning effort your recent sessions ran at (read from local logs)")
    _add_context_args(me)
    me.add_argument("--sessions", type=int, default=10, help="newest conversations to read per tool (default 10)")
    me.add_argument("--by-session", action="store_true",
                    help="also list each conversation with the model and effort it was logged at")
    me.add_argument("--json", action="store_true")
    mc = msub.add_parser("cost", help="where recent sessions' credits went, per model and effort, weighted by price")
    _add_context_args(mc)
    mc.add_argument("--sessions", type=int, default=10, help="newest conversations to read per tool (default 10)")
    mc.add_argument("--grid", action="store_true",
                    help="one line per model and effort: what a step cost on average, dearest first")
    mc.add_argument("--json", action="store_true")
    # --- rework meter block ---
    mr = msub.add_parser("rework", help="estimate how much spend went to redo work, per model and effort (heuristic)")
    _add_context_args(mr)
    mr.add_argument("--sessions", type=int, default=10, help="newest conversations to read per tool (default 10)")
    mr.add_argument("--json", action="store_true")
    md = msub.add_parser("default", help="set a tool's default model by tier (dry-run unless --apply)")
    _add_context_args(md)
    md.add_argument("--tier", choices=("light", "mid", "top"), required=True)
    md.add_argument("--tool", help="adapter id, e.g. claude (default: every detected tool with a tier map)")
    md.add_argument("--apply", action="store_true", help="write the change (backed up; undo restores it)")
    md.add_argument("--dry-run", action="store_true", help="show the plan only (the default)")

    # ---- pace parser (bounded block) ----
    pa = sub.add_parser("pace", help="budget governor: keep a plan window on an even burn line "
                                     "(estimates from local logs and your readings, not a bill)")
    _add_context_args(pa)
    pa.add_argument("pace_cmd", nargs="?", choices=("set", "clear"),
                    help="set: store a reading of percent used; clear: forget a window; none: show the pace")
    pa.add_argument("--window", help="window name, e.g. 5h, weekly, weekly-top")
    pa.add_argument("--used", type=float, help="set: percent used, as your plan's usage page shows it")
    pa.add_argument("--resets", help="set: when the window resets (ISO time)")
    pa.add_argument("--length", help="set: window length, 7d or 5h (default 7d, 5h for a window named 5h)")
    pa.add_argument("--models", help="set: regex of the model names that count in this window")
    pa.add_argument("--at", help="set: time of the reading (ISO, default now)")
    pa.add_argument("--gate", action="store_true",
                    help="exit 0 at or under the even-pace line, 3 over it (0 when no reading is stored)")
    pa.add_argument("--reserve", type=float, help="points kept below the line (default 3)")
    pa.add_argument("--team", type=int, metavar="N", help="how many of N agents can run at once, and for how many hours a day")
    pa.add_argument("--model", help="--team and steps left: model id (default the busiest)")
    pa.add_argument("--effort", help="--team and steps left: effort (default the most used)")
    pa.add_argument("--what-if", action="store_true",
                    help="the same week with no re-saved history and re-reads halved")
    pa.add_argument("--sessions", type=int, default=500, help="newest conversations to read per tool (default 500)")
    pa.add_argument("--now", help=argparse.SUPPRESS)
    pa.add_argument("--json", action="store_true")
    # ---- end pace parser ----

    a = p.parse_args(argv)
    if a.cmd == "doctor":
        return doctor(a)
    if a.cmd == "install":
        return cmd_install(a)
    if a.cmd == "undo":
        if getattr(a, "manifest_flag", None) and not a.manifest:
            a.manifest = a.manifest_flag
        return cmd_undo(a)
    if a.cmd == "bench":
        return cmd_bench(a)
    if a.cmd == "setup":
        return cmd_setup(a)
    if a.cmd == "status":
        return cmd_status(a)
    if a.cmd == "report":
        return cmd_report(a)
    if a.cmd == "watch":
        return cmd_watch(a)
    if a.cmd == "statusline":
        return cmd_statusline(a)
    if a.cmd == "startup":
        return cmd_startup(a)
    if a.cmd == "auto":
        return cmd_auto(a)
    if a.cmd == "nudge":
        return cmd_nudge(a)
    if a.cmd == "dashboard":
        return cmd_dashboard(a)
    if a.cmd == "model":
        return cmd_model(a)
    if a.cmd == "brief":
        return cmd_brief(a)
    if a.cmd == "trim":
        return cmd_trim(a)
    if a.cmd == "queue":  # --- team queue block ---
        return cmd_queue(a)
    if a.cmd == "pace":
        return cmd_pace(a)
    print(f"{a.cmd}: not implemented yet", file=sys.stderr)
    return 3
