"""Dashboard panels (pace, queue, cost, redo work, start-up, watch), the link file, and the reminder hook."""
from __future__ import annotations

import io
import json
import threading
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from conftest import credential_guard
from test_dashboard_weekly import _ctx, _get, _tree, _world, loopback  # noqa: F401  (fixture re-export)

from tokenstretch import dashboard as dmod
from tokenstretch import dashboard_panels as dp
from tokenstretch import dashlink
from tokenstretch import pace as pc
from tokenstretch import queue as q
from tokenstretch.auto import run_auto, session_start
from tokenstretch.cli import main
from tokenstretch.dashboard import PANELS, DashboardState, render_page

URL = "http://127.0.0.1:8765/abcDEF123_-xyz789abcDEF/"
NOW = time.time()


def _home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def _hook(home: Path, payload: dict, now: float = NOW):
    out: list[str] = []
    assert run_auto(stdin=io.StringIO(json.dumps(payload)), out=out.append, now=lambda: now, home=home) == 0
    return [json.loads(x) for x in out]


# ---- panels in the state ---------------------------------------------------------------------

def test_every_panel_renders_or_says_why_it_is_empty(tmp_path):
    w = _world(tmp_path)
    before = _tree(w / "home"), _tree(w / "root")
    with credential_guard():
        st = DashboardState(_ctx(w)).snapshot()
    assert st["pending"] == [] and set(st["panels"]) == set(PANELS)
    for name, p in st["panels"].items():
        assert p is not None, name
        assert p["explain"] and (p.get("empty") or p.get("rows") or p.get("tools") or p.get("kinds")), name
    assert "No plan window stored" in st["panels"]["pace"]["empty"]
    assert "queue" in st["panels"]["queue"]["empty"]
    assert "No priced steps" in st["panels"]["cost"]["empty"]       # the fixture model has no price ratio
    assert st["panels"]["rework"]["note"] and st["panels"]["startup"]["explain"]
    assert st["panels"]["watch"]["kinds"], "the fixture session trips watch warnings"
    assert str(w / "home") not in json.dumps(st)
    assert (_tree(w / "home"), _tree(w / "root")) == before, "building panels must not write files"


def test_a_failing_panel_hides_only_its_own_card(tmp_path, monkeypatch):
    w = _world(tmp_path)

    def boom(*a, **k):
        raise RuntimeError(f"cannot read {w / 'home'}/x")
    monkeypatch.setattr(dp, "build_cost", boom)
    with credential_guard():
        st = DashboardState(_ctx(w)).snapshot()
    assert st["panels"]["cost"] is None and st["panels"]["pace"] is not None and st["panels"]["rework"] is not None
    assert "panel cost" in st["errors"] and str(w / "home") not in st["errors"]["panel cost"]
    assert st["ready"] is True


def test_panels_wait_their_ttl(tmp_path, monkeypatch):
    w = _world(tmp_path)
    clock = [NOW]
    calls: list[str] = []
    real = dp.build_startup
    monkeypatch.setattr(dp, "build_startup", lambda *a, **k: calls.append("s") or real(*a, **k))
    ds = DashboardState(_ctx(w), now=lambda: clock[0])
    with credential_guard():
        ds.snapshot()
        ds.snapshot()
        assert calls == ["s"]
        clock[0] += dmod.PANEL_TTL["startup"] + 1
        ds.snapshot()
    assert calls == ["s", "s"]


def test_page_has_a_card_and_renderer_for_each_panel():
    page = render_page(None)
    for name in PANELS:
        assert f'id="p-{name}"' in page and f'"{name}"' in page
    assert "renderPanels(s)" in page and "innerHTML" not in page


def test_cost_and_rework_panels_with_priced_steps(tmp_path):
    turns = [{"model": "claude-fable-5-2", "effort": "max", "history_tokens": 90_000, "cache_read": 80_000,
              "cache_write": 10_000, "output_tokens": 400} for _ in range(5)]
    sess = [{"adapter": "claude-code", "path": str(tmp_path / "none.jsonl"), "turns": turns}]
    c = dp.build_cost(None, sess, str)
    assert c["rows"][0]["model"] == "claude-fable-5-2" and c["rows"][0]["steps"] == 5
    assert c["total"] > 0 and c["grid"] and "not a bill" in c["note"] and c["explain"]
    r = dp.build_rework(None, sess, str)          # the log is missing: skipped, so an honest empty line
    assert r["rows"] == [] and "readable logs" in r["empty"] and r["note"]
    assert dp.build_cost(None, [], str)["empty"]


