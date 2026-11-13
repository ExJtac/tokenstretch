"""`start`: changes nothing without a yes, applies through setup (undo restores), is safe to run twice,
speaks plain words, and reads the plan's week back. Every test uses a temporary home."""
from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stderr, redirect_stdout

import pytest
from conftest import credential_guard
from test_pace import DAY, RESETS, T0, _sess, _turn
from test_setup import _ctx, _snapshot, _world

from tokenstretch import pace as pc
from tokenstretch import paceauto
from tokenstretch import start as st
from tokenstretch.cli import main

BANNED = re.compile(r"units|pace ratio|re-read|reread", re.I)


def _args(w, *extra) -> list[str]:
    return ["start", "--home", str(w / "home"), "--root", str(w / "root"), "--sys-snapshot", str(w / "sys"), *extra]


def _main(w, *extra):
    out, err = io.StringIO(), io.StringIO()
    with credential_guard(), redirect_stdout(out), redirect_stderr(err):
        code = main(_args(w, *extra))
    return code, out.getvalue(), err.getvalue()


def _run(w, **kw):
    lines: list[str] = []
    with credential_guard():
        code = st.run_start(_ctx(w), out=lines.append, **kw)
    return code, "\n".join(lines)


def _everything(w) -> dict:
    """Every file under home and root, the tokenstretch folder included."""
    return {str(p): p.read_bytes() for base in (w / "home", w / "root") for p in base.rglob("*")
            if p.is_file() and p.name not in {".credentials.json", "credentials"}}


def test_no_flag_changes_nothing_and_prints_the_apply_command(tmp_path):
    w = _world(tmp_path)
    before = _everything(w)
    code, out, err = _main(w)            # pytest's stdin is not a terminal
    assert code == 0 and err == ""
    assert _everything(w) == before
    assert not (w / "home" / ".tokenstretch").exists()
    assert "Nothing was changed" in out and "tokenstretch start --yes" in out
    assert "Switch on automatic plan readings" in out and "Apply these changes?" not in out
    assert "Python" in out and "Claude Code" in out


def test_yes_applies_and_the_undo_path_restores(tmp_path):
    w = _world(tmp_path)
    before = _snapshot(w)
    code, out, _ = _main(w, "--yes")
    assert code == 0
    s = json.loads((w / "home" / ".claude" / "settings.json").read_text())
    assert s["model"] == "sonnet" and "statusLine" in s and "hooks" in s
    assert paceauto.is_on(pc.load_state(w / "home"))
    assert "tokenstretch undo" in out and "pace auto --off" in out
    undo = io.StringIO()
    with credential_guard(), redirect_stdout(undo):
        assert main(["undo", "--home", str(w / "home"), "--root", str(w / "root"),
                     "--sys-snapshot", str(w / "sys")]) == 0
    assert _snapshot(w) == before
    # the automatic readings switch is not a file edit, so it has its own off switch
    with redirect_stdout(io.StringIO()):
        assert main(["pace", "auto", "--off", "--home", str(w / "home")]) == 0
    assert not paceauto.is_on(pc.load_state(w / "home"))


def test_second_run_is_a_no_op(tmp_path):
    w = _world(tmp_path)
    assert _main(w, "--yes")[0] == 0
    after_first = _everything(w)
    for extra in ((), ("--yes",)):
        code, out, _ = _main(w, *extra)
        assert code == 0 and "Already set up. Nothing was changed." in out
        assert _everything(w) == after_first
    code, out = _run(w, interactive=True, input_fn=lambda: pytest.fail("asked a question with nothing to apply"))
    assert code == 0 and "Already set up" in out


def test_json_shape_and_it_never_asks(tmp_path):
    w = _world(tmp_path)
    before = _everything(w)
    code, out, _ = _main(w, "--json")
    data = json.loads(out)
    assert code == 0 and _everything(w) == before
    assert set(data) == {"version", "checks", "planned_changes", "applied", "mode", "apply_command", "undo", "pace",
                         "error"}
    assert set(data["checks"]) == {"python", "tools", "setup_steps_pending", "pace_auto"}
    assert set(data["checks"]["python"]) == {"version", "ok", "needs"} and data["checks"]["python"]["ok"] is True
    assert data["checks"]["tools"] == ["Claude Code"] and data["checks"]["pace_auto"] is False
    ids = [c["id"] for c in data["planned_changes"]]
    assert "claude-default-model" in ids and ids[-1] == "pace-auto"
    assert all(set(c) == {"id", "title", "files"} for c in data["planned_changes"])
    assert data["applied"] is False and data["mode"] == "dry-run" and data["pace"] is None
    assert data["apply_command"].startswith("tokenstretch start --yes")
    code, out, _ = _main(w, "--json", "--yes")
    data = json.loads(out)
    assert code == 0 and data["applied"] is True and data["mode"] == "applied"
    assert set(data["pace"]) >= {"has_reading", "window", "used_percent", "week_passed_percent", "status", "lines"}
    assert data["pace"]["status"] == "no-reading" and data["undo"][0].startswith("tokenstretch undo")
    code, out, _ = _main(w, "--json")
    assert json.loads(out)["mode"] == "already-set-up" and json.loads(out)["planned_changes"] == []


