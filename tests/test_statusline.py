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
SONNET = "claude-sonnet-5-5"   # the mid model: limits are not price-scaled for it


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


# ---- heavy start: history on the first step, before any work --------------------------------------

def test_watch_warns_when_a_session_starts_heavy(tmp_path):
    prompt = {"type": "user", "message": {"role": "user", "content": "build the thing"}}
    w = _world(tmp_path, [prompt, _row(0, 0, read=10_000, write=68_000), _row(1, 40, read=79_000)])
    with credential_guard():
        warns = Watcher(_ctx(w)).scan()
        quiet = Watcher(_ctx(w), WatchConfig(max_start=100_000)).scan()
        line = build_line(_ctx(w), {"transcript_path": str(_log(w))})
    assert [x.key[-1] for x in warns] == ["startup"] and quiet == []
    assert "started at ~78K tokens before doing any work" in warns[0].what
    assert "tokenstretch startup" in warns[0].fix and "/clear" not in warns[0].fix
    assert "ask the agent to run `tokenstretch startup` (or run it in a terminal)" in warns[0].fix
    assert line == "⚠ 79K/150K history · starts at 78K before any work: tokenstretch startup"
    skill = w / "home" / ".claude" / "skills" / "credits" / "SKILL.md"       # with the shortcut: no terminal needed
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: credits\n---\n", encoding="utf-8")
    with credential_guard():
        warns = Watcher(_ctx(w)).scan()
        line = build_line(_ctx(w), {"transcript_path": str(_log(w))})
    assert warns[0].fix.startswith("type `/credits` to see what it is made of") and "terminal" not in warns[0].fix
    assert line == "⚠ 79K/150K history · starts at 78K before any work: /credits"


def test_light_start_and_missing_first_step_stay_quiet(tmp_path):
    from tokenstretch.adapters.claude_code import _first_history_tokens
    prompt = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}
    tool = {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]}}
    w = _world(tmp_path, [prompt, tool, _row(0, 0, read=20_000), _row(1, 40, read=22_000)])
    assert _first_history_tokens(_log(w)) == 20_010                       # tool results are not prompts
    assert _first_history_tokens(_log(w), head_bytes=10) is None          # first usage line not in the head
    assert _first_history_tokens(tmp_path / "missing.jsonl") is None
    with credential_guard():
        assert Watcher(_ctx(w)).scan() == []
    # Not a true start: no prompt before the first step (a log tail), two prompts, or a summary.
    heavy = _row(0, 0, read=10_000, write=90_000)
    summary = {"type": "user", "isCompactSummary": True, "message": {"role": "user", "content": "summary"}}
    for n, rows in enumerate(([heavy], [prompt, prompt, heavy], [summary, heavy])):
        wn = _world(tmp_path / f"n{n}", rows)
        assert _first_history_tokens(_log(wn)) is None
        with credential_guard():
            assert [x.key[-1] for x in Watcher(_ctx(wn)).scan()] == []


# ---- helpers (subagents) and handoff payback ---------------------------------------------------------

def _helper_log(w: Path, name: str, rows: list[dict]) -> None:
    folder = _log(w).with_suffix("") / "subagents"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"agent-{name}.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_watch_reports_helpers_that_cost_more_than_the_main_conversation(tmp_path):
    w = _world(tmp_path, [_row(0, 0, read=30_000), _row(1, 40, read=32_000)])
    _helper_log(w, "a", [_row(i, 40 * i, read=50_000 + 3_000 * i) for i in range(20)])     # ~1.57M sent
    _helper_log(w, "b", [_row(i, 40 * i, read=50_000) for i in range(4)])                   # ~0.2M sent
    with credential_guard():
        warns = Watcher(_ctx(w)).scan()
        line = build_line(_ctx(w), {"transcript_path": str(_log(w))})
        quiet = Watcher(_ctx(w), WatchConfig(helper_min_sent=5_000_000)).scan()
    assert [x.key[-1] for x in warns] == ["helpers"] and quiet == []
    assert "2 helpers in this session re-sent ~1.8M tokens" in warns[0].what
    assert "The longest ran 20 steps and reached ~107K" in warns[0].what and "narrow" in warns[0].fix
    assert line.startswith("⚠ 32K/150K history · helpers re-sent ~1.8M (")