def test_pace_panel_labels_automatic_readings_and_gate(tmp_path):
    w = _world(tmp_path)
    home, now = w / "home", time.time()
    state = {"windows": {}}
    pc.set_reading(state, "auto-5h", 99.0, now + 3600, now - 600, length_s=5 * 3600)
    pc.set_reading(state, "weekly", 10.0, now + 5 * 86400, now - 600, length_s=7 * 86400)
    pc.save_state(home, state)
    with credential_guard():
        p = dp.build_pace(_ctx(w), DashboardState(_ctx(w)).adapters, now)
    rows = {r["window"]: r for r in p["windows"]}
    assert rows["auto-5h"]["source"] == "automatic" and rows["weekly"]["source"] == "typed by hand"
    assert {r["status"] for r in p["windows"]} <= {"ok", "over", "needs-reading"}
    assert p["gate"] is not None and isinstance(p["gate"]["open"], bool) and "not a bill" in p["note"]


def test_queue_panel_counts_tasks_and_scrubs(tmp_path):
    w = _world(tmp_path)
    root, home = w / "root", w / "home"
    q.add_task(root, f"fix the thing in {home}/proj", files=["a.py"], done="tests pass")
    q.add_task(root, "second", hard=True)
    ds = DashboardState(_ctx(w))
    with credential_guard():
        p = dp.build_queue(_ctx(w), ds.adapters, NOW, ds._scrub)
    assert p["counts"] == {"open": 2} and len(p["tasks"]) == 2 and p["gate"] is None
    assert str(home) not in json.dumps(p) and "~/proj" in p["tasks"][0]["task"]
    assert p["tasks"][1]["hard"] is True


def test_watch_panel_groups_by_kind():
    feed = [{"kind": "context", "what": "big", "fix": "clear", "seen_local": "t"},
            {"kind": "context", "what": "bigger", "fix": "clear2", "seen_local": "t"},
            {"kind": "rereads", "what": "reads", "fix": "stop", "seen_local": "t"}]
    from tokenstretch.watch import WatchConfig
    p = dp.build_watch(feed, WatchConfig(), str)
    assert [(k["kind"], k["count"]) for k in p["kinds"]] == [("context", 2), ("rereads", 1)]
    assert dp.build_watch([], WatchConfig(), str)["empty"]


# ---- the link file ---------------------------------------------------------------------------

def test_link_file_roundtrip_stale_and_refusals(tmp_path):
    home = _home(tmp_path)
    assert dashlink.live_url(home) is None
    assert dashlink.write_link(home, "http://192.168.1.5:8765/abcdefgh12345/", 8765) is False
    assert dashlink.write_link(home, "https://127.0.0.1:8765/abcdefgh12345/", 8765) is False
    assert dashlink.write_link(home, URL, 8765, NOW) is True
    assert dashlink.live_url(home, check=lambda port: port == 8765) == URL
    assert dashlink.live_url(home, check=lambda port: False) is None          # nothing answers: stale
    dashlink.link_path(home).write_text(json.dumps({"url": URL, "port": 9999}))
    assert dashlink.live_url(home, check=lambda port: True) is None           # port and url disagree
    dashlink.link_path(home).write_text("not json")
    assert dashlink.live_url(home, check=lambda port: True) is None
    dashlink.write_link(home, URL, 8765)
    dashlink.clear_link(home, "http://127.0.0.1:1111/otherotherother/")       # a different run: kept
    assert dashlink.link_path(home).exists()
    dashlink.clear_link(home, URL)
    assert not dashlink.link_path(home).exists()


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX file modes")
def test_link_file_is_owner_only(tmp_path):
    home = _home(tmp_path)
    dashlink.write_link(home, URL, 8765)
    assert dashlink.link_path(home).stat().st_mode & 0o077 == 0


# ---- the reminder ----------------------------------------------------------------------------

