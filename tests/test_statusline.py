"""statusline (one read-only line for a status bar) and watch's cache-expiry check."""
from __future__ import annotations

import io
import json
import time
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from conftest import CANARIES, credential_guard

from tokenstretch.cli import main
from tokenstretch.context import build_context
from tokenstretch.statusline import build_line, light_context, read_payload, run_statusline
from tokenstretch.watch import Watcher, WatchConfig, cache_misses, run_watch

T0 = datetime(2026, 1, 5, 17, 0, 0, tzinfo=timezone.utc)
SONNET = "claude-sonnet-4-5"


def _row(i: int, at_s: int, *, read: int, write: int = 0, model: str = SONNET, out: int = 900) -> dict:
    ts = (T0 + timedelta(seconds=at_s)).isoformat().replace("+00:00", "Z")
    return {"type": "assistant", "timestamp": ts, "message": {
        "id": f"m{i}", "model": model, "content": [{"type": "text", "text": "ok"}],
        "usage": {"input_tokens": 10, "cache_read_input_tokens": read, "cache_creation_input_tokens": write,
                  "output_tokens": out}}}


def _world(tmp_path: Path, rows: list[dict], settings: dict | None = None) -> Path:
    home, root = tmp_path / "home", tmp_path / "root"
    proj = home / ".claude" / "projects" / "-proj"
    proj.mkdir(parents=True)
    root.mkdir()
    (tmp_path / "sys").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps(settings or {"model": "sonnet"}), encoding="utf-8")
    (home / ".claude" / ".credentials.json").write_text("CANARY_CREDENTIAL_DO_NOT_READ_CLAUDE")
    (proj / "abcd1234-0000.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return tmp_path


def _ctx(w: Path, plan: str | None = "subscription"):
    return build_context(w / "home", w / "root", w / "sys", plan)


def _log(w: Path) -> Path:
    return w / "home" / ".claude" / "projects" / "-proj" / "abcd1234-0000.jsonl"


# A warm session, then a 50-minute pause, then a step that re-wrote the whole history.
EXPIRED = [_row(0, 0, read=100_000), _row(1, 40, read=101_000),
           _row(2, 40 + 50 * 60, read=4_000, write=99_000), _row(3, 80 + 50 * 60, read=103_000, write=1_000)]


# ---- watch: cache expiry ------------------------------------------------------------------

def test_watch_reports_a_recorded_cache_expiry_once(tmp_path):
    w = _world(tmp_path, EXPIRED)
    lines: list[str] = []
    with credential_guard():
        run_watch(_ctx(w), once=True, out=lines.append)
    out = "\n".join(lines)
    assert out.count("⚠") == 1
    assert "discount expired once during a pause (longest ~50 min)" in out
    assert "~99K tokens were re-sent" in out and "/clear" in out
    for c in CANARIES:
        assert c not in out


def test_cache_misses_ignores_compaction_model_switch_and_short_gaps():
    cfg = WatchConfig()
    turns = lambda rows: [  # noqa: E731
        {"ts": r["timestamp"], "model": r["message"]["model"],
         "history_tokens": sum(r["message"]["usage"][k] for k in
                               ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")),
         "cache_read": r["message"]["usage"]["cache_read_input_tokens"],
         "cache_write": r["message"]["usage"]["cache_creation_input_tokens"]} for r in rows]
    assert len(cache_misses(turns(EXPIRED), cfg)) == 1
    # compaction: history shrank to a fifth, so the write is a new small cache, not an expiry
    assert cache_misses(turns([_row(0, 0, read=100_000), _row(1, 3600, read=0, write=20_000)]), cfg) == []
    # model switch rebuilds the cache for another reason (doctor W08 covers it)
    assert cache_misses(turns([_row(0, 0, read=100_000),
                               _row(1, 3600, read=0, write=100_000, model="claude-opus-4-1")]), cfg) == []
    # no pause: a big write right after a step is new content, not an expiry
    assert cache_misses(turns([_row(0, 0, read=100_000), _row(1, 60, read=0, write=100_000)]), cfg) == []
    # caching off entirely (nothing written): W09's job, not this check
    assert cache_misses(turns([_row(0, 0, read=0), _row(1, 3600, read=0)]), cfg) == []
    # turns without cache counts (other tools) are skipped
    bare = [{"ts": t["ts"], "model": t["model"], "history_tokens": t["history_tokens"]} for t in turns(EXPIRED)]
    assert cache_misses(bare, cfg) == []


def test_watch_predicts_expiry_for_an_idle_session_by_cache_lifetime(tmp_path):
    w = _world(tmp_path, [_row(0, 0, read=80_000), _row(1, 40, read=81_000)])
    with credential_guard():
        assert Watcher(_ctx(w)).scan() == []                                           # just written
        assert Watcher(_ctx(w), now=lambda: time.time() + 30 * 60).scan() == []        # inside one hour
        warns = Watcher(_ctx(w), now=lambda: time.time() + 90 * 60).scan()
    assert [x.key[-1] for x in warns] == ["cache"]
    assert "idle for 90 min" in warns[0].what and "about 60 min" in warns[0].what
    assert "/clear" in warns[0].fix and "/compact would re-read" in warns[0].fix


def test_idle_prediction_uses_five_minutes_for_api_keys_and_the_5m_setting(tmp_path):
    w = _world(tmp_path, [_row(0, 0, read=80_000), _row(1, 40, read=81_000)])
    later = lambda: time.time() + 10 * 60  # noqa: E731
    with credential_guard():
        assert Watcher(_ctx(w), now=later).scan() == []
        api = Watcher(_ctx(w, plan="api"), now=later).scan()
    assert [x.key[-1] for x in api] == ["cache"] and "about 5 min" in api[0].what
    w2 = _world(tmp_path / "b", [_row(0, 0, read=80_000), _row(1, 40, read=81_000)],
                settings={"model": "sonnet", "env": {"FORCE_PROMPT_CACHING_5M": "1"}})
    with credential_guard():
        forced = Watcher(_ctx(w2), now=later).scan()
    assert [x.key[-1] for x in forced] == ["cache"]


def test_small_idle_session_is_not_worth_a_warning(tmp_path):
    w = _world(tmp_path, [_row(0, 0, read=5_000), _row(1, 40, read=6_000)])
    with credential_guard():
        assert Watcher(_ctx(w), now=lambda: time.time() + 90 * 60).scan() == []


# ---- statusline ----------------------------------------------------------------------------

def test_statusline_clean_session_shows_size_and_model(tmp_path):
    w = _world(tmp_path, [_row(0, 0, read=20_000), _row(1, 40, read=22_000)])
    with credential_guard():
        line = build_line(_ctx(w), {"transcript_path": str(_log(w))})
    assert line == f"22K/150K history · {SONNET}"


def test_statusline_shows_the_most_urgent_warning(tmp_path):
    rows = EXPIRED + [_row(4, 120 + 50 * 60, read=199_000, write=1_000)]
    w = _world(tmp_path, rows)
    with credential_guard():
        line = build_line(_ctx(w), {"transcript_path": str(_log(w))})
        cache_only = build_line(_ctx(w), {"transcript_path": str(_log(w))}, WatchConfig(max_context=500_000))
    assert line == "⚠ 200K/150K history · /clear at the next task break"      # long history outranks the rest
    assert cache_only.startswith("⚠ 200K/500K history · cache expired after ~50 min idle (~99K re-sent)")
    assert "\n" not in line


def test_statusline_without_a_payload_uses_the_newest_active_session(tmp_path):
    w = _world(tmp_path, [_row(0, 0, read=20_000), _row(1, 40, read=22_000)])
    with credential_guard():
        assert build_line(_ctx(w), {}) == f"22K/150K history · {SONNET}"
        assert build_line(_ctx(w), {}, now=lambda: time.time()) != ""
    (tmp_path / "empty" / "home").mkdir(parents=True)
    assert build_line(light_context(tmp_path / "empty" / "home", tmp_path / "empty")) == ""


def test_statusline_only_opens_session_logs(tmp_path):
    w = _world(tmp_path, [_row(0, 0, read=20_000)])
    cred = w / "home" / ".claude" / ".credentials.json"
    outside = w / "root" / "notes.jsonl"
    outside.write_text(json.dumps(_row(0, 0, read=20_000)) + "\n", encoding="utf-8")
    link = _log(w).parent / "link.jsonl"
    link.symlink_to(cred)
    with credential_guard():
        for p in (cred, outside, link, w / "home" / "missing.jsonl"):
            assert build_line(_ctx(w), {"transcript_path": str(p)}) == ""


def test_statusline_never_fails_and_is_read_only(tmp_path):
    w = _world(tmp_path, EXPIRED)
    before = {p: p.read_bytes() for p in (w / "home").rglob("*") if p.is_file() and p.name != ".credentials.json"}
    assert read_payload(io.StringIO("not json")) == {} and read_payload(io.StringIO("[1]")) == {}
    assert read_payload(io.StringIO("")) == {}
    printed: list[str] = []
    with credential_guard():
        for text in ("not json", "", json.dumps({"transcript_path": 7}),
                     json.dumps({"transcript_path": str(_log(w))})):
            assert run_statusline(w / "home", w / "root", "subscription", stdin=io.StringIO(text),
                                  out=printed.append) == 0
    assert len(printed) == 4 and all(p.startswith("⚠ 104K/150K history · cache expired") for p in printed)
    after = {p: p.read_bytes() for p in (w / "home").rglob("*") if p.is_file() and p.name != ".credentials.json"}
    assert before == after


def test_statusline_cli(tmp_path, monkeypatch):
    w = _world(tmp_path, [_row(0, 0, read=20_000), _row(1, 40, read=160_000)])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"transcript_path": str(_log(w))})))
    buf = io.StringIO()
    with credential_guard(), redirect_stdout(buf):
        code = main(["statusline", "--home", str(w / "home"), "--root", str(w / "root"), "--max-context", "100000"])
    assert code == 0 and buf.getvalue().strip().startswith("⚠ 160K/100K history · ")
    assert buf.getvalue().count("\n") == 1


