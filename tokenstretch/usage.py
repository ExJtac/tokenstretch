"""Shared, tool-path-free helpers for reading and normalizing usage logs.

Normalization rules:
  {input_uncached, cache_read, cache_write, output, reasoning}
- Codex: input includes cache; uncached = input − cached − cache_write, clamped at 0.
- OpenCode: input already excludes cache.
- Cline metrics.inputTokens already includes cache; uncached = input − cacheRead − cacheWrite, clamped at 0. Legacy ui_messages.json tokensIn is still unverified.
- Copilot: treat input as uncached and flag unverified semantics.
Never invent cost figures. Missing data is represented as None / "no measurement".
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .findings import UsageRecord

NO_MEASUREMENT = "no measurement"


def clamp_included_cache(input_tokens: int, cached: int, cache_write: int) -> tuple[int, int, int]:
    """Codex/OpenAI: cached and cache_write are subsets of input_tokens (ccusage clamp)."""
    input_tokens = max(0, int(input_tokens or 0))
    cached = max(0, int(cached or 0))
    cache_write = max(0, int(cache_write or 0))
    cached = min(cached, input_tokens)
    cache_write = min(cache_write, max(0, input_tokens - cached))
    uncached = max(0, input_tokens - cached - cache_write)
    return uncached, cached, cache_write


def normalize_usage(
    *,
    input_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    output: int = 0,
    reasoning: int = 0,
    input_includes_cache: bool = False,
) -> dict[str, int]:
    if input_includes_cache:
        uncached, cache_read, cache_write = clamp_included_cache(input_tokens, cache_read, cache_write)
    else:
        uncached = max(0, int(input_tokens or 0))
        cache_read = max(0, int(cache_read or 0))
        cache_write = max(0, int(cache_write or 0))
    return {
        "input_uncached": uncached,
        "cache_read": cache_read,
        "cache_write": cache_write,
        "output": max(0, int(output or 0)),
        "reasoning": max(0, int(reasoning or 0)),
    }


def record_from_normalized(
    adapter_id: str,
    source: str,
    norm: dict[str, int],
    **kwargs,
) -> UsageRecord:
    return UsageRecord(
        adapter_id=adapter_id,
        source=source,
        input_tokens=norm["input_uncached"],
        cache_read_tokens=norm["cache_read"],
        cache_write_tokens=norm["cache_write"],
        output_tokens=norm["output"],
        reasoning_tokens=norm["reasoning"],
        **kwargs,
    )


def is_zst_jsonl(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith(".jsonl.zst") or path.suffixes[-2:] == [".jsonl", ".zst"]


def _decompress_zst(data: bytes) -> bytes | None:
    """Use stdlib only. compression.zstd landed in Python 3.14; this runtime may not have it."""
    try:
        from compression.zstd import ZstdDecompressor  # type: ignore
    except ImportError:
        return None
    try:
        return ZstdDecompressor().decompress(data)
    except Exception:
        return None


def read_log_text(path: Path) -> tuple[str | None, str | None]:
    """Return (text, notice). For .jsonl.zst, skip with a notice when stdlib cannot decompress."""
    try:
        if is_zst_jsonl(path):
            raw = path.read_bytes()
            text = _decompress_zst(raw)
            if text is None:
                return None, (
                    f"skipped {path}: .jsonl.zst requires stdlib compression.zstd "
                    "(Python 3.14+); this runtime cannot decompress it."
                )
            return text.decode("utf-8", errors="replace"), None
        return path.read_text(encoding="utf-8", errors="replace"), None
    except OSError as exc:
        return None, f"skipped {path}: {exc}"


def iter_jsonl(path: Path):
    """Yield (obj, notice). A leading (None, notice) is used when the file cannot be read."""
    text, notice = read_log_text(path)
    if notice and text is None:
        yield None, notice
        return
    if not text:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line), None
        except json.JSONDecodeError:
            continue


def sqlite_ro_uri(path: Path) -> str:
    """file: URI with mode=ro. Avoids urllib (P00 forbids network-library imports)."""
    resolved = path.resolve()
    s = resolved.as_posix().replace("%", "%25").replace("?", "%3F").replace("#", "%23")
    return f"file:{s}?mode=ro"


def open_sqlite_readonly(path: Path) -> sqlite3.Connection:
    """Open a SQLite database read-only so a live writer (OpenCode/Cline) is not disturbed."""
    conn = sqlite3.connect(sqlite_ro_uri(path), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def fingerprint(rec: UsageRecord) -> tuple:
    return (
        rec.input_tokens,
        rec.cache_read_tokens,
        rec.cache_write_tokens,
        rec.output_tokens,
        rec.reasoning_tokens,
        rec.model or "",
    )


def drop_forked_replays(records: list[UsageRecord]) -> list[UsageRecord]:
    """Drop a forked session's leading records when they replay another session's prefix."""
    by_session: dict[str, list[UsageRecord]] = {}
    order: list[str] = []
    leftover: list[UsageRecord] = []
    for rec in records:
        sid = rec.session_id or ""
        if not sid:
            leftover.append(rec)
            continue
        if sid not in by_session:
            by_session[sid] = []
            order.append(sid)
        by_session[sid].append(rec)

    drop_n: dict[str, int] = {}
    sids = list(order)
    for i, sid in enumerate(sids):
        seq = [fingerprint(r) for r in by_session[sid]]
        if not seq:
            continue
        best = 0
        for other in sids:
            if other == sid:
                continue
            oseq = [fingerprint(r) for r in by_session[other]]
            n = 0
            while n < len(seq) and n < len(oseq) and seq[n] == oseq[n]:
                n += 1
            # Only treat it as a replay if the other session is at least as long
            # and the shared prefix is at least one event, and this session continues
            # or is a copy. Skip identical full copies of the shorter one? If equal
            # length and identical, drop the later session entirely.
            if n < 2:
                # A single matching event is not enough to call it a fork replay
                # (independent sessions can share the same usage numbers).
                continue
            if n == len(seq) and n == len(oseq):
                # full duplicate: drop the later session (higher index)
                if sids.index(other) < i:
                    best = max(best, n)
            elif n < len(seq) and n <= len(oseq):
                # this session starts with the other's prefix (typical fork)
                if n == len(oseq) or sids.index(other) <= i:
                    best = max(best, n)
        if best:
            drop_n[sid] = best

    out: list[UsageRecord] = []
    for sid in order:
        recs = by_session[sid]
        n = drop_n.get(sid, 0)
        if n:
            for rec in recs[n:]:
                rec.notes = list(rec.notes) + [f"dropped {n} forked-session replay prefix event(s)"]
                out.append(rec)
        else:
            out.extend(recs)
    out.extend(leftover)
    return out


def parse_ts(value) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        # epoch milliseconds or seconds
        n = float(value)
        if n > 1e12:
            n = n / 1000.0
        import datetime
        return datetime.datetime.fromtimestamp(n, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def record_in_window(rec: UsageRecord, cutoff_epoch: float | None, until_epoch: float | None) -> bool:
    if cutoff_epoch is None and until_epoch is None:
        return True
    if not rec.ts:
        return True  # file-level mtime already applied by the caller when ts is missing
    epoch = _ts_to_epoch(rec.ts)
    if epoch is None:
        return True
    if cutoff_epoch is not None and epoch < cutoff_epoch:
        return False
    if until_epoch is not None and epoch > until_epoch:
        return False
    return True


def _ts_to_epoch(ts: str) -> float | None:
    import datetime
    text = ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def measured_or_none(values: list[int | None]) -> int | None:
    if not values or any(v is None for v in values):
        return None
    return int(sum(values))


def format_count(value: int | None) -> str:
    return NO_MEASUREMENT if value is None else str(value)
