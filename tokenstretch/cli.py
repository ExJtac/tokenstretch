"""CLI: setup explains then applies with backups; status/report are read-only; doctor is read-only; install defaults to dry-run; undo restores; bench is --from-logs, API-key --bench, or subscription --prepare/--measure."""
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
    path = write_report(ctx, args.out)
    print(f"Report written (local file, no network): {path}")
    return 0


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
    print(f"{a.cmd}: not implemented yet", file=sys.stderr)
    return 3