def test_small_or_cheaper_helpers_stay_quiet(tmp_path):
    main = [_row(i, 40 * i, read=100_000) for i in range(30)]                                # ~3M sent by main
    w = _world(tmp_path, main)
    _helper_log(w, "a", [_row(i, 40 * i, read=60_000) for i in range(20)])                  # 1.2M, under main
    with credential_guard():
        assert [x.key[-1] for x in Watcher(_ctx(w)).scan()] == []
    w2 = _world(tmp_path / "b", [_row(0, 0, read=30_000)])
    _helper_log(w2, "a", [_row(i, 40 * i, read=50_000) for i in range(5)])                  # over main, under 1M
    with credential_guard():
        assert Watcher(_ctx(w2)).scan() == []


def test_long_history_warning_says_when_a_fresh_start_pays_back(tmp_path):
    from tokenstretch.watch import payback_steps
    assert payback_steps(133_000, 78_000) == 5 and payback_steps(200_000, 100_000) == 4
    assert payback_steps(80_000, None) is None and payback_steps(70_000, 78_000) is None
    prompt = {"type": "user", "message": {"role": "user", "content": "go"}}
    w = _world(tmp_path, [prompt, _row(0, 0, read=30_000), _row(1, 40, read=199_990)])
    with credential_guard():
        warn = [x for x in Watcher(_ctx(w)).scan() if x.key[-1] == "context"][0]
        line = build_line(_ctx(w), {"transcript_path": str(_log(w))})
    assert "pays for itself after about 3 more steps (this session started at ~30K" in warn.fix
    assert line == "⚠ 200K/150K history · /clear at the next task break (pays back in ~3 steps)"


# ---- nudge: the same warning, shown inside the session by a prompt hook ------------------------

def test_nudge_says_the_top_warning_once_then_waits(tmp_path):
    from tokenstretch.nudge import build_nudge
    w = _world(tmp_path, [_row(0, 0, read=20_000), _row(1, 40, read=199_000, write=1_000)])
    payload = {"transcript_path": str(_log(w)), "session_id": "abcd1234-0000", "hook_event_name": "UserPromptSubmit"}
    clock = [1_000_000.0]
    with credential_guard():
        first = build_nudge(_ctx(w), payload, now=lambda: clock[0])
        again = build_nudge(_ctx(w), payload, now=lambda: clock[0] + 60)
        later = build_nudge(_ctx(w), payload, now=lambda: clock[0] + 21 * 60)
    assert first["systemMessage"].startswith("tokenstretch: ") and "200K" in first["systemMessage"]
    extra = first["hookSpecificOutput"]
    assert extra["hookEventName"] == "UserPromptSubmit" and "not a request from the user" in extra["additionalContext"]
    assert again is None                      # same warning a minute later: stay quiet
    assert later is not None                  # still true 21 minutes later: say it once more
    state = w / "home" / ".tokenstretch" / "nudge" / "abcd1234-0000.json"
    assert json.loads(state.read_text())["kind"] == "context"


def test_nudge_is_silent_when_clean_or_unsure_and_never_fails(tmp_path):
    from tokenstretch.nudge import build_nudge, run_nudge
    w = _world(tmp_path, [_row(0, 0, read=20_000), _row(1, 40, read=22_000)])
    with credential_guard():
        assert build_nudge(_ctx(w), {"transcript_path": str(_log(w))}) is None          # nothing to warn about
        assert build_nudge(_ctx(w), {}) is None                                         # no session named: no guess
        assert build_nudge(_ctx(w), {"transcript_path": str(w / "root" / "x.jsonl")}) is None
    assert not (w / "home" / ".tokenstretch" / "nudge").exists()
    printed: list[str] = []
    for raw in ("not json", "", json.dumps({"transcript_path": 5})):
        assert run_nudge(w / "home", w / "root", stdin=io.StringIO(raw), out=printed.append) == 0
    assert printed == []


def test_nudge_ignores_a_session_id_that_is_not_a_plain_name(tmp_path):
    from tokenstretch.nudge import build_nudge
    w = _world(tmp_path, [_row(0, 0, read=20_000), _row(1, 40, read=199_000, write=1_000)])
    payload = {"transcript_path": str(_log(w)), "session_id": "../../escape"}
    with credential_guard():
        assert build_nudge(_ctx(w), payload) is not None
    assert not (w / "home" / ".tokenstretch").exists() or not list((w / "home" / ".tokenstretch").rglob("*.json"))


def test_nudge_cli_prints_one_json_object(tmp_path, monkeypatch):
    w = _world(tmp_path, [_row(0, 0, read=20_000), _row(1, 40, read=199_000, write=1_000)])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"transcript_path": str(_log(w)), "session_id": "s1"})))
    buf = io.StringIO()
    with credential_guard(), redirect_stdout(buf):
        code = main(["nudge", "--home", str(w / "home"), "--root", str(w / "root")])
    assert code == 0 and json.loads(buf.getvalue())["systemMessage"].startswith("tokenstretch: ")


