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
    assert res.mode == "applied" and len(res.manifests) == 3
    assert "✔ switched default model to sonnet, backed up ~/.claude/settings.json" in out
    assert "/model opus" in out
    s = json.loads((w / "home" / ".claude" / "settings.json").read_text())
    assert s == {"model": "sonnet", "theme": "dark"}
    assert "credit-rules start" in (w / "root" / "CLAUDE.md").read_text()
    assert json.loads((w / "home" / ".caveman" / "cloud.json").read_text())["telemetry"]["enabled"] is False
    # Re-running is a no-op plan.
    items, _ = build_plan(_ctx(w))
    assert items == []
    # CLI undo restores everything from the run.
    buf = io.StringIO()
    with credential_guard(), redirect_stdout(buf):
        code = main(["undo", "--home", str(w / "home"), "--root", str(w / "root"), "--sys-snapshot", str(w / "sys")])
    assert code == 0 and "Restored 3 file(s)" in buf.getvalue()
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
    assert st["sessions"] and st["sessions"][0]["long"] and st["sessions"][0]["model"] == "claude-opus-4"
    buf = io.StringIO()
    with credential_guard(), redirect_stdout(buf):
        main(["status", "--home", str(w / "home"), "--root", str(w / "root"), "--sys-snapshot", str(w / "sys")])
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
