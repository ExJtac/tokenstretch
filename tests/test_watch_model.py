"""watch (once-per-condition warnings, read-only) and the model picker (heuristic + backed-up default)."""
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
from tokenstretch.models import suggest_tier
from tokenstretch.watch import Watcher, WatchConfig, run_watch

T0 = datetime(2026, 1, 5, 17, 0, 0, tzinfo=timezone.utc)


def _row(i: int, model: str, history: int, *, read: str | None = None, out: int = 120, step_s: int = 40) -> dict:
    ts = (T0 + timedelta(seconds=step_s * i)).isoformat().replace("+00:00", "Z")
    content = [{"type": "text", "text": "ok"}]
    if read:
        content.append({"type": "tool_use", "name": "Read", "input": {"file_path": read}})
    return {"type": "assistant", "timestamp": ts, "message": {
        "id": f"m{i}", "model": model, "content": content,
        "usage": {"input_tokens": 10, "cache_read_input_tokens": history, "cache_creation_input_tokens": 0,
                  "output_tokens": out}}}


def _world(tmp_path: Path, rows: list[dict], settings: dict | None = None) -> Path:
    home, root = tmp_path / "home", tmp_path / "root"
    proj = home / ".claude" / "projects" / "-proj"
    proj.mkdir(parents=True)
    root.mkdir()
    (tmp_path / "sys").mkdir()
    (home / ".claude" / "settings.json").write_text(json.dumps(settings or {"model": "opus", "theme": "dark"}),
                                                     encoding="utf-8")
    (home / ".claude" / ".credentials.json").write_text("CANARY_CREDENTIAL_DO_NOT_READ_CLAUDE")
    (proj / "abcd1234-0000.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return tmp_path


def _wasteful_rows(read_path: str) -> list[dict]:
    return [_row(i, "claude-opus-4-1", 100_000 + 20_000 * i, read=read_path) for i in range(6)]


def _ctx(w: Path):
    return build_context(w / "home", w / "root", w / "sys")


def _watch_once(w: Path) -> str:
    lines: list[str] = []
    with credential_guard():
        run_watch(_ctx(w), once=True, out=lines.append)
    out = "\n".join(lines)
    for c in CANARIES:
        assert c not in out
    return out


def test_watch_once_reports_each_condition_once(tmp_path):
    secret_body = "SECRET_FILE_BODY_NEVER_PRINTED"
    rows = _wasteful_rows(str(tmp_path / "home" / "proj" / "server.py"))
    rows[0]["message"]["content"].append({"type": "text", "text": secret_body})
    w = _world(tmp_path, rows)
    out = _watch_once(w)
    assert out.count("⚠") == 4
    assert "history is ~200K tokens (over 150K)" in out and "/clear" in out and "/compact" in out
    assert "the top model (claude-opus-4-1)" in out and "/model sonnet" in out
    assert "~/proj/server.py ×6" in out
    assert "history grew ~100K tokens" in out
    assert out.count("Why it costs credits:") == 4 and out.count("Fix:") == 4
    assert secret_body not in out


def test_watch_no_duplicates_across_passes(tmp_path):
    w = _world(tmp_path, _wasteful_rows("/x/a.py"))
    with credential_guard():
        watcher = Watcher(_ctx(w))
        first, second = watcher.scan(), watcher.scan()
    assert len(first) == 4 and second == []
    assert len({x.key for x in first}) == 4


def test_watch_loop_dedupes_and_ctrl_c_exits_cleanly(tmp_path):
    w = _world(tmp_path, _wasteful_rows("/x/a.py"))
    lines: list[str] = []
    calls = {"n": 0}

    def sleep(_):
        calls["n"] += 1
        if calls["n"] >= 3:
            raise KeyboardInterrupt

    with credential_guard():
        code = run_watch(_ctx(w), interval=0.01, out=lines.append, sleep=sleep)
    out = "\n".join(lines)
    assert code == 0 and out.count("⚠") == 4 and "Stopped watching. 4 warning(s) shown." in out


def test_watch_quiet_session_and_thresholds(tmp_path):
    rows = [_row(i, "claude-sonnet-4-5", 20_000 + 500 * i, read=f"/x/f{i}.py", out=900) for i in range(5)]
    w = _world(tmp_path, rows)
    out = _watch_once(w)
    assert "⚠" not in out and "No waste spotted" in out
    # A lower --max-context makes (a) fire, and only (a).
    with credential_guard():
        warns = Watcher(_ctx(w), WatchConfig(max_context=10_000)).scan()
    assert [x.key[-1] for x in warns] == ["context"]


def test_watch_skips_stale_sessions_and_is_read_only(tmp_path):
    w = _world(tmp_path, _wasteful_rows("/x/a.py"))
    before = {p: p.read_bytes() for p in (w / "home").rglob("*") if p.is_file() and p.name != ".credentials.json"}
    with credential_guard():
        stale = Watcher(_ctx(w), now=lambda: time.time() + 3 * 86400).scan()
    assert stale == []
    _watch_once(w)
    after = {p: p.read_bytes() for p in (w / "home").rglob("*") if p.is_file() and p.name != ".credentials.json"}
    assert before == after


def test_watch_notify_uses_injected_local_notifier(tmp_path):
    w = _world(tmp_path, _wasteful_rows("/x/a.py"))
    sent = []
    with credential_guard():
        Watcher(_ctx(w), notifier=lambda t, m: sent.append((t, m))).scan()
    assert len(sent) == 4 and all("Fix:" in m for _, m in sent)


def test_watch_codex_rollout(tmp_path):
    w = _world(tmp_path, [_row(0, "claude-sonnet-4-5", 1000, out=900)])
    sess = w / "home" / ".codex" / "sessions" / "2026" / "01" / "05"
    sess.mkdir(parents=True)
    (w / "home" / ".codex" / "auth.json").write_text("CANARY_CREDENTIAL_DO_NOT_READ_CODEX")
    lines = [{"type": "session_meta", "payload": {"id": "sess-1"}},
             {"type": "turn_context", "payload": {"model": "gpt-5-codex"}}]
    for i, inp in enumerate((90_000, 170_000)):
        lines.append({"type": "event_msg", "timestamp": f"2026-01-05T17:0{i}:00Z", "payload": {
            "type": "token_count", "info": {
                "last_token_usage": {"input_tokens": inp, "cached_input_tokens": 0, "output_tokens": 50},
                "total_token_usage": {"input_tokens": inp * (i + 1), "output_tokens": 50 * (i + 1)}}}})
    (sess / "rollout-2026-01-05T17-00-00-sess-1.jsonl").write_text(
        "\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    out = _watch_once(w)
    assert "Codex" in out.split("\n")[1]
    assert "[codex" in out and "history is ~170K" in out and "/new" in out


def test_model_suggest_heuristics():
    assert suggest_tier("Debug the race condition in the websocket reconnect logic")["tier"] == "top"
    assert suggest_tier("Design the architecture for a new billing service")["tier"] == "top"
    assert suggest_tier("Look into a security vulnerability in the auth flow")["tier"] == "top"
    assert suggest_tier("rename getUser to fetchUser")["tier"] == "light"
    assert suggest_tier("fix typos in the README")["tier"] == "light"
    assert suggest_tier("format the code with black")["tier"] == "light"
    assert suggest_tier("add unit tests for the date parser and fix the off-by-one bug it finds")["tier"] == "mid"
    assert suggest_tier("implement pagination on the orders endpoint")["tier"] == "mid"
    many = "update " + " ".join(f"src/m{i}.py" for i in range(6)) + " so they share the new helper"
    res = suggest_tier(many)
    assert res["files"] == 6 and any("names 6 files" in r for r in res["reasons"])
    assert suggest_tier("")["reasons"]


def test_model_suggest_cli(tmp_path):
    w = _world(tmp_path, [_row(0, "claude-sonnet-4-5", 1000)])
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Find the root cause of the intermittent deadlock in the job queue", encoding="utf-8")
    buf = io.StringIO()
    with credential_guard(), redirect_stdout(buf):
        code = main(["model", "suggest", "--file", str(prompt), "--home", str(w / "home"),
                     "--root", str(w / "root"), "--sys-snapshot", str(w / "sys")])
    out = buf.getvalue()
    assert code == 0 and "Suggested tier: TOP" in out and "Claude Code: opus" in out
    assert "rule of thumb, not a measured result" in out
    buf = io.StringIO()
    with credential_guard(), redirect_stdout(buf):
        main(["model", "suggest", "rename", "a", "variable", "--json", "--home", str(w / "home"),
              "--root", str(w / "root"), "--sys-snapshot", str(w / "sys")])
    data = json.loads(buf.getvalue())
    assert data["tier"] == "light" and data["tools"][0]["model"] == "haiku"


def _cli(w: Path, *args: str) -> str:
    buf = io.StringIO()
    with credential_guard(), redirect_stdout(buf):
        assert main([*args, "--home", str(w / "home"), "--root", str(w / "root"),
                     "--sys-snapshot", str(w / "sys")]) == 0
    return buf.getvalue()


def test_model_default_dry_run_then_apply_then_undo(tmp_path):
    w = _world(tmp_path, [_row(0, "claude-opus-4-1", 1000)])
    settings = w / "home" / ".claude" / "settings.json"
    original = settings.read_bytes()
    out = _cli(w, "model", "default", "--tier", "mid", "--tool", "claude")
    assert "Dry run" in out and "model: opus -> sonnet" in out
    assert settings.read_bytes() == original
    assert not (w / "home" / ".tokenstretch" / "manifests").exists()

    out = _cli(w, "model", "default", "--tier", "light", "--apply")
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data == {"model": "haiku", "theme": "dark"}
    assert "backed up" in out and "tokenstretch undo" in out
    assert "already on haiku" in _cli(w, "model", "default", "--tier", "light", "--apply")

    _cli(w, "undo")
    assert settings.read_bytes() == original