# ---- startup: what a session carries before the first prompt -----------------------------------

def test_startup_sizes_local_parts_largest_first(tmp_path):
    from tokenstretch.startup import build, format_report
    w = _world(tmp_path, [_row(0, 0, read=20_000)])
    home, root = w / "home", w / "root"
    (root / "CLAUDE.md").write_text("x" * 8_000, encoding="utf-8")
    (root / "pkg").mkdir()
    (root / "pkg" / "CLAUDE.md").write_text("y" * 40_000, encoding="utf-8")       # loads only when working in pkg/
    for name, extra in (("listed", ""), ("manual", "disable-model-invocation: true\n")):
        d = home / ".claude" / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {'d' * 396}\n{extra}---\nbody\n", encoding="utf-8")
    with credential_guard():
        data = build(_ctx(w))
    claude = next(d for d in data if d["adapter"] == "claude-code")
    names = [(p["kind"], p["name"], p["tokens"]) for p in claude["parts"]]
    assert names[0][0] == "instruction file" and names[0][1].endswith("root/CLAUDE.md") and names[0][2] == 2_000
    assert ("skill listing (user)", "listed", 100) in names
    assert not any(n == "manual" for _, n, _ in names)             # manual-only skills cost nothing per session
    assert not any("pkg" in n for _, n, _ in names)
    assert claude["known"] == sum(t for _, _, t in names) and claude["measured_start"] is None and claude["rest"] is None
    text = "\n".join(format_report(data))
    assert "Sized from local files: ~2.1K tokens" in text and "no recent session with a clean first step" in text


def test_startup_cli_json_and_rest(tmp_path, monkeypatch):
    import tokenstretch.startup as st
    w = _world(tmp_path, [_row(0, 0, read=20_000)])
    (w / "root" / "CLAUDE.md").write_text("x" * 8_000, encoding="utf-8")
    monkeypatch.setattr(st, "measured_starts", lambda ctx, ad, limit=5: [60_000, 58_000])
    buf = io.StringIO()
    with credential_guard(), redirect_stdout(buf):
        code = main(["startup", "--home", str(w / "home"), "--root", str(w / "root"), "--sys-snapshot", str(w / "sys"), "--json"])
    d = next(x for x in json.loads(buf.getvalue()) if x["adapter"] == "claude-code")
    assert code == 0 and d["measured_start"] == 60_000 and d["rest"] == 60_000 - d["known"] and d["recent_starts"] == [60_000, 58_000]
    lines = "\n".join(st.format_report([d]))
    assert "Recent sessions: 60K, 58K." in lines and "can't be sized from local files" in lines


def test_startup_sizes_a_connector_by_difference(tmp_path, monkeypatch):
    import tokenstretch.startup as st
    w = _world(tmp_path, [_row(0, 0, read=20_000)])
    home, root = w / "home", w / "root"
    mcp = root / ".mcp.json"
    args = ["startup", "--home", str(home), "--root", str(root), "--sys-snapshot", str(w / "sys"), "--json"]

    def run(start: int, servers: list[str], *extra: str) -> dict:
        mcp.write_text(json.dumps({"mcpServers": {n: {"command": "x"} for n in servers}}), encoding="utf-8")
        monkeypatch.setattr(st, "measured_starts", lambda ctx, ad, limit=5: [start])
        buf = io.StringIO()
        with credential_guard(), redirect_stdout(buf):
            assert main([*args, *extra]) == 0
        return next(x for x in json.loads(buf.getvalue()) if x["adapter"] == "claude-code")

    first = run(60_000, ["big", "small"])
    assert [p["name"] for p in first["unsized"]] == ["big (project)", "small (project)"] and first["turned_off"] == []
    hist = home / ".tokenstretch" / "startup-history.json"
    assert hist.is_file() and "big (project)|small (project)" in hist.read_text(encoding="utf-8")

    same = run(52_000, ["big", "small"], "--no-save")            # same set, 8K lighter: something else changed
    assert same["since_last"] == -8_000 and "start ~8.0K tokens lighter" in "\n".join(st.format_report([same]))
    assert run(60_900, ["big", "small"], "--no-save")["since_last"] == 0

    off = run(42_000, ["small"])                                   # big turned off: 18K lighter
    assert off["turned_off"] == [{"name": "big (project)", "tokens": 18_000}]
    assert "big (project) is off: sessions now start ~18K tokens lighter" in "\n".join(st.format_report([off]))

    back = run(60_000, ["big", "small"], "--no-save")              # on again: sized from the remembered run
    big = next(p for p in back["parts"] if p["name"] == "big (project)")
    assert big["tokens"] == 18_000 and big["kind"] == "connector (measured by difference)"
    assert [p["name"] for p in back["unsized"]] == ["small (project)"] and back["rest"] == first["rest"] - 18_000

    assert st.size_by_difference({"k": {"connectors": ["small (project)"], "rest": 40_500, "ts": 1}},
                                 ["big (project)", "small (project)"], 42_000) == ({}, {})   # under 2K: noise
    assert st.size_by_difference({"k": {"rest": "x"}}, [], 1) == ({}, {})


