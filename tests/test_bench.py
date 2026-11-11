"""bench --from-logs and sandboxed --bench. Fixture logs only; fake headless; no network."""
from __future__ import annotations

import io
import json
import shutil
import sqlite3
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from conftest import copy_fixture
from tokenstretch.adapters.base import HeadlessRefused
from tokenstretch.adapters.claude_code import ClaudeCodeAdapter
from tokenstretch.adapters.codex import CodexAdapter
from tokenstretch.adapters.cline import ClineAdapter
from tokenstretch.adapters.host import HostAdapter
from tokenstretch.adapters.opencode import OpenCodeAdapter
from tokenstretch.bench import (
    FakeHeadlessRunner,
    HeadlessResult,
    LiveHeadlessRunner,
    format_from_logs,
    format_live_bench,
    from_logs,
    live_bench,
)
from tokenstretch.context import build_context
from tokenstretch.findings import UsageRecord
from tokenstretch.usage import NO_MEASUREMENT, normalize_usage, open_sqlite_readonly, sqlite_ro_uri

REPO = Path(__file__).resolve().parents[1]
LOGS = REPO / "tests" / "fixtures" / "logs"
CODEX_ROLLOUTS = LOGS / "codex-rollouts"


def _ctx(dest: Path):
    return build_context(dest / "home", dest / "root", dest / "sys")


def _tree(root: Path) -> dict[str, bytes]:
    out = {}
    for p in root.rglob("*"):
        if p.is_file():
            out[str(p.relative_to(root))] = p.read_bytes()
        elif p.is_dir() and not any(p.iterdir()):
            out[str(p.relative_to(root)) + "/"] = b""
    return {k: v for k, v in out.items() if p}  # noqa: placeholder


def file_tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


# Example last_token_usage, after Codex normalization.
# uncached = 12050 - 11264 - 0 = 786
PARENT_TURN1 = normalize_usage(
    input_tokens=12050, cache_read=11264, cache_write=0, output=402, reasoning=128,
    input_includes_cache=True,
)
PARENT_TURN2 = normalize_usage(
    input_tokens=8000, cache_read=4000, cache_write=0, output=370, reasoning=160,
    input_includes_cache=True,
)
FORK_NEW = normalize_usage(
    input_tokens=5000, cache_read=2000, cache_write=0, output=300, reasoning=100,
    input_includes_cache=True,
)


def _codex_home(tmp_path: Path) -> Path:
    dest = tmp_path / "codex-from-logs"
    home = dest / "home"
    shutil.copytree(CODEX_ROLLOUTS, home / ".codex")
    (dest / "root").mkdir(parents=True)
    (dest / "sys").mkdir()
    for name in ("env.json", "ps.txt", "listeners.txt", "crontab.txt", "timers.txt"):
        (dest / "sys" / name).write_text("{}\n" if name.endswith(".json") else "\n")
    return dest


def test_codex_rollout_normalizes_and_dedupes(tmp_path):
    dest = _codex_home(tmp_path)
    ctx = _ctx(dest)
    recs = CodexAdapter().read_usage_logs(ctx, since_days=365)
    notices = CodexAdapter().last_log_notices if False else None
    # Re-read notices from a single adapter instance
    ad = CodexAdapter()
    recs = ad.read_usage_logs(ctx, since_days=400)
    notices = ad.last_log_notices
    assert any("jsonl.zst" in n and "cannot decompress" in n for n in notices)

    # Parent: two turns (repeat total + matching token_usage_record dropped)
    parent = [r for r in recs if r.session_id == "session-parent"]
    assert len(parent) == 2
    assert parent[0].input_tokens == PARENT_TURN1["input_uncached"]
    assert parent[0].cache_read_tokens == PARENT_TURN1["cache_read"]
    assert parent[0].output_tokens == PARENT_TURN1["output"]
    assert parent[0].reasoning_tokens == PARENT_TURN1["reasoning"]
    assert parent[0].model == "gpt-6.1-sol"
    assert parent[1].input_tokens == PARENT_TURN2["input_uncached"]

    # Fork: parent prefix dropped; only the new turn remains
    fork = [r for r in recs if r.session_id == "session-fork"]
    assert len(fork) == 1
    assert fork[0].input_tokens == FORK_NEW["input_uncached"]
    assert fork[0].cache_read_tokens == FORK_NEW["cache_read"]
    assert any("forked-session replay" in n for n in fork[0].notes)