def test_reminder_is_off_until_switched_on(tmp_path):
    home = _home(tmp_path)
    dashlink.write_link(home, URL, 8765)
    assert dashlink.reminder_text(home, {"session_id": "s1"}, NOW, check=lambda p: True) is None
    dashlink.set_remind(home, True)
    text = dashlink.reminder_text(home, {"session_id": "s1"}, NOW, check=lambda p: True)
    assert text and f"View your tokenstretch dashboard at {URL}" in text
    assert "Do not stop the task" in text and "do not ask the user" in text and len(text) < 400
    dashlink.set_remind(home, False)
    assert dashlink.reminder_text(home, {"session_id": "s2"}, NOW, check=lambda p: True) is None


def test_reminder_once_per_session_and_never_without_a_session_id(tmp_path):
    home = _home(tmp_path)
    dashlink.set_remind(home, True)
    ok = lambda p: True  # noqa: E731
    assert dashlink.reminder_text(home, {}, NOW, check=ok) is None
    assert dashlink.reminder_text(home, {"session_id": "../evil"}, NOW, check=ok) is None
    assert dashlink.reminder_text(home, {"session_id": "s1"}, NOW, check=ok)
    assert dashlink.reminder_text(home, {"session_id": "s1"}, NOW + 5, check=ok) is None
    assert dashlink.reminder_text(home, {"session_id": "s2"}, NOW, check=ok)
    assert not any("evil" in str(p) for p in home.rglob("*"))


def test_reminder_when_no_dashboard_runs_names_the_command_not_a_link(tmp_path):
    home = _home(tmp_path)
    dashlink.set_remind(home, True)
    text = dashlink.reminder_text(home, {"session_id": "s1"}, NOW)
    assert text and "Run `tokenstretch dashboard`" in text and "http" not in text


def test_old_markers_are_forgotten(tmp_path):
    home = _home(tmp_path)
    dashlink.set_remind(home, True)
    dashlink.reminder_text(home, {"session_id": "old"}, NOW)
    import os
    marker = home / ".tokenstretch" / "dashremind" / "old.json"
    os.utime(marker, (NOW - 8 * 86400, NOW - 8 * 86400))
    dashlink.reminder_text(home, {"session_id": "new"}, NOW)
    assert not marker.exists()


def test_hook_adds_the_reminder_once_at_session_start_and_again_after_a_compaction(tmp_path, monkeypatch):
    home = _home(tmp_path)
    monkeypatch.setattr(dashlink, "_answers", lambda port, timeout=0.4: True)
    monkeypatch.setattr(dashlink, "live_url", lambda h, check=None: URL)
    dashlink.set_remind(home, True)
    proj = tmp_path / "proj"
    proj.mkdir()
    first = _hook(home, {"hook_event_name": "SessionStart", "source": "startup", "session_id": "s1", "cwd": str(proj)})
    ctx = first[0]["hookSpecificOutput"]["additionalContext"]
    assert first[0]["hookSpecificOutput"]["hookEventName"] == "SessionStart" and URL in ctx
    assert _hook(home, {"hook_event_name": "SessionStart", "source": "resume", "session_id": "s1", "cwd": str(proj)}) == []
    # A compaction summarizes the first reminder away: it is given again, after the carry-on note.
    comp = _hook(home, {"hook_event_name": "SessionStart", "source": "compact", "session_id": "s1", "cwd": str(proj)})
    ctx = comp[0]["hookSpecificOutput"]["additionalContext"]
    assert "carry on" in ctx and ctx.index("carry on") < ctx.index(URL)


def test_hook_keeps_the_handoff_note_and_adds_the_reminder(tmp_path, monkeypatch):
    import os
    home = _home(tmp_path)
    monkeypatch.setattr(dashlink, "live_url", lambda h, check=None: URL)
    dashlink.set_remind(home, True)
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    note = proj / ".claude" / "handoff-auto.md"
    note.write_text("Goal: ship.\n", encoding="utf-8")
    os.utime(note, (NOW - 60, NOW - 60))
    out = _hook(home, {"hook_event_name": "SessionStart", "source": "startup", "session_id": "s1", "cwd": str(proj)})
    ctx = out[0]["hookSpecificOutput"]["additionalContext"]
    assert "handoff-auto.md" in ctx and URL in ctx


def test_hook_is_silent_and_reads_nothing_without_a_session_id(tmp_path, monkeypatch):
    home = _home(tmp_path)
    dashlink.set_remind(home, True)
    seen: list = []
    monkeypatch.setattr(dashlink, "remind_enabled", lambda h: seen.append(h) or True)
    assert _hook(home, {"hook_event_name": "SessionStart", "source": "startup", "cwd": str(tmp_path)}) == []
    assert seen == [] and session_start({"source": "startup"}, NOW, home) is None


