"""Compact mode: an optional rules block for shorter, cleaner reports. No saving is claimed.

Off by default. `compact on` adds the block to each detected tool's project instruction file,
`compact off` takes it out, and plain `compact` says where it is. Each adapter names its own file;
this module only plans and applies, through the same backup-and-manifest writer as setup, so
`undo` reverses the latest switch. No model calls, no network.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .adapters.base import COMPACT_START, SetupItem
from .context import Context
from .identity import CLI_NAME
from .installer import _now_stamp, apply_changes, compact_switch, kit_root, sha256_bytes, sha256_file
from .setup import _short, detected_adapters


def plan(ctx: Context, on: bool, adapters=None) -> list[SetupItem]:
    """One step per file to change. Two tools never get the block in files that both of them load."""
    items: list[SetupItem] = []
    planned: set[Path] = set()
    for ad in adapters if adapters is not None else detected_adapters(ctx):
        item = ad.compact_item(ctx, on, frozenset(planned))
        if item is None or any(Path(ch["path"]) in planned for ch in item.changes):
            continue
        planned.update(Path(ch["path"]) for ch in item.changes)
        if not on:
            for ch in item.changes:
                _exact_off(ctx, ch)
        items.append(item)
    return items


def _switched_on(ctx: Context, path: str) -> tuple[dict, Path] | None:
    """The record of the `compact on` that last changed `path`, with its backup folder.

    None when a later change to the file was recorded by anything else: then that record no longer
    says what the file was like before the block.
    """
    d = kit_root(ctx) / "manifests"
    for p in sorted(d.glob("*.json"), reverse=True) if d.is_dir() else []:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for rec in (data.get("files") or []) if isinstance(data, dict) else []:
            if rec.get("path") == path:
                on = compact_switch(data) == "on" and rec.get("action") in {"created", "modified"}
                return (rec, Path(str(data.get("backup_dir") or ""))) if on else None
    return None


def _exact_off(ctx: Context, change: dict) -> None:
    """Make `compact off` put the file back exactly as it was before `compact on`, where that is known.

    Taking the block out of the text cannot always tell what was there before: a file with no final
    line ending and one with a single line ending look the same once the block is appended. So when
    the file is still byte for byte what `compact on` wrote, the backup made then goes back, and a
    file that `compact on` created is deleted instead of being left empty. A file edited since keeps
    its edits: only the block is taken out of the text.
    """
    found = _switched_on(ctx, change["path"])
    if found is None:
        return
    rec, backup_dir = found
    path = Path(change["path"])
    left = change.get("content") or b""
    left = left.encode("utf-8", errors="surrogateescape") if isinstance(left, str) else left
    if rec.get("action") == "created":
        if sha256_file(path) == rec.get("sha256_after") or not left.strip():
            change["kind"], change["content"] = "delete", b""
            change["reason"] = "delete the file compact mode created; nothing else is in it"
        return
    if sha256_file(path) != rec.get("sha256_after") or not rec.get("backup"):
        return
    try:
        before = (backup_dir / rec["backup"]).read_bytes()
    except OSError:
        return
    if sha256_bytes(before) == rec.get("sha256_before"):
        change["content"] = before


def files_on(ctx: Context, adapters=None) -> list[Path]:
    """The instruction files that have the compact block now."""
    out: list[Path] = []
    for ad in adapters if adapters is not None else detected_adapters(ctx):
        path = ad.compact_file(ctx)
        if path is None or path in out or not path.is_file():
            continue
        if COMPACT_START in path.read_text(encoding="utf-8", errors="replace"):
            out.append(path)
    return out


def _run_id(ctx: Context) -> str:
    """A stamp no earlier manifest has, so two switches in one second do not share a manifest."""
    base, run, n = _now_stamp(), "", 1
    run = base
    while (kit_root(ctx) / "manifests" / f"{run}-01.json").exists():
        n += 1
        run = f"{base}x{n}"
    return run


def apply(ctx: Context, items: list[SetupItem], run_id: str | None = None, first: int = 1, *,
          on: bool = True) -> list[str]:
    """Write each step with a backup and a manifest. Returns the manifest paths.

    The manifest says which way the switch went, so `undo` can tell an `off` from an `on`.
    """
    run_id = run_id or _run_id(ctx)
    manifests = []
    for n, item in enumerate(items, first):
        done = apply_changes(ctx, item.changes, stamp=f"{run_id}-{n:02d}", profile="compact",
                             extra_manifest={"setup_run": run_id, "setup_item": item.id, "title": item.title,
                                             "compact": "on" if on else "off"})
        manifests.append(done["manifest_path"])
    return manifests


def run_compact(ctx: Context, action: str | None, *, dry_run: bool = False, as_json: bool = False,
                out: Callable[[str], None] = print) -> int:
    adapters = detected_adapters(ctx)
    if action not in {"on", "off"}:
        on_in = files_on(ctx, adapters)
        if as_json:
            out(json.dumps({"on": bool(on_in), "files": [str(p) for p in on_in]}, indent=2))
        elif on_in:
            out("Compact mode is on in: " + ", ".join(_short(str(p), ctx.home) for p in on_in))
            out(f"To turn it off: `{CLI_NAME} compact off`")
        else:
            out("Compact mode is off. It asks for complete answers in fewer words: targeted edits, "
                "pointers to code instead of quotes, no recaps.")
            out(f"To turn it on in this project: `{CLI_NAME} compact on`")
        return 0
    on = action == "on"
    items = plan(ctx, on, adapters)
    result = {"action": action, "mode": "dry-run" if dry_run else "applied", "manifests": [],
              "changes": [{"id": i.id, "title": i.title, "files": [ch["path"] for ch in i.changes]} for i in items]}
    if items and not dry_run:
        try:
            result["manifests"] = apply(ctx, items, on=on)
        except (OSError, RuntimeError, ValueError) as exc:
            out(f"compact stopped: {exc}")
            return 1
    if as_json:
        out(json.dumps(result, indent=2))
        return 0
    if not items:
        if on and files_on(ctx, adapters):
            out("Compact mode is already on. Nothing was changed.")
        elif on:
            out("Nothing was changed: no project instruction file to put the rules in. Run this in a project "
                "folder that Claude Code or Codex is used in, or one that already has a GEMINI.md or "
                ".github/copilot-instructions.md.")
        else:
            out("Compact mode is already off. Nothing was changed.")
        return 0
    for item in items:
        where = ", ".join(_short(ch["path"], ctx.home) for ch in item.changes)
        out(f"{'Would change' if dry_run else '✔'} {where}: {item.done if not dry_run else item.title}")
    if dry_run:
        out(f"Dry run: nothing was changed. To apply: `{CLI_NAME} compact {action}`")
    elif on:
        out(f"Compact mode is on for new sessions in this project. Each file was backed up first. "
            f"To turn it off: `{CLI_NAME} compact off` (or `{CLI_NAME} undo` right now).")
    else:
        out("Compact mode is off. Each file was backed up first.")
    return 0