def test_plain_output_has_no_banned_words_in_any_state(tmp_path):
    w = _world(tmp_path)
    texts = [_main(w)[1], _main(w, "--yes")[1], _main(w)[1]]
    state = {"windows": {}}
    pc.set_reading(state, "auto-week", 50, RESETS, T0 + 3.5 * DAY, length_s=7 * DAY)
    pc.save_state(w / "home", state)
    texts.append(_main(w, "--now", pc.iso(T0 + 3.6 * DAY))[1])
    for text in texts:
        assert text.strip() and not BANNED.search(text), BANNED.search(text)


def test_interactive_question_decline_then_yes(tmp_path):
    w = _world(tmp_path)
    before = _everything(w)
    asked: list[str] = []
    code, out = _run(w, interactive=True, input_fn=lambda: asked.append("q") or "n")
    assert code == 0 and asked == ["q"] and "[y/N]" in out and "No changes made" in out
    assert _everything(w) == before
    code, out = _run(w, interactive=True, input_fn=lambda: "yes")
    assert code == 0 and "Done:" in out and "To undo:" in out
    assert paceauto.is_on(pc.load_state(w / "home"))


def test_detail_appends_the_normal_pace_output(tmp_path):
    w = _world(tmp_path)
    code, out, _ = _main(w, "--yes", "--detail")
    assert code == 0 and out.index("No plan reading yet") < out.index("No window readings stored")


def test_python_too_old_stops_without_changes(tmp_path, monkeypatch):
    w = _world(tmp_path)
    before = _everything(w)
    monkeypatch.setattr(st, "python_check", lambda: {"version": "3.9.0", "ok": False})
    code, out = _run(w, yes=True)
    assert code == 1 and "3.11 or newer" in out and _everything(w) == before


def test_a_failed_step_exits_1_and_leaves_readings_off(tmp_path, monkeypatch):
    w = _world(tmp_path)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(st, "apply_plan", boom)
    code, out, err = _main(w, "--yes")
    assert code == 1 and "disk full" in err
    assert not paceauto.is_on(pc.load_state(w / "home"))


def test_no_supported_tool_changes_nothing(tmp_path):
    w = tmp_path
    (w / "home").mkdir()
    (w / "root").mkdir()
    (w / "sys").mkdir()
    code, out, _ = _main(w, "--yes")
    assert code == 0 and "No supported coding tool" in out
    assert not (w / "home" / ".tokenstretch").exists()


# ---- how the week is going ---------------------------------------------------------------------------

NOW = T0 + 3.5 * DAY


def _summary(w, monkeypatch, used, sessions, now=NOW, name="auto-week"):
    state = {"windows": {}}
    if used is not None:
        pc.set_reading(state, name, used, RESETS, NOW, length_s=7 * DAY)
    pc.save_state(w / "home", state)
    monkeypatch.setattr(pc, "window_sessions", lambda ctx, s, limit=500: sessions)
    return st.pace_summary(_ctx(w), now)


@pytest.mark.parametrize("used,status,words", [(50, "on-track", "On track"), (80, "ahead", "Ahead of pace"),
                                               (20, "behind", "Behind pace")])
def test_week_status_in_plain_words(tmp_path, monkeypatch, used, status, words):
    w = _world(tmp_path)
    sessions = [_sess(*[_turn(T0 + i * 60) for i in range(100)])]
    r = _summary(w, monkeypatch, used, sessions)
    assert r["status"] == status and r["has_reading"] and r["estimated"]
    assert r["used_percent"] == used and r["week_passed_percent"] == 50
    assert len(r["lines"]) == 3 and words in r["lines"][1] and "this week's plan is used" in r["lines"][0]
    assert not BANNED.search("\n".join(r["lines"]))


def test_week_without_logs_is_unknown_not_guessed(tmp_path, monkeypatch):
    r = _summary(_world(tmp_path), monkeypatch, 40, [])
    assert r["status"] == "unknown" and r["used_percent"] == 40 and r["estimated"] is False
    assert "Not enough usage" in r["lines"][1] and len(r["lines"]) == 3


def test_no_reading_says_where_one_comes_from(tmp_path, monkeypatch):
    r = _summary(_world(tmp_path), monkeypatch, None, [])
    assert r["status"] == "no-reading" and not r["has_reading"] and len(r["lines"]) == 3
    assert "terminal" in r["lines"][0] and "pace set" in r["lines"][1]


def test_a_finished_week_is_stale(tmp_path, monkeypatch):
    r = _summary(_world(tmp_path), monkeypatch, 50, [], now=RESETS + 3600)
    assert r["status"] == "stale" and r["used_percent"] is None


def test_five_hour_window_alone_is_not_the_week(tmp_path, monkeypatch):
    w = _world(tmp_path)
    state = {"windows": {}}
    pc.set_reading(state, "auto-5h", 30, NOW + 3600, NOW, length_s=5 * 3600)
    pc.save_state(w / "home", state)
    assert st.pace_summary(_ctx(w), NOW)["status"] == "no-reading"