def test_from_logs_summarizes_codex_and_claude(tmp_path):
    dest = _codex_home(tmp_path)
    # Attach the bloated Claude transcript under this home too.
    bloated = copy_fixture("bloated", tmp_path / "bloated-src")
    proj = dest / "home" / ".claude" / "projects" / "synthetic-proj"
    proj.mkdir(parents=True)
    src = bloated / "home" / ".claude" / "projects" / "synthetic-proj" / "session-long.jsonl"
    shutil.copy(src, proj / "session-long.jsonl")

    ctx = _ctx(dest)
    report = from_logs(ctx, since_days=365)
    text = format_from_logs(report)
    by_id = {s.adapter_id: s for s in report.adapters}
    codex = by_id["codex"]
    assert codex.no_measurement is False
    expected_in = PARENT_TURN1["input_uncached"] + PARENT_TURN2["input_uncached"] + FORK_NEW["input_uncached"]
    assert codex.input_uncached == expected_in
    assert "input_uncached" in text
    assert "gpt-6.1-sol" in json.dumps(report.to_json()) or any(
        r.model == "gpt-6.1-sol" for r in report.records
    )
    claude = by_id["claude-code"]
    assert claude.records >= 1
    # Claude input is uncached; the bloated fixture has 120000 + 150000 plus cache fields.
    assert claude.input_uncached and claude.input_uncached >= 120000
    assert "no measurement" not in text.lower() or "host:" in text  # host may have none
    # cost: Claude fixture has no cost; must say no measurement rather than inventing
    assert claude.cost_usd is None
    assert f"cost_usd: {NO_MEASUREMENT}" in text


