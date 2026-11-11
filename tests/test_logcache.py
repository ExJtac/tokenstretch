"""Incremental log cache: unchanged files skipped, appended bytes only, shrink/replace re-parse,
partial last lines, window skip by mtime, and a relative perf check on a large synthetic log."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conftest import credential_guard

from tokenstretch.adapters.claude_code import ClaudeCodeAdapter
from tokenstretch.adapters.codex import CodexAdapter
from tokenstretch.context import build_context
from tokenstretch.logcache import LogCache, split_complete

NOW = datetime.now(timezone.utc)


def _assistant(i: int, *, out: int = 50, pad: int = 0) -> str:
    ts = (NOW - timedelta(minutes=5) + timedelta(milliseconds=i)).isoformat().replace("+00:00", "Z")
    return json.dumps({"type": "assistant", "timestamp": ts, "message": {
        "id": f"msg{i}", "model": "claude-sonnet-4-5",
        "content": [{"type": "text", "text": "x" * pad}],
        "usage": {"input_tokens": 1, "cache_read_input_tokens": 1000 + i, "cache_creation_input_tokens": 0,
                  "output_tokens": out}}})


def _user(i: int, pad: int = 200) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": "y" * pad}]}})


def _world(tmp_path: Path):
    home, root = tmp_path / "home", tmp_path / "root"
    proj = home / ".claude" / "projects" / "-proj"
    proj.mkdir(parents=True)
    root.mkdir()
    (tmp_path / "sys").mkdir()
    (home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    (home / ".claude" / ".credentials.json").write_text("CANARY_CREDENTIAL_DO_NOT_READ_CLAUDE")
    return build_context(home, root, tmp_path / "sys"), proj


def _write(path: Path, lines: list[str], mode="w"):
    with open(path, mode, encoding="utf-8") as fh:
        fh.write("".join(l + "\n" for l in lines))


def test_split_complete_keeps_partial_line():
    lines, used = split_complete(b'{"a":1}\n{"b":')
    assert lines == [b'{"a":1}'] and used == 8
    lines, used = split_complete(b'{"a":1}\n{"b":2}')  # a final line without newline but valid JSON
    assert lines == [b'{"a":1}', b'{"b":2}'] and used == 15


def test_append_parses_only_new_bytes_and_matches_full_parse(tmp_path):
    ctx, proj = _world(tmp_path)
    f = proj / "s1.jsonl"
    _write(f, [x for i in range(50) for x in (_user(i), _assistant(i))])
    ad = ClaudeCodeAdapter()
    cache = LogCache()
    with credential_guard():
        assert len(cache.records(ctx, [ad])) == 50
        assert cache.stats["full"] == 1
        size1 = f.stat().st_size
        # unchanged: skipped entirely
        cache.records(ctx, [ad])
        assert cache.stats["unchanged"] == 1 and cache.stats["bytes_read"] == size1
        # append 10 messages, the last one only half written
        tail = _assistant(60)
        with open(f, "a", encoding="utf-8") as fh:
            fh.write("".join(_assistant(i) + "\n" for i in range(50, 59)) + tail[:40])
        before = cache.stats["bytes_read"]
        recs = cache.records(ctx, [ad])
        assert len(recs) == 59 and cache.stats["appended"] == 1
        assert cache.stats["bytes_read"] - before == f.stat().st_size - size1  # only the new bytes
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(tail[40:] + "\n")
        recs = cache.records(ctx, [ad])
        assert len(recs) == 60
        # a streamed update of an existing message (same id, more output) replaces it, not adds
        _write(f, [_assistant(3, out=999)], mode="a")
        recs = cache.records(ctx, [ad])
        assert len(recs) == 60 and [r.output_tokens for r in recs if r.task_id == "msg3"] == [999]
        full = ad.finish_usage_records(ad.parse_log(f))
        assert [(r.task_id, r.output_tokens) for r in recs] == [(r.task_id, r.output_tokens) for r in full]


def test_shrink_or_replace_reparses(tmp_path):
    ctx, proj = _world(tmp_path)
    f = proj / "s1.jsonl"
    _write(f, [_assistant(i) for i in range(20)])
    ad, cache = ClaudeCodeAdapter(), LogCache()
    with credential_guard():
        assert len(cache.records(ctx, [ad])) == 20
        _write(f, [_assistant(i) for i in range(5)])  # shrank
        assert len(cache.records(ctx, [ad])) == 5 and cache.stats["full"] == 2
        g = proj / "new.tmp"
        _write(g, [_assistant(i) for i in range(100, 108)])
        os.replace(g, f)  # new inode
        assert len(cache.records(ctx, [ad])) == 8 and cache.stats["full"] == 3


def test_old_files_skipped_by_mtime_before_opening(tmp_path):
    ctx, proj = _world(tmp_path)
    old = proj / "old.jsonl"
    _write(old, [_assistant(i) for i in range(5)])
    t = time.time() - 90 * 86400
    os.utime(old, (t, t))
    sess = ctx.home / ".codex" / "archived_sessions" / "2025" / "01"
    sess.mkdir(parents=True)
    rollout = sess / "rollout-2025-01-01T00-00-00-old.jsonl"
    rollout.write_text('{"type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":'
                       '{"input_tokens":5,"output_tokens":1}}}}\n', encoding="utf-8")
    os.utime(rollout, (t, t))
    opened = []
    import builtins
    real = builtins.open

    def spy(file, *a, **k):
        opened.append(str(file))
        return real(file, *a, **k)

    builtins.open = spy
    try:
        cache = LogCache()
        assert cache.records(ctx, [ClaudeCodeAdapter(), CodexAdapter()], since_days=56) == []
    finally:
        builtins.open = real
    assert str(old) not in opened and str(rollout) not in opened
    assert cache.stats["skipped_old"] == 2


def test_codex_rollout_appends_incrementally(tmp_path):
    ctx, _ = _world(tmp_path)
    d = ctx.home / ".codex" / "sessions" / "2026" / "11" / "11"
    d.mkdir(parents=True)
    f = d / "rollout-2026-11-11T10-00-00-cx.jsonl"

    def tc(i):
        ts = (NOW - timedelta(minutes=10 - i)).isoformat().replace("+00:00", "Z")
        return json.dumps({"type": "event_msg", "timestamp": ts, "payload": {"type": "token_count", "info": {
            "last_token_usage": {"input_tokens": 100 + i, "cached_input_tokens": 0, "output_tokens": 5},
            "total_token_usage": {"input_tokens": 1000 * (i + 1), "output_tokens": 5 * (i + 1)}}}})
    big = json.dumps({"type": "response_item", "payload": {"type": "message", "content": "z" * 5000}})
    _write(f, [json.dumps({"type": "session_meta", "payload": {"id": "cx"}}),
               json.dumps({"type": "turn_context", "payload": {"model": "gpt-5-codex"}}), big, tc(0), tc(1)])
    ad, cache = CodexAdapter(), LogCache()
    with credential_guard():
        recs = cache.records(ctx, [ad])
        assert [r.input_tokens for r in recs] == [100, 101] and recs[0].model == "gpt-5-codex"
        _write(f, [big, tc(2)], mode="a")
        recs = cache.records(ctx, [ad])
        assert [r.input_tokens for r in recs] == [100, 101, 102] and recs[-1].session_id == "cx"
        assert cache.stats["appended"] == 1


def test_second_build_is_much_faster_on_a_large_log(tmp_path):
    """Relative timing only (no absolute numbers), so slow CI machines don't make it flaky."""
    ctx, proj = _world(tmp_path)
    lines = []
    for i in range(25_000):
        lines += [_user(i, pad=400), _assistant(i, pad=300)]
    _write(proj / "big.jsonl", lines)  # 50k lines
    ad, cache = ClaudeCodeAdapter(), LogCache()
    t0 = time.perf_counter()
    first = cache.records(ctx, [ad])
    t1 = time.perf_counter()
    second = cache.records(ctx, [ad])
    t2 = time.perf_counter()
    assert len(first) == len(second) == 25_000
    assert (t2 - t1) * 5 < (t1 - t0), f"cached pass {t2 - t1:.3f}s vs first {t1 - t0:.3f}s"