def test_hook_with_a_broken_switch_file_stays_quiet(tmp_path):
    home = _home(tmp_path)
    (home / ".tokenstretch").mkdir()
    dashlink.remind_path(home).write_text("{{{")
    assert _hook(home, {"hook_event_name": "SessionStart", "source": "startup", "session_id": "s1"}) == []


# ---- command line and setup ------------------------------------------------------------------

def _cli(w: Path, *args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(["dashboard", *args, "--home", str(w / "home"), "--root", str(w / "root"),
                     "--sys-snapshot", str(w / "sys")])
    return code, buf.getvalue()


def test_cli_remind_switch_and_url(tmp_path, monkeypatch):
    w = _world(tmp_path)
    home = w / "home"
    assert dashlink.remind_enabled(home) is False
    code, out = _cli(w, "--remind", "on")
    assert code == 0 and dashlink.remind_enabled(home) is True
    code, out = _cli(w, "--remind", "off")
    assert code == 0 and dashlink.remind_enabled(home) is False
    code, out = _cli(w, "--url")
    assert code == 1 and "No dashboard is running" in out and "http" not in out
    monkeypatch.setattr(dashlink, "live_url", lambda h, check=None: URL)
    code, out = _cli(w, "--url")
    assert code == 0 and out.strip() == URL


def test_setup_turns_the_reminder_on_and_undo_removes_it(tmp_path):
    from test_setup import _run, _world as setup_world
    w = setup_world(tmp_path)
    home = w / "home"
    assert not dashlink.remind_path(home).exists()
    res, out = _run(w, yes=True, installs=False)
    assert dashlink.remind_enabled(home) is True and "dashboard link reminder" in out
    res2, out2 = _run(w, yes=True, installs=False)
    assert dashlink.remind_enabled(home) is True and "claude-dashboard-reminder" not in json.dumps(
        [i["id"] for i in res2.items])


# ---- the live server writes and removes the link ----------------------------------------------

def test_run_dashboard_leaves_a_link_while_running_and_removes_it(tmp_path, loopback, monkeypatch):  # noqa: F811
    w = _world(tmp_path)
    home = w / "home"
    made: list = []
    real = dmod.make_server
    monkeypatch.setattr(dmod, "make_server", lambda *a, **k: made.append(real(*a, **k)) or made[-1])
    lines: list[str] = []
    t = threading.Thread(target=lambda: dmod.run_dashboard(_ctx(w), port=0, open_browser=False, out=lines.append),
                         daemon=True)
    t.start()
    deadline = time.time() + 10
    while time.time() < deadline and not dashlink.link_path(home).exists():
        time.sleep(0.02)
    srv, token = made[0]
    url = dashlink.live_url(home)
    assert url == f"http://127.0.0.1:{srv.server_address[1]}/{token}/" and url in "\n".join(lines)
    r, body = _get(srv, f"/{token}/api/state")
    assert r.status == 200 and "panels" in json.loads(body)
    srv.shutdown()
    t.join(10)
    assert not dashlink.link_path(home).exists() and dashlink.live_url(home) is None


def test_reminder_is_given_again_after_a_compaction_with_the_link_of_that_moment(tmp_path):
    from tokenstretch import auto
    home = _home(tmp_path)
    dashlink.set_remind(home, True)
    first = dashlink.reminder_text(home, {"session_id": "s1"}, NOW)
    assert first and "each time you finish a task" in first and "http" not in first
    assert dashlink.reminder_text(home, {"session_id": "s1"}, NOW + 5) is None
    dashlink.write_link(home, URL, 8765)
    again = dashlink.reminder_text(home, {"session_id": "s1"}, NOW + 9, check=lambda p: True, again=True)
    assert again and f"View your tokenstretch dashboard at {URL}" in again
    dashlink.clear_link(home, URL)
    reply = auto.session_start({"source": "compact", "session_id": "s1"}, NOW + 10, home=home)
    ctx = reply["hookSpecificOutput"]["additionalContext"]
    assert auto.CARRY_ON in ctx and "tokenstretch dashboard" in ctx
    dashlink.set_remind(home, False)
    reply = auto.session_start({"source": "compact", "session_id": "s1"}, NOW + 11, home=home)
    assert reply["hookSpecificOutput"]["additionalContext"] == auto.CARRY_ON
