"""Run the doctor checks and remember which findings were already shown, so a later run reports only new ones.

No model is involved: this is the same local, read-only checkup as `doctor`. What it remembers is one
short hash per finding in `~/.tokenstretch/findings-seen.json` (no message, no path, no evidence).
A finding that goes away and later comes back counts as new again.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from .adapters import REGISTRY
from .findings import SEVERITY_ORDER, Finding
from .identity import STATE_DIRNAME, VERSION, state_dir
from .mask import mask_finding_evidence, mask_text

NOTICE_WIDTH = 160


def collect(ctx, privacy: bool = False, use_cache: bool = False) -> list[Finding]:
    """Every finding from the detected tools, worst first, with evidence masked."""
    from . import logcache

    findings: list[Finding] = []
    if use_cache and not privacy:
        logcache.open_disk_memo(ctx.home / STATE_DIRNAME / "cache" / "doctor-logs.json", VERSION)
    try:
        for ad in REGISTRY:
            if not ad.detect(ctx):
                continue
            findings += ad.privacy_checks(ctx) if privacy else ad.checks(ctx) + ad.privacy_checks(ctx)
    finally:
        logcache.save_disk_memo()
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.id))
    for f in findings:
        f.evidence = mask_finding_evidence(f.evidence)
    return findings


def finding_key(f: Finding) -> str:
    """Stable across runs: the check, its severity, the tool and the place. The message is left out because its counts move."""
    raw = "\x1f".join((f.adapter_id, f.id, f.severity, f.slug, str(f.source)))
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:20]


def seen_file(home: Path) -> Path:
    return state_dir(home) / "findings-seen.json"


def _load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data.get("seen"), dict) else None
    except (OSError, ValueError, AttributeError):
        return None


def new_since_last(home: Path, findings: list[Finding], now: float | None = None,
                   record: bool = True) -> tuple[list[Finding], bool]:
    """(findings not present at the last check, True when this is the first check).

    The first check has nothing to compare with, so it reports none as new and records all of them.
    """
    now = time.time() if now is None else now
    path = seen_file(home)
    old = _load(path)
    first = old is None
    seen = {} if first else old["seen"]
    current = {finding_key(f): f for f in findings}
    fresh = [] if first else [f for k, f in current.items() if k not in seen]
    if record:
        data = {"checked_at": int(now), "seen": {k: seen.get(k, int(now)) for k in current}}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass
    return fresh, first


def notice(fresh: list[Finding]) -> str:
    """One line for a desktop notification: how many, and the worst one."""
    top = fresh[0]
    text = mask_text(top.message)
    if len(text) > NOTICE_WIDTH:
        text = text[:NOTICE_WIDTH - 1].rstrip() + "…"
    more = f" (+{len(fresh) - 1} more)" if len(fresh) > 1 else ""
    return f"{top.severity.upper()} {top.id}: {text}{more}"
