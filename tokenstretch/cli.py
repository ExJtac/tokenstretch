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
    for ad in REGISTRY:
        if not ad.detect(ctx):
            continue
        findings += ad.privacy_checks(ctx) if args.privacy else ad.checks(ctx) + ad.privacy_checks(ctx)
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
    result = run_setup(ctx, yes=bool(args.yes), dry_run=bool(args.dry_run), installs=installs)
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
    cfg = WatchConfig(max_context=args.max_context, max_reads=args.max_reads, max_start=args.max_start)
    return run_watch(ctx, cfg, interval=max(1.0, args.interval), once=bool(args.once), notify=bool(args.notify))


def cmd_statusline(args) -> int:
    from .statusline import run_statusline
    from .watch import WatchConfig

    cfg = WatchConfig(max_context=args.max_context, max_reads=args.max_reads)
    return run_statusline(args.home, args.root, getattr(args, "plan", None), cfg, wrap=args.wrap)


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
        picks = tool_picks(ctx, res["tier"])
        if args.json:
            print(json.dumps({**res, "tools": picks}, indent=2))
        else:
            print("\n".join(format_suggestion(res, picks)))
        return 0
    return run_default(ctx, args.tier, tool=args.tool, apply=bool(args.apply) and not args.dry_run)


def cmd_brief(args) -> int:
    from .brief import Brief, build, format_report

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
    data = build(Brief(task=text, files=list(args.files or []), sources=list(args.sources or []), done=args.done or "",
                       reply_words=max(10, args.reply_words), research=research,
                       max_searches=max(0, args.max_searches), no_build=bool(args.no_build), helper=bool(args.helper),
                       rules=list(args.rule or [])))
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
    w.add_argument("--notify", action="store_true", help="also show a local desktop notification (osascript/notify-send)")

    sl = sub.add_parser("statusline",
                        help="one read-only line for a status bar: history size and the top watch warning")
    _add_context_args(sl)
    sl.add_argument("--max-context", type=int, default=150_000, help="warn when history passes this (default 150000)")
    sl.add_argument("--max-reads", type=int, default=3, help="warn when one file is read this many times (default 3)")
    sl.add_argument("--wrap", metavar="COMMAND",
                    help="share the slot: run this status-line command too (same input) and add this line after its first line")

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
    br.add_argument("--reply-words", type=int, default=80, help="cap on the reply length (default 80)")
    br.add_argument("--research", action="store_true", help="the task is research (default: decided from the wording)")
    br.add_argument("--no-research", action="store_true", help="forbid searching and new research")
    br.add_argument("--max-searches", type=int, default=0, help="cap searches for a research task")
    br.add_argument("--rule", action="append", metavar="TEXT",
                    help="add your own rule line to the brief; repeat for more")
    br.add_argument("--no-build", action="store_true", help="tell the agent not to run builds, tests, or installs")
    br.add_argument("--helper", action="store_true", help="word the brief for a helper (subagent)")
    br.add_argument("--check", action="store_true", help="only list what is missing; exit 1 if anything is")
    br.add_argument("--json", action="store_true")

    m = sub.add_parser("model", help="heuristic model picker and a backed-up default-model switch")
    msub = m.add_subparsers(dest="model_cmd", required=True)
    ms = msub.add_parser("suggest", help="suggest light / mid / top for a task (local rules, no model calls)")
    _add_context_args(ms)
    ms.add_argument("task", nargs="*", help="the task description")
    ms.add_argument("--file", type=Path, help="read the task description from this file")
    ms.add_argument("--json", action="store_true")
    md = msub.add_parser("default", help="set a tool's default model by tier (dry-run unless --apply)")
    _add_context_args(md)
    md.add_argument("--tier", choices=("light", "mid", "top"), required=True)
    md.add_argument("--tool", help="adapter id, e.g. claude (default: every detected tool with a tier map)")
    md.add_argument("--apply", action="store_true", help="write the change (backed up; undo restores it)")
    md.add_argument("--dry-run", action="store_true", help="show the plan only (the default)")

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
    print(f"{a.cmd}: not implemented yet", file=sys.stderr)
    return 3
