"""pace: calibration from readings, the gate, window rollover, model filter, team and what-if maths, watch check."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import datetime, timezone

import pytest
from conftest import credential_guard
from test_watch_model import _ctx, _row, _world

from tokenstretch import pace as pc
from tokenstretch.cli import main
from tokenstretch.watch import Watcher

TOP, MID = "claude-fable-5-2", "claude-sonnet-5-5"
T0 = datetime(2026, 1, 5, 0, 0, 0, tzinfo=timezone.utc).timestamp()
DAY = 86400
RESETS = T0 + 7 * DAY


def _turn(at: float, model: str = TOP, effort: str = "max", out: int = 100, **extra) -> dict:
    # 100 output tokens on the top model = 100 * 4 * 5 = 2000 units
    return {"ts": pc.iso(at), "model": model, "effort": effort, "history_tokens": 0, "cache_read": 0,
            "cache_write": 0, "output_tokens": out, **extra}


def _sess(*turns: dict, helpers=None) -> dict:
    return {"turns": list(turns), "helpers": helpers or []}


def _state(*readings, models=None, resets=RESETS, name="weekly") -> dict:
    st = {"windows": {}}
    for used, at in readings:
        pc.set_reading(st, name, used, resets, at, length_s=7 * DAY, models=models)
    return st


def _win(state, sessions, now, reserve=3.0, only=None):
    return pc.evaluate(state, sessions, now, reserve, only)


def test_one_reading_calibrates_from_units_since_window_start():
    now = T0 + 3.5 * DAY
    done = [_turn(T0 + i * 60) for i in range(100)]                 # 200000 units before the reading
    later = [_turn(now + 60 + i) for i in range(10)]                 # 20000 units after it (not yet in a reading)
    st = _state((50, now))
    w = _win(st, [_sess(*done, *later)], now + 3600)[0]
    assert w["calibrated"] and w["method"].startswith("units since window start")
    assert w["units_per_pct"] == pytest.approx(4000)
    assert w["est_used"] == pytest.approx(55)
    assert w["time_share"] == pytest.approx(0.5, abs=0.01)
    assert w["pace_ratio"] == pytest.approx(1.1, abs=0.03)
    assert w["units_left"] == pytest.approx(45 * 4000)
    assert w["allowance_per_day"] == pytest.approx(45 * 4000 / w["days_left"], rel=0.01)


def test_two_readings_use_the_slope_between_them():
    t1, t2 = T0 + 1 * DAY, T0 + 3 * DAY
    turns = ([_turn(T0 + 60 + i) for i in range(20)]                  # 40000 units by the first reading
             + [_turn(T0 + 1.5 * DAY + i) for i in range(60)])        # 120000 more by the second
    st = _state((20, t1), (40, t2))
    w = _win(st, [_sess(*turns)], t2)[0]
    assert w["method"] == "slope between readings"
    assert w["units_per_pct"] == pytest.approx(6000)                  # one reading alone would say 4000
    assert w["est_used"] == pytest.approx(40)
    # a reading with no extra percent falls back to one-reading maths
    st2 = _state((40, t1), (40, t2))
    assert _win(st2, [_sess(*turns)], t2)[0]["method"].startswith("units since window start")


def test_gate_on_both_sides_of_the_line_and_the_wait():
    now = T0 + 3.5 * DAY
    sessions = [_sess(*[_turn(T0 + i * 60) for i in range(100)])]    # 200000 units
    over = pc.gate(_win(_state((50, now)), sessions, now), now)       # line = 50 - 3 = 47
    assert over["ok"] is False and over["window"] == "weekly"
    assert over["used"] == pytest.approx(50) and over["line"] == pytest.approx(47, abs=0.1)
    assert over["wait_seconds"] == pytest.approx((50 + 3) / 100 * 7 * DAY - 3.5 * DAY, abs=2)
    under = pc.gate(_win(_state((40, now)), sessions, now), now)
    assert under["ok"] is True and under["wait_seconds"] == 0
    # reserve moves the line: 50% used is on the line with a reserve of 0
    assert pc.gate(_win(_state((50, now)), sessions, now, reserve=0), now)["ok"] is True
    # several windows: one over blocks, --window checks only the named one
    st = _state((50, now))
    pc.set_reading(st, "5h", 5, now + 3600, now, length_s=5 * 3600)
    both = _win(st, sessions, now)
    assert pc.gate(both, now)["ok"] is False
    assert pc.gate(_win(st, sessions, now, only="5h"), now)["ok"] is True


def test_gate_with_no_reading_never_blocks(tmp_path):
    g = pc.gate([], T0)
    assert g["ok"] is True and g["window"] is None and "not blocking" in g["note"]
    w = _world(tmp_path, [_row(0, TOP, 1000)])
    args = ["--home", str(w / "home"), "--root", str(w / "root"), "--sys-snapshot", str(w / "sys")]
    buf = io.StringIO()
    with redirect_stdout(buf), credential_guard():
        assert main(["pace", "--gate", *args, "--json"]) == 0
    assert json.loads(buf.getvalue()) == {"ok": True, "wait_seconds": 0, "window": None, "used": None, "line": None}
    assert not (w / "home" / ".tokenstretch" / "pace.json").exists()


def test_window_rollover_restarts_readings_and_a_finished_window_is_silent():
    st = _state((50, T0 + 3 * DAY))
    assert pc.set_reading(st, "weekly", 55, RESETS + 120, T0 + 4 * DAY, length_s=7 * DAY) == "added"   # reset time jitter
    assert len(st["windows"]["weekly"]["readings"]) == 2
    assert pc.set_reading(st, "weekly", 5, RESETS + 7 * DAY, RESETS + 100, length_s=7 * DAY) == "restarted"
    assert len(st["windows"]["weekly"]["readings"]) == 1
    assert pc.set_reading(st, "weekly", 2, RESETS + 7 * DAY, RESETS + 200, length_s=7 * DAY) == "corrected"
    old = _state((90, T0 + 3 * DAY))
    sessions = [_sess(*[_turn(T0 + i * 60) for i in range(50)])]
    after = RESETS + 3600
    w = _win(old, sessions, after)[0]
    assert w["finished"] and not w["calibrated"]
    assert pc.gate([w], after)["ok"] is True
    assert pc.watch_over(old, sessions, after) == []
    # the new period counts units only from its own start
    new = _state((10, RESETS + DAY), resets=RESETS + 7 * DAY)
    fresh = _win(new, [_sess(*[_turn(T0 + i * 60) for i in range(50)], _turn(RESETS + 0.5 * DAY))], RESETS + DAY)[0]
    assert fresh["units_used"] == pytest.approx(2000)


def test_models_filter_counts_only_matching_models():
    now = T0 + 3.5 * DAY
    turns = [_turn(T0 + i * 60) for i in range(100)] + [_turn(T0 + i * 60 + 30, model=MID, out=1000) for i in range(100)]
    top = _win(_state((50, now), models="fable"), [_sess(*turns)], now)[0]
    allm = _win(_state((50, now)), [_sess(*turns)], now)[0]
    assert top["units_used"] == pytest.approx(200_000)                # sonnet: 1000 * 1 * 5 = 5000 per turn
    assert allm["units_used"] == pytest.approx(200_000 + 500_000)
    assert top["units_per_pct"] == pytest.approx(4000)
    with pytest.raises(Exception):
        pc.set_reading({"windows": {}}, "x", 1, RESETS, T0, models="(")


def test_unpriced_models_are_left_out_and_reported():
    now = T0 + 3.5 * DAY
    turns = [_turn(T0 + i * 60) for i in range(100)] + [_turn(T0 + i * 60 + 30, model="gpt-9-nova", out=5000) for i in range(7)]
    w = _win(_state((50, now)), [_sess(*turns)], now)[0]
    assert w["units_used"] == pytest.approx(200_000)
    assert w["unpriced"] == {"gpt-9-nova": 7}
    text = "\n".join(pc.format_windows([w], {}, w))
    assert "gpt-9-nova (7 steps)" in text and "not a bill" in text


def _team_sessions():
    # 100 steps an hour for the first hour of the window: 36 s apart, 2000 units each
    return [_sess(*[_turn(T0 + 36 * i) for i in range(100)])]


def test_team_maths_with_nearest_effort_label():
    now = T0 + 3.5 * DAY
    sessions = _team_sessions()
    wins = _win(_state((1, now)), sessions, now)
    stats = pc.step_stats(sessions)
    t = pc.team_view(wins, stats, 8, TOP, "max", now)
    w = wins[0]
    rate = 2000 * 100                                                  # units per agent-hour
    assert t["ok"] and t["units_per_step"] == pytest.approx(2000) and t["steps_per_hour"] == pytest.approx(100)
    at_once = w["units_left"] / ((parse := pc.parse_time(w["resets"]) - now) / 3600) / rate
    assert t["agents_at_once"] == pytest.approx(at_once)
    assert t["hours_per_day"] == pytest.approx(min(24, 24 * at_once / 8))
    assert t["note"] == ""
    near = pc.team_view(wins, stats, 8, TOP, "low", now)
    assert near["effort"] == "max" and "nearest measured effort, max" in near["note"]
    assert "nearest measured effort" in "\n".join(pc.format_team(near))
    none = pc.team_view(wins, stats, 8, "claude-opus-5-5", "max", now)
    assert none["ok"] is False and "no steps" in none["note"]
    # a window limited to other models does not count for this model
    only_mid = _win(_state((1, now), models="sonnet"), sessions, now)
    assert pc.team_view(only_mid, stats, 8, TOP, "max", now)["ok"] is False


def test_what_if_removes_resaved_history_and_halves_rereads():
    now = T0 + 3.5 * DAY
    turns = [_turn(T0 + 60, model=MID, effort="high", out=10),
             {**_turn(T0 + 120, model=MID, effort="high", out=0), "history_tokens": 40_000, "cache_write": 40_000},
             {**_turn(T0 + 180, model=MID, effort="high", out=0), "history_tokens": 100_000, "cache_read": 100_000}]
    sessions = [_sess(*turns)]
    # units: 50 output + 40000 re-saved * 1.25 = 50000 + 100000 re-read * 0.05 = 5000 -> 55050
    st = _state((10, now))
    wins = _win(st, sessions, now)
    wi = pc.what_if(st, sessions, wins, None, now)["windows"][0]
    assert wi["units_used"] == pytest.approx(55_050)
    assert wi["units_saved"] == pytest.approx(50_000 + 2_500)
    assert wi["factor"] == pytest.approx(55_050 / 2_550)
    assert wi["est_used_with_cuts"] == pytest.approx(10 - 52_500 / wins[0]["units_per_pct"])
    # team: more agent-hours by the same factor
    sess2 = _team_sessions()
    w2 = _win(_state((1, now)), sess2, now)
    team = pc.team_view(w2, pc.step_stats(sess2), 8, TOP, "max", now)
    wi2 = pc.what_if(_state((1, now)), sess2, w2, team, now)
    assert wi2["team"]["factor"] == pytest.approx(1.0)                 # no history buckets in this log: no gain
    assert wi2["team"]["hours_per_day_with_cuts"] == pytest.approx(wi2["team"]["hours_per_day"])


def test_steps_left_today_uses_measured_units_per_step():
    now = T0 + 3.5 * DAY
    sessions = _team_sessions()
    w = _win(_state((1, now)), sessions, now)[0]
    t = pc.steps_today(w, pc.step_stats(sessions), TOP, "max")
    assert t["steps"] == int(w["allowance_per_day"] / 2000) and t["units_per_step"] == pytest.approx(2000)


def test_watch_warns_ten_points_over_the_line_and_stays_silent_otherwise(tmp_path):
    now = T0 + 3.5 * DAY
    sessions = [_sess(*[_turn(T0 + i * 60) for i in range(100)])]
    assert pc.watch_over(_state((59, now)), sessions, now) == []        # 9 points over
    assert len(pc.watch_over(_state((60, now)), sessions, now)) == 1    # 10 points over
    assert pc.watch_over({"windows": {}}, sessions, now) == []

    class Fake:
        id, display_name = "claude", "Claude"
        def watch_sessions(self, ctx, limit=5, max_age_s=None):
            return sessions
        window_sessions = watch_sessions

    ctx = _ctx(_world(tmp_path, [_row(0, TOP, 1000)]))
    clock = [now]
    watcher = Watcher(ctx, adapters=[Fake()], now=lambda: clock[0])
    assert watcher.scan() == []                                         # no reading stored
    pc.save_state(ctx.home, _state((70, now)))
    found = watcher.scan()
    assert [w.key[0] for w in found] == ["pace"] and "points over" in found[0].what
    clock[0] += 120
    assert watcher.scan() == []                                         # once per window period


def test_cli_set_show_and_gate_exit_codes(tmp_path):
    rows = [_row(i, TOP, 50_000, out=500) for i in range(12)]           # logged 2026-01-05 17:00
    w = _world(tmp_path, rows)
    base = ["--home", str(w / "home"), "--root", str(w / "root"), "--sys-snapshot", str(w / "sys")]
    now = "2026-01-08T17:00:00Z"

    def run(*argv):
        buf = io.StringIO()
        with redirect_stdout(buf), credential_guard():
            code = main(["pace", *argv, *base, "--now", now])
        return code, buf.getvalue()

    assert run("set", "--window", "weekly", "--used", "90", "--resets", "2026-01-12T17:00:00Z", "--length", "7d")[0] == 0
    stored = json.loads((w / "home" / ".tokenstretch" / "pace.json").read_text())
    assert stored["windows"]["weekly"]["readings"][0]["used"] == 90
    code, text = run("--gate", "--json")
    gate = json.loads(text)
    assert code == 3 and gate["ok"] is False and gate["wait_seconds"] > 0 and set(gate) == {
        "ok", "wait_seconds", "window", "used", "line"}
    code, text = run()
    assert code == 0 and "estimated used now" in text and "not a bill" in text
    code, text = run("--team", "4", "--model", TOP, "--effort", "max", "--json")
    assert code == 0 and "team" in json.loads(text)
    code, text = run("--what-if")
    assert code == 0 and "What if" in text
    assert run("set", "--window", "weekly", "--used", "1", "--resets", "2026-01-12T17:00:00Z")[0] == 0
    stored = json.loads((w / "home" / ".tokenstretch" / "pace.json").read_text())
    assert stored["windows"]["weekly"]["readings"][-1]["used"] == 1     # lower than before: older readings dropped
    assert run("--gate")[0] == 0
    assert run("clear", "--window", "weekly")[0] == 0 and run("--gate")[0] == 0
    assert run("set", "--window", "w", "--used", "150", "--resets", "2026-01-12T17:00:00Z")[0] == 2


def test_length_and_time_parsing():
    assert pc.parse_length("7d") == 7 * DAY and pc.parse_length("5h") == 18000 and pc.parse_length("1d12h") == 129600
    with pytest.raises(ValueError):
        pc.parse_length("soon")
    assert pc.default_length("5h") == 18000 and pc.default_length("weekly") == 7 * DAY
    assert pc.parse_time("2026-01-05T00:00:00Z") == T0 and pc.parse_time("2026-01-05T01:00:00+01:00") == T0


def test_helper_with_its_own_timed_steps_is_cut_by_time_not_by_share():
    now = T0 + 3 * DAY
    main_steps = [_turn(T0 + 60), _turn(now + 600)]
    helper = {"turns": [_turn(T0 + 120), _turn(T0 + 180), _turn(now + 900)]}
    view = pc.slice_view([_sess(*main_steps, helpers=[helper])], T0, now)
    assert view["total"] == pytest.approx(3 * 2000)                  # 1 main step + 2 helper steps before `now`
    only_helper = pc.slice_view([_sess(helpers=[helper])], T0, now)  # a main log with no step in range still counts its helper
    assert only_helper["total"] == pytest.approx(2 * 2000)
    mid_only = pc.slice_view([_sess(*main_steps, helpers=[helper])], T0, now, pc.re.compile("sonnet"))
    assert mid_only["total"] == 0


def _log_line(i: int, at: float, out: int = 100, mid: str | None = None) -> str:
    return json.dumps({"type": "assistant", "timestamp": pc.iso(at), "message": {
        "id": mid or f"msg{i}", "model": TOP, "content": [{"type": "text", "text": "x" * 2000}],
        "usage": {"input_tokens": 1, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 5,
                  "output_tokens": out}}}) + "\n"


def test_step_cache_reads_only_what_a_log_gained_and_never_counts_a_step_twice(tmp_path):
    from tokenstretch.adapters.claude_code import _feed_usage_steps
    from tokenstretch.stepcache import StepCache
    log, store = tmp_path / "s.jsonl", tmp_path / "cache" / "steps.json"
    log.write_text(_log_line(0, T0) + _log_line(1, T0 + 60))
    c = StepCache(store)
    assert [s["output_tokens"] for s in c.steps(log, _feed_usage_steps)] == [100, 100]
    assert c.steps(log, _feed_usage_steps)[0]["history_tokens"] == 16 and c.save()
    offset = json.loads(store.read_text())["files"][str(log)]["offset"]
    with log.open("a") as fh:                                         # a second block of step 1, then a new step, then half a line
        fh.write(_log_line(1, T0 + 61) + _log_line(2, T0 + 120, out=7) + '{"type": "assistant", "mess')
    calls = []
    def feed(lines, seen):
        calls.append(len(lines))
        return _feed_usage_steps(lines, seen)
    c2 = StepCache(store)
    assert [s["output_tokens"] for s in c2.steps(log, feed)] == [100, 100, 7]
    assert calls == [2] and c2.save()                                # only the two new whole lines were parsed
    assert json.loads(store.read_text())["files"][str(log)]["offset"] > offset
    log.write_text(_log_line(9, T0, out=3))                           # replaced by a shorter file: read again from the start
    assert [s["output_tokens"] for s in StepCache(store).steps(log, _feed_usage_steps)] == [3]
    store.write_text("{not json")
    assert [s["output_tokens"] for s in StepCache(store).steps(log, _feed_usage_steps)] == [3]


def test_window_sessions_counts_steps_beyond_the_tail_of_a_long_log(tmp_path):
    from tokenstretch.adapters.claude_code import ClaudeCodeAdapter, _session_turns
    ctx = _ctx(_world(tmp_path, [_row(0, TOP, 1000)]))
    proj = ctx.home / ".claude" / "projects" / "p"
    proj.mkdir(parents=True, exist_ok=True)
    log = proj / "long.jsonl"
    log.write_text("".join(_log_line(i, T0 + i) for i in range(300)))
    sub = proj / "long" / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-1.jsonl").write_text(_log_line(0, T0 + 5, mid="h0") + _log_line(1, T0 + 6, mid="h1"))
    assert len(_session_turns(log, tail_bytes=50_000)) < 300          # the tail read misses the early steps
    ad = ClaudeCodeAdapter()
    mine = [s for s in ad.window_sessions(ctx) if s["path"] == str(log)]
    assert len(mine) == 1 and len(mine[0]["turns"]) == 300 and len(mine[0]["helpers"][0]["turns"]) == 2
    assert (ctx.home / ".tokenstretch" / "cache" / "window-steps.json").is_file()
    assert len([s for s in ad.window_sessions(ctx) if s["path"] == str(log)][0]["turns"]) == 300