# ---- --wrap: share the status-line slot with another tool ---------------------------------------

def test_wrap_adds_our_line_after_the_other_tools_first_line(tmp_path):
    import sys
    from tokenstretch.statusline import join_lines, run_wrapped
    w = _world(tmp_path, [_row(0, 0, read=20_000), _row(1, 40, read=22_000)])
    payload = json.dumps({"transcript_path": str(_log(w)), "model": {"display_name": "Sonnet"}})
    # A stand-in for another tool's status line: it reads the same JSON and prints two lines.
    other = tmp_path / "other.py"
    other.write_text("import json,sys\nd=json.load(sys.stdin)\nprint('[CAVE] ' + d['model']['display_name'])\nprint('line two')\n",
                     encoding="utf-8")
    cmd = f'"{sys.executable}" "{other}"'
    assert run_wrapped(cmd, payload) == "[CAVE] Sonnet\nline two"          # it got the same input
    printed: list[str] = []
    with credential_guard():
        run_statusline(w / "home", w / "root", "subscription", stdin=io.StringIO(payload), out=printed.append, wrap=cmd)
    assert printed == [f"[CAVE] Sonnet · 22K/150K history · {SONNET}\nline two"]
    assert join_lines("a", "") == "a" and join_lines("", "b") == "b" and join_lines("  \n", "b") == "b"


