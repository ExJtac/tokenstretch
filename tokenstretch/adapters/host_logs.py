"""Usage-log parsers for Cline and OpenCode (Copilot parsing moved to the Copilot adapter).

Cline and OpenCode paths live in those adapters; this module keeps the parsers and a
collect helper. Cline input is treated as uncached and flagged `unverified_semantics`.
OpenCode input already excludes cache (verified-in-source).
SQLite is opened read-only via a file: URI with mode=ro.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..context import Context
from ..findings import UsageRecord
from ..usage import (
    iter_jsonl,
    normalize_usage,
    open_sqlite_readonly,
    parse_ts,
    record_from_normalized,
)

UNVERIFIED = "unverified semantics: whether input includes cache is not confirmed"


def _cline_data(ctx: Context) -> Path:
    val, _ = ctx.env_or_rc("CLINE_DATA_DIR")
    if val:
        return Path(val).expanduser()
    cline_dir, _ = ctx.env_or_rc("CLINE_DIR")
    if cline_dir:
        return Path(cline_dir).expanduser() / "data"
    return ctx.home / ".cline" / "data"


def _cline_sessions(ctx: Context) -> Path:
    val, _ = ctx.env_or_rc("CLINE_SESSION_DATA_DIR")
    if val:
        return Path(val).expanduser()
    return _cline_data(ctx) / "sessions"


def _opencode_root(ctx: Context) -> Path:
    val, _ = ctx.env_or_rc("OPENCODE_DATA_DIR")
    if val:
        return Path(val.split(",")[0].strip()).expanduser()
    xdg, _ = ctx.env_or_rc("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "opencode"
    return ctx.home / ".local" / "share" / "opencode"


def _vscode_global_storage_roots(ctx: Context) -> list[Path]:
    # Standard VS Code layout [reasoning in usage-logs.md]; include common forks.
    base = ctx.home / ".config"
    names = ("Code", "Code - Insiders", "VSCodium", "Cursor", "Windsurf")
    return [base / n / "User" / "globalStorage" / "saoudrizwan.claude-dev" / "tasks" for n in names]


def default_paths(ctx: Context) -> list[Path]:
    """No host-owned log paths: Copilot, Cline, and OpenCode paths live in those adapters."""
    return []


def sniff(path: Path, first_lines: list[str]) -> bool:
    blob = "\n".join(first_lines)
    name = path.name
    if name == "events.jsonl" or "session.shutdown" in blob or "modelMetrics" in blob:
        return True
    if name.endswith(".messages.json") or "cacheReadTokens" in blob or "tokensIn" in blob:
        return True
    if name.endswith(".db") or "tokens" in blob and "cache" in blob:
        return True
    if path.parent.parent.name == "message" and path.parent.parent.parent.name == "storage":
        return True
    return "gen_ai.usage.input_tokens" in blob


def parse_path(path: Path, adapter_id: str = "host") -> tuple[list[UsageRecord], list[str]]:
    name = path.name
    if name.endswith(".db"):
        return parse_opencode_db(path, adapter_id)
    if name == "events.jsonl" or "otel" in path.parts:
        return parse_copilot(path, adapter_id)
    if name.endswith(".messages.json"):
        return parse_cline_messages(path, adapter_id)
    if name == "ui_messages.json":
        return parse_cline_ui_messages(path, adapter_id)
    if path.suffix == ".json":
        recs, notices = parse_opencode_legacy_json(path, adapter_id)
        if recs or notices:
            return recs, notices
        recs, notices = parse_copilot(path, adapter_id)
        if recs:
            return recs, notices
    # last resort: try jsonl copilot/otel
    if path.suffix == ".jsonl":
        return parse_copilot(path, adapter_id)
    return [], []


def parse_copilot(path: Path, adapter_id: str) -> tuple[list[UsageRecord], list[str]]:
    """Copilot events.jsonl / OTel file parsing lives in the Copilot adapter; this keeps old callers working."""
    from .copilot_cli import parse_events, parse_otel_file
    recs = parse_events(path, adapter_id) if path.name == "events.jsonl" else parse_otel_file(path, adapter_id)
    return recs, []


def parse_cline_messages(path: Path, adapter_id: str) -> tuple[list[UsageRecord], list[str]]:
    records: list[UsageRecord] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"skipped {path}: {exc}"]
    if not isinstance(data, dict):
        return [], []
    session_id = data.get("sessionId") or path.parent.name
    is_sub = data.get("agent") in {"subagent", "teammate"}
    for msg in data.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        metrics = msg.get("metrics") or {}
        if not metrics:
            continue
        model = ((msg.get("modelInfo") or {}).get("id")) or None
        cost = metrics.get("cost")
        norm = normalize_usage(
            input_tokens=int(metrics.get("inputTokens") or 0),
            cache_read=int(metrics.get("cacheReadTokens") or 0),
            cache_write=int(metrics.get("cacheWriteTokens") or 0),
            output=int(metrics.get("outputTokens") or 0),
            reasoning=0,
            input_includes_cache=True,
        )
        rec = record_from_normalized(
            adapter_id, "log", norm, session_id=session_id, model=model,
            ts=parse_ts(msg.get("ts") or data.get("updated_at")),
            is_subagent=is_sub, unverified_semantics=False, notes=[],
            cost_usd=float(cost) if cost is not None else None,
            extra={"path": str(path)},
        )
        records.append(rec)
    return records, []


def parse_cline_ui_messages(path: Path, adapter_id: str) -> tuple[list[UsageRecord], list[str]]:
    records: list[UsageRecord] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"skipped {path}: {exc}"]
    rows = data if isinstance(data, list) else data.get("messages") or []
    session_id = path.parent.name
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("say") != "api_req_started":
            continue
        raw = row.get("text") or "{}"
        try:
            info = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            continue
        if not isinstance(info, dict):
            continue
        cost = info.get("cost")
        norm = normalize_usage(
            input_tokens=int(info.get("tokensIn") or 0),
            cache_read=int(info.get("cacheReads") or 0),
            cache_write=int(info.get("cacheWrites") or 0),
            output=int(info.get("tokensOut") or 0),
            reasoning=0,
            input_includes_cache=False,
        )
        rec = record_from_normalized(
            adapter_id, "log", norm, session_id=session_id,
            ts=parse_ts(row.get("ts")),
            unverified_semantics=True, notes=[UNVERIFIED],
            cost_usd=float(cost) if cost is not None else None,
            extra={"path": str(path), "legacy": True},
        )
        records.append(rec)
    return records, []


def parse_opencode_db(path: Path, adapter_id: str) -> tuple[list[UsageRecord], list[str]]:
    records: list[UsageRecord] = []
    notices: list[str] = []
    seen_ids: set[str] = set()
    try:
        conn = open_sqlite_readonly(path)
    except Exception as exc:
        return [], [f"skipped {path}: {exc}"]
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # Prefer per-step message rows over cumulative session_v2 totals.
        if "session_message" in tables:
            rows = conn.execute(
                "SELECT id, session_id, type, seq, time_created, data "
                "FROM session_message WHERE type IN ('assistant', 'compaction') ORDER BY session_id, seq"
            )
            # Fork copies share seq prefixes; drop prefix duplicates per session later.
            by_session: dict[str, list[tuple]] = {}
            for row in rows:
                data = row["data"]
                if isinstance(data, (bytes, str)):
                    try:
                        data = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                if not isinstance(data, dict):
                    continue
                tokens = data.get("tokens") or {}
                cache = tokens.get("cache") or {}
                model = (data.get("model") or {}).get("id")
                cost = data.get("cost")
                mid = row["id"]
                sid = row["session_id"]
                norm = normalize_usage(
                    input_tokens=int(tokens.get("input") or 0),
                    cache_read=int(cache.get("read") or 0),
                    cache_write=int(cache.get("write") or 0),
                    output=int(tokens.get("output") or 0),
                    reasoning=int(tokens.get("reasoning") or 0),
                    input_includes_cache=False,
                )
                rec = record_from_normalized(
                    adapter_id, "log", norm, session_id=str(sid), task_id=str(mid),
                    model=model, ts=parse_ts(row["time_created"] or (data.get("time") or {}).get("created")),
                    cost_usd=float(cost) if cost is not None else None,
                    extra={"path": str(path), "seq": row["seq"]},
                )
                by_session.setdefault(str(sid), []).append((row["seq"], mid, rec))
                seen_ids.add(str(mid))
            # session_v2.fork_session_id: don't add session_v2 totals; message rows are enough.
            # Drop fork copies that replay the parent's seq/fingerprint prefix via drop_forked_replays later.
            for sid, items in by_session.items():
                records.extend(r for _, _, r in items)
        elif "message" in tables:
            rows = conn.execute("SELECT * FROM message")
            for row in rows:
                data = row["data"] if "data" in row.keys() else None
                if isinstance(data, (bytes, str)):
                    try:
                        data = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                if not isinstance(data, dict):
                    continue
                tokens = data.get("tokens") or {}
                cache = tokens.get("cache") or {}
                if not tokens:
                    continue
                mid = data.get("id") or (row["id"] if "id" in row.keys() else None)
                if mid and str(mid) in seen_ids:
                    continue
                norm = normalize_usage(
                    input_tokens=int(tokens.get("input") or 0),
                    cache_read=int(cache.get("read") or 0),
                    cache_write=int(cache.get("write") or 0),
                    output=int(tokens.get("output") or 0),
                    reasoning=int(tokens.get("reasoning") or 0),
                    input_includes_cache=False,
                )
                rec = record_from_normalized(
                    adapter_id, "log", norm,
                    session_id=str(data.get("sessionID") or data.get("session_id") or ""),
                    task_id=str(mid or ""),
                    model=data.get("modelID") or (data.get("model") or {}).get("id"),
                    cost_usd=float(data["cost"]) if data.get("cost") is not None else None,
                    extra={"path": str(path), "v1": True},
                )
                records.append(rec)
        elif "session_v2" in tables:
            # Only if no message rows existed. These are cumulative; one record per session.
            notices.append(f"{path}: using session_v2 cumulative totals (no session_message rows)")
            for row in conn.execute(
                "SELECT * FROM session_v2"
            ):
                keys = row.keys()
                if "fork_session_id" in keys and row["fork_session_id"]:
                    continue  # don't add fork totals on top of the parent
                model = None
                if "model" in keys and row["model"]:
                    try:
                        m = json.loads(row["model"]) if isinstance(row["model"], str) else row["model"]
                        model = (m or {}).get("id")
                    except json.JSONDecodeError:
                        model = None
                norm = normalize_usage(
                    input_tokens=int(row["tokens_input"] if "tokens_input" in keys else 0),
                    cache_read=int(row["tokens_cache_read"] if "tokens_cache_read" in keys else 0),
                    cache_write=int(row["tokens_cache_write"] if "tokens_cache_write" in keys else 0),
                    output=int(row["tokens_output"] if "tokens_output" in keys else 0),
                    reasoning=int(row["tokens_reasoning"] if "tokens_reasoning" in keys else 0),
                    input_includes_cache=False,
                )
                rec = record_from_normalized(
                    adapter_id, "log", norm,
                    session_id=str(row["id"] if "id" in keys else ""),
                    model=model,
                    cost_usd=float(row["cost"]) if "cost" in keys and row["cost"] is not None else None,
                    extra={"path": str(path), "session_v2": True},
                )
                records.append(rec)
    finally:
        conn.close()
    return records, notices


def parse_opencode_legacy_json(path: Path, adapter_id: str) -> tuple[list[UsageRecord], list[str]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], [f"skipped {path}: {exc}"]
    if not isinstance(data, dict):
        return [], []
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        return [], []
    cache = tokens.get("cache") or {}
    cost = data.get("cost")
    norm = normalize_usage(
        input_tokens=int(tokens.get("input") or 0),
        cache_read=int(cache.get("read") or 0),
        cache_write=int(cache.get("write") or 0),
        output=int(tokens.get("output") or 0),
        reasoning=int(tokens.get("reasoning") or 0),
        input_includes_cache=False,
    )
    rec = record_from_normalized(
        adapter_id, "log", norm,
        session_id=str(data.get("sessionID") or path.parent.name),
        task_id=str(data.get("id") or path.stem),
        model=data.get("modelID") or (data.get("model") or {}).get("id"),
        ts=parse_ts((data.get("time") or {}).get("created") or data.get("time")),
        cost_usd=float(cost) if cost is not None else None,
        extra={"path": str(path), "legacy_json": True},
    )
    return [rec], []


def collect_usage(adapter, ctx, paths, since_days: int = 30) -> list:
    """Window and dedupe records from `adapter.parse_log` over `paths`."""
    import time
    from ..usage import drop_forked_replays, record_in_window
    cutoff = time.time() - since_days * 86400
    adapter.last_log_notices = []
    records = []
    for path in paths:
        recs = adapter.parse_log(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = cutoff
        for r in recs:
            if r.ts:
                if record_in_window(r, cutoff, None):
                    records.append(r)
            elif mtime >= cutoff:
                records.append(r)
    return drop_forked_replays(drop_legacy_json_dupes(records))


def drop_legacy_json_dupes(records: list) -> list:
    """OpenCode: one assistant message can exist in both opencode.db and legacy
    storage/message/<sid>/<mid>.json (v1->v2 migration).
    "dedupe by message id". The DB row wins; the legacy JSON copy is dropped."""
    db_ids = {r.task_id for r in records if r.task_id and not (r.extra or {}).get("legacy_json")}
    out, seen_legacy = [], set()
    for r in records:
        if (r.extra or {}).get("legacy_json") and r.task_id:
            if r.task_id in db_ids or r.task_id in seen_legacy:
                continue
            seen_legacy.add(r.task_id)
        out.append(r)
    return out

