"""The on-disk per-log cache behind `doctor` (stat-keyed, local, safe to delete)."""
from __future__ import annotations

import io
import json
import os
import time
from contextlib import redirect_stdout

from tokenstretch import logcache
from tokenstretch.cli import main


def _old(path, age=600):
    t = time.time() - age
    os.utime(path, (t, t))


def test_disk_memo_reuses_a_still_log_and_rereads_a_changed_one(tmp_path):
    log, store = tmp_path / "a.jsonl", tmp_path / "cache" / "m.json"
    log.write_text("x\n")
    _old(log)
    calls = []
    fn = lambda: calls.append(1) or [1, [2, 3]]
    logcache._MEMO.clear()
    logcache.open_disk_memo(store, "v")
    assert logcache.memo_by_stat(log, "codex-w14", fn) == [1, [2, 3]]
    assert logcache.save_disk_memo() and store.is_file()
    logcache._MEMO.clear()                       # a new process
    logcache.open_disk_memo(store, "v")
    assert logcache.memo_by_stat(log, "codex-w14", fn) == [1, [2, 3]] and len(calls) == 1
    assert logcache.save_disk_memo() is False    # nothing new: no write
    log.write_text("x\ny\n")
    _old(log, 300)
    logcache._MEMO.clear()
    logcache.open_disk_memo(store, "v")
    logcache.memo_by_stat(log, "codex-w14", fn)
    assert len(calls) == 2
    logcache.save_disk_memo()


def test_disk_memo_skips_live_logs_other_tags_and_other_versions(tmp_path):
    log, store = tmp_path / "a.jsonl", tmp_path / "m.json"
    log.write_text("x\n")                        # just written: a live session
    logcache._MEMO.clear()
    logcache.open_disk_memo(store, "v")
    logcache.memo_by_stat(log, "codex-w14", lambda: 5)
    assert logcache.save_disk_memo() is False and not store.exists()
    _old(log)
    logcache._MEMO.clear()
    logcache.open_disk_memo(store, "v")
    logcache.memo_by_stat(log, "claude-turns", lambda: 5)      # not a disk tag
    assert logcache.save_disk_memo() is False
    logcache.open_disk_memo(store, "v")
    logcache.memo_by_stat(log, "codex-w14", lambda: 5)
    assert logcache.save_disk_memo()
    calls = []
    logcache._MEMO.clear()
    logcache.open_disk_memo(store, "other-version")
    logcache.memo_by_stat(log, "codex-w14", lambda: calls.append(1) or 5)
    assert calls == [1]
    logcache.save_disk_memo()
    store.write_text("{not json")                 # damaged: treated as empty, never an error
    logcache._MEMO.clear()
    logcache.open_disk_memo(store, "v")
    assert logcache.memo_by_stat(log, "codex-w14", lambda: 7) == 7
    logcache.save_disk_memo()
    assert json.loads(store.read_text())["rows"]


def test_disk_memo_drops_rows_for_deleted_logs(tmp_path):
    a, b, store = tmp_path / "a.jsonl", tmp_path / "b.jsonl", tmp_path / "m.json"
    for p in (a, b):
        p.write_text("x\n")
        _old(p)
    logcache._MEMO.clear()
    logcache.open_disk_memo(store, "v")
    logcache.memo_by_stat(a, "codex-w14", lambda: 1)
    logcache.save_disk_memo()
    a.unlink()
    logcache.open_disk_memo(store, "v")
    logcache.memo_by_stat(b, "codex-w14", lambda: 2)
    logcache.save_disk_memo()
    rows = json.loads(store.read_text())["rows"]
    assert len(rows) == 1 and next(iter(rows)).endswith("b.jsonl")


def _doctor(home, root, sys_dir, *extra):
    buf = io.StringIO()
    logcache._MEMO.clear()
    with redirect_stdout(buf):
        main(["doctor", "--json", "--home", str(home), "--root", str(root), "--sys-snapshot", str(sys_dir), *extra])
    return buf.getvalue()


def test_doctor_gives_the_same_answer_cold_warm_and_uncached(tmp_path):
    home, root, sys_dir = tmp_path / "home", tmp_path / "root", tmp_path / "sys"
    proj = home / ".claude" / "projects" / "-proj"
    proj.mkdir(parents=True)
    root.mkdir()
    sys_dir.mkdir()
    (home / ".claude" / "settings.json").write_text("{}")
    rows = [{"type": "assistant", "message": {"id": f"m{i}", "model": "claude-opus-4",
             "usage": {"input_tokens": 5, "cache_read_input_tokens": 400_000, "cache_creation_input_tokens": 100,
                       "output_tokens": 10}}} for i in range(3)]
    log = proj / "abc12345.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    _old(log)
    store = home / ".tokenstretch" / "cache" / "doctor-logs.json"
    plain = _doctor(home, root, sys_dir, "--no-cache")
    assert not store.exists()                     # --no-cache writes nothing
    cold = _doctor(home, root, sys_dir)
    assert store.is_file()
    warm = _doctor(home, root, sys_dir)
    assert plain == cold == warm and "W14" in plain
    assert json.dumps(str(home))[1:-1] in store.read_text() and "claude-opus" not in store.read_text()   # paths and totals only