def test_startup_no_save_writes_nothing(tmp_path, monkeypatch):
    import tokenstretch.startup as st
    w = _world(tmp_path, [_row(0, 0, read=20_000)])
    monkeypatch.setattr(st, "measured_starts", lambda ctx, ad, limit=5: [60_000])
    with credential_guard(), redirect_stdout(io.StringIO()):
        assert main(["startup", "--home", str(w / "home"), "--root", str(w / "root"), "--sys-snapshot", str(w / "sys"), "--no-save"]) == 0
    assert not (w / "home" / ".tokenstretch" / "startup-history.json").exists()


# ---- helper wake-ups ---------------------------------------------------------------------------------

def _notice(at_s: int, *, origin: bool = True) -> dict:
    ts = (T0 + timedelta(seconds=at_s)).isoformat().replace("+00:00", "Z")
    row = {"type": "user", "timestamp": ts,
           "message": {"role": "user", "content": "<task-notification>\n<status>completed</status> HELPER_REPLY_TEXT"}}
    if origin:
        row["origin"] = {"kind": "task-notification"}
    return row


def test_watch_counts_steps_started_by_finished_helpers(tmp_path):
    rows = [_row(0, 0, read=60_000)]
    for i in range(1, 5):                                   # four notices, each followed by two steps
        rows += [_notice(100 * i, origin=i % 2 == 0), _row(2 * i, 100 * i + 5, read=100_000 + 5_000 * i),
                 _row(2 * i + 1, 100 * i + 40, read=101_000 + 5_000 * i)]
    w = _world(tmp_path, rows)
    with credential_guard():
        warns = Watcher(_ctx(w)).scan()
        line = build_line(_ctx(w), {"transcript_path": str(_log(w))})
        quiet = Watcher(_ctx(w), WatchConfig(wake_min=5)).scan()
        small = Watcher(_ctx(w), WatchConfig(wake_min_sent=1_000_000)).scan()
    wake = [x for x in warns if x.key[-1] == "wakeups"]
    assert len(wake) == 1 and "wakeups" not in [x.key[-1] for x in quiet + small]
    assert "woke this conversation 4 times" in wake[0].what and "re-sent ~450K tokens" in wake[0].what
    assert "largest ~120K" in wake[0].what and "one final report" in wake[0].fix
    assert "helpers woke this chat 4x" in line
    assert "HELPER_REPLY_TEXT" not in "".join(x.what + x.why + x.fix for x in warns) + line


def test_ordinary_user_messages_are_not_wake_ups(tmp_path):
    said = {"type": "user", "message": {"role": "user", "content": "what does a task-notification row mean?"}}
    tool = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "content": "<task-notification> quoted in a file"}]}}
    rows = [_row(0, 0, read=100_000)]
    for i in range(1, 6):
        rows += [said, tool, _row(i, 50 * i, read=100_000 + 1_000 * i)]
    w = _world(tmp_path, rows)
    with credential_guard():
        assert "wakeups" not in [x.key[-1] for x in Watcher(_ctx(w)).scan()]


def test_doctor_w28_reports_helper_wakeups_from_the_logs(tmp_path):
    from argparse import Namespace
    from tokenstretch.cli import doctor

    def ids(w):
        buf = io.StringIO()
        with credential_guard(), redirect_stdout(buf):
            doctor(Namespace(home=w / "home", root=w / "root", sys_snapshot=w / "sys", plan=None, privacy=False, json=True))
        return [f for f in json.loads(buf.getvalue()) if f["id"] == "W28"]

    rows = [_row(0, 0, read=60_000)]
    for i in range(1, 5):
        rows += [_notice(100 * i), _row(i, 100 * i + 5, read=100_000 + 5_000 * i)]
    hit = ids(_world(tmp_path / "a", rows))
    assert len(hit) == 1 and hit[0]["severity"] == "med" and str(hit[0]["evidence"]["value"]) == "450040"
    assert "woke the main conversation 4 times" in hit[0]["message"] and "about 0.5M tokens" in hit[0]["message"]
    assert ids(_world(tmp_path / "b", rows[:5])) == []            # two wake-ups: under the floor