def test_wrap_failures_never_cost_either_line(tmp_path):
    import sys
    from tokenstretch.statusline import run_wrapped
    w = _world(tmp_path, [_row(0, 0, read=20_000), _row(1, 40, read=22_000)])
    payload = json.dumps({"transcript_path": str(_log(w))})
    ours = f"22K/150K history · {SONNET}"
    slow = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    assert run_wrapped("exit 3", payload) == "" and run_wrapped("definitely-not-a-command-xyz", payload) == ""
    assert run_wrapped(slow, payload, timeout=0.3) == ""
    for bad in ("exit 3", "definitely-not-a-command-xyz"):                  # the other tool broke: ours still shows
        printed: list[str] = []
        with credential_guard():
            assert run_statusline(w / "home", w / "root", "subscription", stdin=io.StringIO(payload),
                                  out=printed.append, wrap=bad) == 0
        assert printed == [ours]
    printed = []                                                            # ours has nothing: theirs still shows
    assert run_statusline(tmp_path / "nohome", tmp_path, None, stdin=io.StringIO("{}"), out=printed.append,
                          wrap="echo THEIRS") == 0
    assert printed == ["THEIRS"]


def test_wrap_cli(tmp_path, monkeypatch):
    w = _world(tmp_path, [_row(0, 0, read=20_000), _row(1, 40, read=22_000)])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"transcript_path": str(_log(w))})))
    buf = io.StringIO()
    with credential_guard(), redirect_stdout(buf):
        code = main(["statusline", "--home", str(w / "home"), "--root", str(w / "root"), "--wrap", "echo CAVE"])
    assert code == 0 and buf.getvalue() == f"CAVE · 22K/150K history · {SONNET}\n"