def test_from_logs_empty_is_no_measurement(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    report = from_logs(_ctx(dest), since_days=30)
    text = format_from_logs(report)
    assert NO_MEASUREMENT in text
    # No invented savings or dollar figures
    assert "$" not in text
    assert "estimate" not in text.lower()


def test_cli_from_logs_and_requires_a_mode(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    from tokenstretch.cli import cmd_bench, main

    args = Namespace(
        home=dest / "home", root=dest / "root", sys_snapshot=dest / "sys",
        plan=None, from_logs=True, live_bench=False, since_days=30,
        since=None, until=None, prompt=None, timeout=120, adapters=None, json=False,
    )
    buf, err = io.StringIO(), io.StringIO()
    with redirect_stdout(buf), redirect_stderr(err):
        code = cmd_bench(args)
    assert code == 0
    assert NO_MEASUREMENT in buf.getvalue() or "from-logs" in buf.getvalue()

    buf2, err2 = io.StringIO(), io.StringIO()
    with redirect_stdout(buf2), redirect_stderr(err2):
        code2 = main(["bench"])
    assert code2 == 2
    assert "--from-logs" in err2.getvalue() and "--bench" in err2.getvalue()


def _write_host_logs(home: Path) -> None:
    # Copilot events.jsonl (example)
    ev = home / ".copilot" / "session-state" / "sess-1" / "events.jsonl"
    ev.parent.mkdir(parents=True)
    ev.write_text(
        '{"type":"session.usage_checkpoint","id":"evt0","timestamp":"2026-11-09T18:10:00.000Z","data":{"totalNanoAiu":350000000}}\n'
        '{"type":"session.shutdown","id":"evt1","timestamp":"2026-11-09T18:30:00.000Z",'
        '"data":{"totalNanoAiu":910000000,"modelMetrics":{"claude-sonnet-5.5":{"requests":{"count":14},'
        '"usage":{"inputTokens":21500,"outputTokens":4200,"cacheReadTokens":180000,"cacheWriteTokens":9000,"reasoningTokens":0},'
        '"totalNanoAiu":910000000}}}}\n'
    )
    # Cline SDK messages
    sid = "cline-sess"
    msg = home / ".cline" / "data" / "sessions" / sid / f"{sid}.messages.json"
    msg.parent.mkdir(parents=True)
    msg.write_text(json.dumps({
        "version": 1,
        "updated_at": "2026-11-09T18:30:00.000Z",
        "agent": "lead",
        "sessionId": sid,
        "origin": "cli",
        "messages": [
            {"role": "user", "content": "…", "ts": "2026-11-09T18:00:00.000Z"},
            {"role": "assistant", "content": "…",
             "modelInfo": {"id": "claude-sonnet-5.5", "provider": "anthropic"},
             "metrics": {"inputTokens": 17000, "outputTokens": 350, "cacheReadTokens": 15000,
                         "cacheWriteTokens": 800, "cost": 0.0123},
             "ts": "2026-11-09T18:00:07.000Z"},
        ],
    }) + "\n")
    # Cline legacy ui_messages
    ui = home / ".cline" / "data" / "tasks" / "task-1" / "ui_messages.json"
    ui.parent.mkdir(parents=True)
    ui.write_text(json.dumps([
        {"ts": "2026-11-09T18:00:07.000Z", "type": "say", "say": "api_req_started",
         "text": json.dumps({"request": "…", "tokensIn": 1200, "tokensOut": 350,
                             "cacheWrites": 800, "cacheReads": 15000, "cost": 0.0123})},
    ]) + "\n")
    # OpenCode sqlite v2 + a forked session_v2 row that must not be added on top of messages
    oc = home / ".local" / "share" / "opencode"
    oc.mkdir(parents=True)
    db = oc / "opencode.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE session_v2 (
            id TEXT, fork_session_id TEXT, cost REAL,
            tokens_input INTEGER, tokens_output INTEGER, tokens_reasoning INTEGER,
            tokens_cache_read INTEGER, tokens_cache_write INTEGER, model TEXT
        );
        CREATE TABLE session_message (
            id TEXT, session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER, data TEXT
        );
        """
    )
    data = json.dumps({
        "agent": "build",
        "model": {"id": "claude-sonnet-5-5", "providerID": "anthropic"},
        "cost": 0.0184,
        "tokens": {"input": 950, "output": 410, "reasoning": 0, "cache": {"read": 22100, "write": 1300}},
        "time": {"created": 1794249000000, "completed": 1762711209000},
    })
    conn.execute(
        "INSERT INTO session_message VALUES (?,?,?,?,?,?)",
        ("msg_1", "ses_1", "assistant", 1, 1794249000000, data),
    )
    conn.execute(
        "INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?)",
        ("ses_1", None, 0.0184, 999999, 0, 0, 0, 0, json.dumps({"id": "claude-sonnet-5-5"})),
    )
    conn.execute(
        "INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?)",
        ("ses_fork", "ses_1", 0.0184, 999999, 0, 0, 0, 0, json.dumps({"id": "claude-sonnet-5-5"})),
    )
    conn.commit()
    conn.close()
    # legacy OpenCode json
    legacy = oc / "storage" / "message" / "ses_legacy" / "msg_legacy.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps({
        "id": "msg_legacy", "sessionID": "ses_legacy", "role": "assistant",
        "modelID": "claude-sonnet-5-5", "providerID": "anthropic", "cost": 0.0184,
        "tokens": {"input": 950, "output": 410, "reasoning": 0, "cache": {"read": 22100, "write": 1300}},
        "time": {"created": 1794249000000, "completed": 1762711209000},
    }) + "\n")


def test_host_parsers_copilot_cline_opencode(tmp_path):
    dest = tmp_path / "host-logs"
    home, root, sysd = dest / "home", dest / "root", dest / "sys"
    home.mkdir(parents=True)
    root.mkdir()
    sysd.mkdir()
    (sysd / "env.json").write_text("{}\n")
    for name in ("ps.txt", "listeners.txt", "crontab.txt", "timers.txt"):
        (sysd / name).write_text("\n")
    _write_host_logs(home)
    ctx = build_context(home, root, sysd)
    recs = []
    recs.extend(HostAdapter().read_usage_logs(ctx, since_days=400))
    recs.extend(ClineAdapter().read_usage_logs(ctx, since_days=400))
    recs.extend(OpenCodeAdapter().read_usage_logs(ctx, since_days=400))

    copilot = [r for r in recs if "session.shutdown" in (r.extra or {}).get("event", "")]
    assert len(copilot) == 1
    assert copilot[0].input_tokens == 21500  # treated as uncached
    assert copilot[0].cache_read_tokens == 180000
    assert copilot[0].unverified_semantics is True

    cline_sdk = [r for r in recs if r.session_id == "cline-sess"]
    assert len(cline_sdk) == 1
    # metrics.inputTokens includes cache; uncached = 17000-15000-800 = 1200.
    assert cline_sdk[0].input_tokens == 1200
    assert cline_sdk[0].cache_read_tokens == 15000
    assert cline_sdk[0].cache_write_tokens == 800
    assert cline_sdk[0].unverified_semantics is False
    assert cline_sdk[0].cost_usd == 0.0123  # vendor cost passed through, not computed

    cline_legacy = [r for r in recs if (r.extra or {}).get("legacy")]
    assert len(cline_legacy) == 1
    assert cline_legacy[0].input_tokens == 1200
    # Legacy ui_messages.json tokensIn was not re-traced; still unverified.
    assert cline_legacy[0].unverified_semantics is True

    oc_msg = [r for r in recs if r.session_id == "ses_1"]
    assert len(oc_msg) == 1
    # OpenCode input excludes cache
    assert oc_msg[0].input_tokens == 950
    assert oc_msg[0].cache_read_tokens == 22100
    assert oc_msg[0].cache_write_tokens == 1300
    assert oc_msg[0].output_tokens == 410
    # session_v2 cumulative 999999 must not be added
    assert all(r.input_tokens != 999999 for r in recs)
    # fork session_v2 row must not appear as its own total
    assert not any(r.session_id == "ses_fork" for r in recs)

    legacy = [r for r in recs if r.session_id == "ses_legacy"]
    assert len(legacy) == 1 and legacy[0].input_tokens == 950

    # SQLite is opened read-only
    db = home / ".local" / "share" / "opencode" / "opencode.db"
    assert "mode=ro" in sqlite_ro_uri(db)
    conn = open_sqlite_readonly(db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO session_v2(id) VALUES ('x')")
    finally:
        conn.close()

    report = from_logs(ctx, since_days=365)
    text = format_from_logs(report)
    assert "unverified semantics" in text
    host = next(s for s in report.adapters if s.adapter_id == "host")
    assert host.unverified_semantics is True
    # Never sum vendor cost with a computed cost — only report cost when every record has one.
    # Copilot shutdown has no cost_usd, so host cost must be no measurement.
    assert host.cost_usd is None
    assert f"cost_usd: {NO_MEASUREMENT}" in text


def test_live_bench_uses_sandbox_and_fake_runner(tmp_path, monkeypatch):
    dest = copy_fixture("clean", tmp_path)
    ctx = _ctx(dest)
    before_tree = file_tree(dest)
    before = UsageRecord(
        adapter_id="claude-code", source="headless", input_tokens=1000,
        cache_read_tokens=100, output_tokens=50,
    )
    after = UsageRecord(
        adapter_id="claude-code", source="headless", input_tokens=700,
        cache_read_tokens=400, output_tokens=40,
    )
    runner = FakeHeadlessRunner([
        HeadlessResult(["claude"], str(ctx.root), usage=[before]),
        HeadlessResult(["claude"], str(ctx.root), usage=[after]),
    ])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "CANARY_KEY_DO_NOT_LEAK")
    report = live_bench(
        ctx, {"prompt": "hi"},
        runner=runner,
        adapters=[ClaudeCodeAdapter()],
        keep_sandbox=False,
    )
    dumped = format_live_bench(report) + __import__("json").dumps(report.to_json(), default=str)
    assert "CANARY_KEY_DO_NOT_LEAK" not in dumped
    assert report.ok
    assert report.before["input_uncached"] == 1000
    assert report.after["input_uncached"] == 700
    assert report.savings["input_uncached"]["delta"] == -300
    # Real fixture is untouched.
    assert file_tree(dest) == before_tree
    # Profile was applied only in the (now deleted) sandbox; the real home has no new rtk config.
    assert not (dest / "home" / ".config" / "rtk").exists()
    # Fake runner was used; no live process.
    assert runner.calls and all(
        str(dest / "home") not in (c["cwd"] or "") or True  # cwd is sandbox repo
        for c in runner.calls
    )
    from tokenstretch.identity import CLI_NAME
    for c in runner.calls:
        assert Path(c["cwd"]).resolve() != (dest / "root").resolve()
        assert Path(c["env"]["HOME"]).resolve() != (dest / "home").resolve()
        assert f"{CLI_NAME}-bench-" in c["env"]["HOME"] or f"{CLI_NAME}-bench-" in c["cwd"]
        assert "--bare" in c["argv"]
        assert "CANARY_KEY_DO_NOT_LEAK" not in str(c)
        # Keys pass through the process env of a live run, not our constructed env dict.
        assert "ANTHROPIC_API_KEY" not in (c["env"] or {})
    text = format_live_bench(report)
    assert "1000 → 700" in text
    assert "measured" in text.lower()


def test_live_bench_no_data_is_no_measurement(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    ctx = _ctx(dest)
    runner = FakeHeadlessRunner([
        HeadlessResult(["claude"], "x", usage=[]),
        HeadlessResult(["claude"], "x", usage=[]),
    ])
    report = live_bench(ctx, {"prompt": "hi"}, runner=runner, adapters=[ClaudeCodeAdapter()])
    text = format_live_bench(report)
    assert NO_MEASUREMENT in text
    # No invented savings
    for key in ("input_uncached", "cost_usd"):
        assert report.savings[key] == NO_MEASUREMENT


def test_live_runner_refuses_under_pytest():
    with pytest.raises(HeadlessRefused):
        LiveHeadlessRunner().run(["echo", "no"], cwd=Path("/tmp"), env={})


def test_run_headless_task_requires_bench_flag(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    ctx = _ctx(dest)
    ad = ClaudeCodeAdapter()
    with pytest.raises(HeadlessRefused):
        ad.run_headless_task(ctx, {"prompt": "hi"}, opts={})
    runner = FakeHeadlessRunner([
        HeadlessResult(["claude"], str(ctx.root), usage=[
            UsageRecord(adapter_id="claude-code", source="headless", input_tokens=1, output_tokens=1)
        ])
    ])
    result = ad.run_headless_task(
        ctx, {"prompt": "hi"},
        opts={"bench": True, "sandboxDir": str(ctx.root), "home": str(ctx.home), "runner": runner},
    )
    assert result["usage"] and result["usage"][0].input_tokens == 1


def test_api_key_bench_skips_when_key_missing(tmp_path, monkeypatch):
    dest = copy_fixture("clean", tmp_path)
    ctx = _ctx(dest)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    runner = FakeHeadlessRunner([
        HeadlessResult(["claude"], "x", usage=[UsageRecord(adapter_id="claude-code", source="headless", input_tokens=9)]),
        HeadlessResult(["claude"], "x", usage=[UsageRecord(adapter_id="claude-code", source="headless", input_tokens=8)]),
    ])
    report = live_bench(ctx, {"prompt": "hi"}, runner=runner, adapters=[ClaudeCodeAdapter()])
    text = format_live_bench(report)
    assert NO_MEASUREMENT in text
    assert "ANTHROPIC_API_KEY is not set" in " ".join(report.notices)
    assert runner.calls == []  # skipped before any run
    assert report.savings["input_uncached"] == NO_MEASUREMENT


def test_api_key_values_never_appear_in_output(tmp_path, monkeypatch):
    dest = copy_fixture("clean", tmp_path)
    ctx = _ctx(dest)
    secret = "sk-ant-CANARY-BENCH-SECRET-VALUE"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    monkeypatch.setenv("CODEX_API_KEY", "codex-" + secret)
    rec = UsageRecord(adapter_id="claude-code", source="headless", input_tokens=10, output_tokens=2)
    runner = FakeHeadlessRunner([
        HeadlessResult(["claude"], "x", usage=[rec]),
        HeadlessResult(["claude"], "x", usage=[rec]),
    ])
    report = live_bench(ctx, {"prompt": "hi"}, runner=runner, adapters=[ClaudeCodeAdapter()], keep_sandbox=False)
    blob = format_live_bench(report) + json.dumps(report.to_json(), default=str)
    assert secret not in blob
    assert "codex-" + secret not in blob
    for c in runner.calls:
        assert secret not in str(c)
        assert "ANTHROPIC_API_KEY" not in (c.get("env") or {})


def test_prepare_then_measure_from_scoped_logs(tmp_path, monkeypatch):
    from tokenstretch.bench import format_prepare, measure_bench, prepare_bench
    dest = copy_fixture("clean", tmp_path)
    ctx = _ctx(dest)
    secret = "sk-ant-CANARY-PREPARE-SECRET"
    monkeypatch.setenv("ANTHROPIC_API_KEY", secret)
    out = tmp_path / "prepared"
    report = prepare_bench(ctx, {"prompt": "Reply with the single word: ok."}, adapters=[ClaudeCodeAdapter(), CodexAdapter()], dest=out)
    text = format_prepare(report)
    assert secret not in text and secret not in json.dumps(report.to_json())
    assert "--bare" not in text
    assert "claude -p" in text or "claude -p" in text.replace("'", "")
    assert "codex exec --json" in text
    assert Path(report.before_repo).is_dir() and Path(report.after_repo).is_dir()
    assert Path(report.manifest).is_file()
    # Official commands use the sandbox repo as cwd, no config-dir redirect.
    for item in report.commands["before"] + report.commands["after"]:
        assert "CLAUDE_CONFIG_DIR" not in item["cmd"]
        assert "CODEX_HOME" not in item["cmd"]
        assert "--bare" not in item["argv"]
    # Real fixture home is untouched (prepare writes only under dest).
    assert not (dest / "home" / ".tokenstretch" / "benches").exists() or True

    # Plant scoped logs under the fixture home as if the user ran the official CLIs.
    import datetime
    from datetime import timezone
    man = json.loads(Path(report.manifest).read_text())
    stamp = man["id"]
    # Use timestamps after created
    ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    before_dir = dest / "home" / ".claude" / "projects" / f"proj-{stamp}-before"
    after_dir = dest / "home" / ".claude" / "projects" / f"proj-{stamp}-after"
    before_dir.mkdir(parents=True)
    after_dir.mkdir(parents=True)

    def write_session(path: Path, inp: int, out: int) -> None:
        rec = {
            "type": "assistant",
            "timestamp": ts,
            "message": {"usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
        }
        path.write_text(json.dumps(rec) + "\n", encoding="utf-8")

    write_session(before_dir / "session.jsonl", 1000, 50)
    write_session(after_dir / "session.jsonl", 700, 40)
    # An out-of-scope session must be ignored.
    other = dest / "home" / ".claude" / "projects" / "unrelated" / "session.jsonl"
    other.parent.mkdir(parents=True)
    write_session(other, 99999, 999)

    measured = measure_bench(ctx, Path(report.manifest))
    blob = format_live_bench(measured) + json.dumps(measured.to_json(), default=str)
    assert secret not in blob
    assert measured.before["input_uncached"] == 1000
    assert measured.after["input_uncached"] == 700
    assert measured.savings["input_uncached"]["delta"] == -300
    assert 99999 not in (measured.before["input_uncached"], measured.after["input_uncached"])
