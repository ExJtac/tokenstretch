"""setup / status / report / undo round trip, plus the measure-header and W17 bug fixes."""
from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stdout
from pathlib import Path

from conftest import credential_guard

from tokenstretch.cli import main
from tokenstretch.context import build_context
from tokenstretch.runner import FakeRunner
from tokenstretch.setup import build_plan, collect_status, run_setup, write_report

SETTINGS = {"model": "opus", "theme": "dark"}


def _world(tmp_path: Path) -> Path:
    home, root = tmp_path / "home", tmp_path / "root"
    (home / ".claude" / "projects" / "-proj").mkdir(parents=True)
    (home / ".caveman").mkdir(parents=True)
    root.mkdir()
    (tmp_path / "sys").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps(SETTINGS), encoding="utf-8")
    (home / ".caveman" / "cloud.json").write_text('{"telemetry": {"enabled": true}}', encoding="utf-8")
    # Credential canaries: must never be opened.
    (home / ".claude" / ".credentials.json").write_text("CANARY_CREDENTIAL_DO_NOT_READ_CLAUDE")
    (home / ".caveman" / "credentials").write_text("CANARY_CREDENTIAL_DO_NOT_READ_CAVEMAN")
    (root / "CLAUDE.md").write_text("# Project\n", encoding="utf-8")
    rows = [{"type": "assistant", "message": {"id": f"m{i}", "model": "claude-opus-4",
             "usage": {"input_tokens": 5, "cache_read_input_tokens": 200_000, "cache_creation_input_tokens": 100,
                       "output_tokens": 10}}} for i in range(3)]
    (home / ".claude" / "projects" / "-proj" / "abc12345.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return tmp_path


def _ctx(w: Path):
    return build_context(w / "home", w / "root", w / "sys")


def _snapshot(w: Path) -> dict:
    return {str(p): p.read_bytes() for p in (w / "home").rglob("*")
            if p.is_file() and ".tokenstretch" not in p.parts and p.name not in {".credentials.json", "credentials"}} | \
           {str(p): p.read_bytes() for p in (w / "root").rglob("*") if p.is_file()}


def _run(w: Path, **kw):
    lines: list[str] = []
    with credential_guard():
        res = run_setup(_ctx(w), out=lines.append, **kw)
    return res, "\n".join(lines)


def test_plan_explains_each_item(tmp_path):
    w = _world(tmp_path)
    items, _ = build_plan(_ctx(w))
    ids = {i.id for i in items}
    assert {"claude-default-model", "claude-rules-block", "caveman-telemetry-off"} <= ids
    for i in items:
        assert i.title and i.why and i.changes and i.undo
        assert all(c.get("key") and c.get("path") for c in i.changes)


def test_no_tty_no_yes_is_dry_run(tmp_path):
    w = _world(tmp_path)
    before = _snapshot(w)
    res, out = _run(w, interactive=False)
    assert res.mode == "dry-run"
    assert "Dry run" in out and "Why:" in out and "Undo:" in out
    assert _snapshot(w) == before
    assert not (w / "home" / ".tokenstretch" / "manifests").exists()


def test_explicit_dry_run_changes_nothing(tmp_path):
    w = _world(tmp_path)
    before = _snapshot(w)
    res, _ = _run(w, dry_run=True, yes=True)
    assert res.mode == "dry-run" and _snapshot(w) == before


def test_interactive_decline(tmp_path):
    w = _world(tmp_path)
    before = _snapshot(w)
    res, out = _run(w, interactive=True, input_fn=lambda: "n")
    assert res.mode == "declined" and _snapshot(w) == before
    assert "[y/N]" in out


def test_yes_applies_with_progress_and_undo_restores(tmp_path):
    w = _world(tmp_path)
    before = _snapshot(w)
    res, out = _run(w, yes=True, installs=False)
    assert res.mode == "applied" and len(res.manifests) == 5
    assert "✔ switched default model to sonnet and turned on live warnings, backed up ~/.claude/settings.json" in out
    assert "/model opus" in out
    s = json.loads((w / "home" / ".claude" / "settings.json").read_text())
    assert s["model"] == "sonnet" and s["theme"] == "dark"
    # The live warnings ride in the same step as the model change: one edit per file.
    assert s["statusLine"]["type"] == "command" and s["statusLine"]["command"].endswith(" statusline")
    hook = s["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert hook["command"].endswith(" nudge 2>/dev/null || true") and hook["timeout"] == 5   # can never block a prompt
    assert set(s) == {"model", "theme", "statusLine", "hooks", "env"}
    assert s["env"] == {"CLAUDE_CODE_AUTO_COMPACT_WINDOW": "200000"}
    for event in ("PreCompact", "PostCompact", "SessionStart"):
        h = s["hooks"][event][0]["hooks"][0]
        assert h["command"].endswith(" auto 2>/dev/null || true") and h["timeout"] == 5
    assert "and turned on live warnings" in out
    assert "credit-rules start" in (w / "root" / "CLAUDE.md").read_text()
    assert json.loads((w / "home" / ".caveman" / "cloud.json").read_text())["telemetry"]["enabled"] is False
    # Re-running is a no-op plan.
    items, _ = build_plan(_ctx(w))
    assert items == []
    # CLI undo restores everything from the run.
    buf = io.StringIO()
    with credential_guard(), redirect_stdout(buf):
        code = main(["undo", "--home", str(w / "home"), "--root", str(w / "root"), "--sys-snapshot", str(w / "sys")])
    assert code == 0 and "Restored 3 file(s), deleted 2 created file(s)" in buf.getvalue()
    assert _snapshot(w) == before


def test_interactive_yes_then_installs_use_fake_runner(tmp_path):
    w = _world(tmp_path)
    answers = iter(["y", "y"])
    runner = FakeRunner()
    res, out = _run(w, interactive=True, input_fn=lambda: next(answers), runner=runner)
    assert res.mode == "applied"
    assert res.installs is not None
    assert "uses the network" in out
    argvs = " ".join(" ".join(c.argv) for c in runner.calls)
    assert runner.calls and "rtk" in argvs.lower()
    assert "tokeneconomizer" not in argvs.lower() and "ccusage" not in argvs.lower()
    assert all(c.skipped for c in runner.calls)  # FakeRunner never executes


def test_undo_after_installs_restores_original(tmp_path):
    w = _world(tmp_path)
    before = _snapshot(w)
    res, _ = _run(w, yes=True, installs=True, runner=FakeRunner())
    assert res.installs and res.installs["ok"], res.installs
    with credential_guard(), redirect_stdout(io.StringIO()):
        code = main(["undo", "--home", str(w / "home"), "--root", str(w / "root"), "--sys-snapshot", str(w / "sys")])
    assert code == 0
    assert _snapshot(w) == before


def test_created_file_is_deleted_by_undo(tmp_path):
    w = _world(tmp_path)
    (w / "root" / "CLAUDE.md").unlink()
    (w / "root" / ".claude").mkdir()
    res, out = _run(w, yes=True, installs=False)
    assert (w / "root" / "CLAUDE.md").is_file()
    assert "created" in out
    with credential_guard(), redirect_stdout(io.StringIO()):
        main(["undo", "--home", str(w / "home"), "--root", str(w / "root"), "--sys-snapshot", str(w / "sys")])
    assert not (w / "root" / "CLAUDE.md").exists()


def test_cli_setup_without_tty_is_dry_run(tmp_path, monkeypatch):
    w = _world(tmp_path)
    before = _snapshot(w)
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    buf = io.StringIO()
    with credential_guard(), redirect_stdout(buf):
        code = main(["setup", "--home", str(w / "home"), "--root", str(w / "root"), "--sys-snapshot", str(w / "sys")])
    assert code == 0 and "Dry run" in buf.getvalue()
    assert _snapshot(w) == before


def test_status_shows_applied_models_and_long_sessions(tmp_path):
    w = _world(tmp_path)
    _run(w, yes=True, installs=False)
    with credential_guard():
        st = collect_status(_ctx(w))
    assert any(f["state"] == "applied" for m in st["applied"] for f in m["files"])
    assert any(m["model"] == "sonnet" for row in st["models"] for m in row["models"])
    # Setup turned automatic summaries on at 200K, so a 200K session is not flagged.
    assert st["sessions"] and not st["sessions"][0]["long"] and st["sessions"][0]["model"] == "claude-opus-4"
    args = ["status", "--home", str(w / "home"), "--root", str(w / "root"), "--sys-snapshot", str(w / "sys")]
    buf = io.StringIO()
    with credential_guard(), redirect_stdout(buf):
        main(args)
    assert "/clear" not in buf.getvalue() and "/compact once" not in buf.getvalue()
    settings = w / "home" / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    data["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "120000"   # the session is well past this
    settings.write_text(json.dumps(data), encoding="utf-8")
    buf = io.StringIO()
    with credential_guard(), redirect_stdout(buf):
        main(args)
    assert "over 120K: /compact once" in buf.getvalue() and "/clear at" not in buf.getvalue()
    del data["env"]                                             # no automatic summaries: the manual tip
    settings.write_text(json.dumps(data), encoding="utf-8")
    buf = io.StringIO()
    with credential_guard(), redirect_stdout(buf):
        main(args)
    assert "/clear at the next task break" in buf.getvalue()


def test_report_is_local_html(tmp_path):
    w = _world(tmp_path)
    _run(w, yes=True, installs=False)
    with credential_guard():
        path = write_report(_ctx(w), tmp_path / "r.html")
    text = path.read_text()
    assert text.startswith("<!doctype html>")
    assert "tokenstretch undo" in text and "What doctor found" in text and "Newest sessions" in text
    assert not re.search(r"""(src|href)\s*=\s*["']?\s*(https?:)?//""", text, re.I)
    assert not re.search(r"url\(\s*['\"]?(https?:)?//", text, re.I)
    assert "<script" not in text.lower() and "<link" not in text.lower()
    assert "CANARY" not in text


def test_cli_report_prints_path(tmp_path):
    w = _world(tmp_path)
    buf = io.StringIO()
    with credential_guard(), redirect_stdout(buf):
        main(["report", "--home", str(w / "home"), "--root", str(w / "root"), "--sys-snapshot", str(w / "sys")])
    m = re.search(r"(/\S+\.html)", buf.getvalue())
    assert m and Path(m.group(1)).is_file()


# ---- bug fixes -------------------------------------------------------------------

def test_measure_header_says_subscription():
    from tokenstretch.bench import LiveBenchReport, format_live_bench
    sub = format_live_bench(LiveBenchReport(ok=True, sandbox_root="", sandbox_home="", mode="subscription",
                                            after_model="sonnet"))
    assert "API-key" not in sub and "subscription mode" in sub and "--model sonnet" in sub
    api = format_live_bench(LiveBenchReport(ok=True, sandbox_root="", sandbox_home=""))
    assert "API-key mode" in api


def test_w17_grouped_into_one_finding(tmp_path):
    from tokenstretch.adapters.host import HostAdapter
    root = tmp_path / "root"
    for i in range(4):
        d = root / f"pkg{i}"
        d.mkdir(parents=True)
        (d / "CLAUDE.md").write_text(f"rules {i}\n")
        (d / "AGENTS.md").write_text(f"rules {i}\n")
    (tmp_path / "home").mkdir()
    (tmp_path / "sys").mkdir()
    ctx = build_context(tmp_path / "home", root, tmp_path / "sys")
    w17 = [f for f in HostAdapter()._w17(ctx) if f.id == "W17"]
    assert len(w17) == 1
    assert "4 sets" in w17[0].message and "8 files" in w17[0].message
    assert "and 3 more" in w17[0].evidence["file"]


# ---- /handoff shortcut ------------------------------------------------------------------------

def test_setup_adds_a_manual_only_handoff_skill_and_undo_removes_it(tmp_path):
    from tokenstretch.files import frontmatter
    w = _world(tmp_path)
    skill = w / "home" / ".claude" / "skills" / "handoff" / "SKILL.md"
    items, _ = build_plan(_ctx(w))
    item = next(i for i in items if i.id == "claude-handoff-skill")
    assert [c["kind"] for c in item.changes] == ["create"] and item.changes[0]["path"] == str(skill)
    _, out = _run(w, yes=True, installs=False)
    assert "✔ added the /handoff shortcut" in out
    text = skill.read_text(encoding="utf-8")
    fm = frontmatter(text)
    assert fm.get("name") == "handoff" and fm.get("disable-model-invocation") == "true"   # nothing added per turn
    assert "$ARGUMENTS" in text and "resume" in text and ".claude/handoff.md" in text
    assert len(text) < 1600
    with credential_guard():
        main(["undo", "--home", str(w / "home"), "--root", str(w / "root"), "--sys-snapshot", str(w / "sys")])
    assert not skill.exists() and not (w / "home" / ".claude" / "skills").exists()


def test_setup_never_replaces_an_existing_handoff_shortcut(tmp_path):
    for rel in (("home", ".claude", "commands", "handoff.md"), ("root", ".claude", "skills", "handoff", "SKILL.md")):
        w = _world(tmp_path / rel[0])
        mine = w.joinpath(*rel)
        mine.parent.mkdir(parents=True)
        mine.write_text("my own handoff\n", encoding="utf-8")
        items, _ = build_plan(_ctx(w))
        assert "claude-handoff-skill" not in {i.id for i in items}
        _run(w, yes=True, installs=False)
        assert mine.read_text(encoding="utf-8") == "my own handoff\n"


def test_watch_and_statusline_point_to_handoff_once_it_exists(tmp_path):
    from tokenstretch.statusline import build_line
    from tokenstretch.watch import Watcher
    w = _world(tmp_path)                       # 200K history on opus
    with credential_guard():
        before = [x for x in Watcher(_ctx(w)).scan() if x.key[-1] == "context"][0]
        assert "/handoff" not in before.fix and build_line(_ctx(w)).endswith("· /clear at the next task break")
    _run(w, yes=True, installs=False)
    settings = w / "home" / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    data.pop("env")                            # setup without automatic summaries: the manual tip stays
    settings.write_text(json.dumps(data), encoding="utf-8")
    with credential_guard():
        after = [x for x in Watcher(_ctx(w)).scan() if x.key[-1] == "context"][0]
        line = build_line(_ctx(w))
    assert "type /handoff (writes a short note) and /clear" in after.fix and "/handoff resume" in after.fix
    assert line.endswith("· /handoff then /clear at the next task break")


def test_no_clear_advice_while_automatic_summaries_are_on(tmp_path):
    """Setup makes the tool summarize at a set size. Below that size (plus a margin) a tip to
    clear by hand would contradict it, so watch, the status line and the prompt hook stay quiet."""
    from tokenstretch.statusline import build_line
    from tokenstretch.watch import Watcher
    w = _world(tmp_path)                       # 200K history on opus
    _run(w, yes=True, installs=False)
    settings = w / "home" / ".claude" / "settings.json"
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "200000"
    with credential_guard():
        assert not [x for x in Watcher(_ctx(w)).scan() if x.key[-1] == "context"]
        line = build_line(_ctx(w))
    assert "/clear" not in line and "/handoff" not in line and "/compact" not in line

    data["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "120000"   # history is well past the window
    settings.write_text(json.dumps(data), encoding="utf-8")
    with credential_guard():
        late = [x for x in Watcher(_ctx(w)).scan() if x.key[-1] == "context"][0]
        line = build_line(_ctx(w))
    assert late.fix.startswith("type /compact once") and "/clear" not in late.fix and "/handoff" not in late.fix
    assert line.endswith("· /compact once (automatic summary did not run)")

    data["env"]["DISABLE_AUTO_COMPACT"] = "1"                   # switched off by the user: manual tip again
    settings.write_text(json.dumps(data), encoding="utf-8")
    with credential_guard():
        manual = [x for x in Watcher(_ctx(w)).scan() if x.key[-1] == "context"][0]
    assert "/clear" in manual.fix


# ---- live warnings inside the tool, and /brief ------------------------------------------------

def test_setup_keeps_your_status_line_and_wraps_it(tmp_path):
    w = _world(tmp_path)
    path = w / "home" / ".claude" / "settings.json"
    mine = {"model": "sonnet", "statusLine": {"type": "command", "command": "bash ~/my line.sh", "padding": 0},
            "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}
    path.write_text(json.dumps(mine), encoding="utf-8")
    items, _ = build_plan(_ctx(w))
    item = next(i for i in items if i.id == "claude-live-warnings")
    assert "your status line kept" in item.changes[0]["reason"]
    _run(w, yes=True, installs=False)
    s = json.loads(path.read_text())
    assert s["statusLine"]["command"].endswith(" statusline --wrap 'bash ~/my line.sh'")
    assert s["statusLine"]["padding"] == 0
    cmds = [h["command"] for g in s["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
    assert cmds[0] == "echo hi" and " nudge " in cmds[1] and len(cmds) == 2
    # A second run plans nothing for it: no double wrap, no second hook.
    assert "claude-live-warnings" not in {i.id for i in build_plan(_ctx(w))[0]}
    with credential_guard():
        main(["undo", "--home", str(w / "home"), "--root", str(w / "root"), "--sys-snapshot", str(w / "sys")])
    assert json.loads(path.read_text()) == mine


def test_setup_leaves_unreadable_or_unknown_settings_alone(tmp_path):
    w = _world(tmp_path)
    path = w / "home" / ".claude" / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    assert "claude-live-warnings" not in {i.id for i in build_plan(_ctx(w))[0]}
    path.write_text(json.dumps({"model": "sonnet", "statusLine": "custom"}), encoding="utf-8")
    _run(w, yes=True, installs=False)
    assert json.loads(path.read_text())["statusLine"] == "custom"   # a shape we do not know stays as it is


def test_setup_adds_a_manual_only_brief_skill_and_never_replaces_yours(tmp_path):
    from tokenstretch.files import frontmatter
    w = _world(tmp_path)
    skill = w / "home" / ".claude" / "skills" / "brief" / "SKILL.md"
    _, out = _run(w, yes=True, installs=False)
    assert "✔ added the /brief shortcut" in out
    text = skill.read_text(encoding="utf-8")
    fm = frontmatter(text)
    assert fm.get("name") == "brief" and fm.get("disable-model-invocation") == "true"
    assert "$ARGUMENTS" in text and "Done when" in text and "Missing" in text and len(text) < 1800
    w2 = _world(tmp_path / "second")
    mine = w2 / "root" / ".claude" / "commands" / "brief.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("my own brief\n", encoding="utf-8")
    assert "claude-brief-skill" not in {i.id for i in build_plan(_ctx(w2))[0]}


# ---- autopilot: shorter sessions with nothing to type -------------------------------------------

def test_setup_never_overrides_your_own_compaction_choices(tmp_path):
    for mine in ({"env": {"DISABLE_AUTO_COMPACT": "1"}}, {"env": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "60"}},
                 {"autoCompactWindow": 400000}, {"autoCompactEnabled": False}, {"env": "odd"}):
        w = _world(tmp_path / str(abs(hash(json.dumps(mine)))))
        path = w / "home" / ".claude" / "settings.json"
        path.write_text(json.dumps({"model": "sonnet", **mine}), encoding="utf-8")
        _run(w, yes=True, installs=False)
        s = json.loads(path.read_text())
        assert "CLAUDE_CODE_AUTO_COMPACT_WINDOW" not in json.dumps(s)
        assert {k: s[k] for k in mine} == mine
        assert "SessionStart" in s["hooks"]          # the hooks still go in


def test_auto_hook_shapes_the_summary_saves_it_and_points_a_new_chat_at_it(tmp_path):
    import io as _io
    from tokenstretch.auto import run_auto
    proj = tmp_path / "proj"
    proj.mkdir()
    def call(payload, now=1_000_000.0):
        printed = []
        assert run_auto(stdin=_io.StringIO(json.dumps(payload)), out=printed.append, now=lambda: now) == 0
        return printed
    pre = call({"hook_event_name": "PreCompact", "trigger": "auto", "cwd": str(proj)})
    assert "Next step" in pre[0] and "Leave out file contents" in pre[0]
    assert call({"hook_event_name": "SessionStart", "source": "startup", "cwd": str(proj)}) == []   # no note yet
    assert call({"hook_event_name": "PostCompact", "cwd": str(proj), "compact_summary": "Goal: ship it."}) == []
    note = proj / ".claude" / "handoff-auto.md"
    assert note.read_text().endswith("Goal: ship it.\n") and "safe to delete" in note.read_text()
    import os
    os.utime(note, (1_000_000.0, 1_000_000.0))
    start = json.loads(call({"hook_event_name": "SessionStart", "source": "startup", "cwd": str(proj)}, now=1_000_600.0)[0])
    text = start["hookSpecificOutput"]["additionalContext"]
    assert ".claude/handoff-auto.md" in text and "10 min ago" in text and "ignore the note" in text
    # A restart needs no "go": any opener resumes, but what waits on the user still waits.
    assert "what's happening?" in text and "in the same turn" in text and "Do not ask the user to confirm" in text
    assert "waiting on the user still waits" in text and len(text) < 900
    assert call({"hook_event_name": "SessionStart", "source": "resume", "cwd": str(proj)}, now=1_000_600.0) == []
    # After a compaction the work restarts by itself: carry on with the next step, no go-ahead asked.
    after = json.loads(call({"hook_event_name": "SessionStart", "source": "compact", "cwd": str(proj)},
                            now=1_000_600.0)[0])["hookSpecificOutput"]
    assert after["hookEventName"] == "SessionStart"
    assert "carry on with the summary's next step now, in this turn" in after["additionalContext"]
    assert "do not ask them to confirm or to say go" in after["additionalContext"]
    assert "waiting on the user still waits" in after["additionalContext"]
    assert call({"hook_event_name": "SessionStart", "source": "startup", "cwd": str(proj)}, now=1_000_000.0 + 80 * 3600) == []


def test_auto_hook_never_fails_or_writes_where_it_should_not(tmp_path):
    import io as _io
    from tokenstretch.auto import run_auto
    printed = []
    for raw in ("", "not json", "[1]", json.dumps({"hook_event_name": "PostCompact", "cwd": "relative", "compact_summary": "x"}),
                json.dumps({"hook_event_name": "PostCompact", "cwd": str(tmp_path / "missing"), "compact_summary": "x"}),
                json.dumps({"hook_event_name": "PostCompact", "cwd": str(tmp_path), "compact_summary": ""}),
                json.dumps({"hook_event_name": "Other", "cwd": str(tmp_path)})):
        assert run_auto(stdin=_io.StringIO(raw), out=printed.append) == 0
    assert printed == [] and not (tmp_path / ".claude").exists()
    target = tmp_path / "secret.txt"
    target.write_text("keep", encoding="utf-8")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "handoff-auto.md").symlink_to(target)
    run_auto(stdin=_io.StringIO(json.dumps({"hook_event_name": "PostCompact", "cwd": str(tmp_path), "compact_summary": "x"})))
    assert target.read_text() == "keep"
