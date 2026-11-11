"""Gemini CLI and Copilot CLI tier-1 adapters: logs, watch, bench, hooks, setup, undo, doctor."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from conftest import credential_guard, finding_ids, findings_with
from tokenstretch.adapters import REGISTRY
from tokenstretch.adapters.base import HeadlessRefused, HookPlan
from tokenstretch.bench import FakeHeadlessRunner, HeadlessResult, live_bench, prepare_bench
from tokenstretch.catalog import CLEAN_HOOK_PLAN_ITEMS
from tokenstretch.context import build_context
from tokenstretch.installer import apply_changes, plan_file_changes
from tokenstretch.logcache import LogCache
from tokenstretch.models import run_default
from tokenstretch.setup import build_plan, collect_findings

GEM = next(a for a in REGISTRY if a.id == "gemini-cli")
COP = next(a for a in REGISTRY if a.id == "copilot-cli")


def _world(tmp_path: Path) -> Path:
    for d in ("home", "root", "sys"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / "sys" / "env.json").write_text("{}\n")
    return tmp_path


def _ctx(w: Path, env: dict | None = None, rc: str = ""):
    if env is not None:
        (w / "sys" / "env.json").write_text(json.dumps(env))
    if rc:
        (w / "home" / ".zshrc").write_text(rc)
    return build_context(w / "home", w / "root", w / "sys")


def _snapshot(w: Path) -> dict:
    out = {}
    for base in (w / "home", w / "root"):
        for p in base.rglob("*"):
            if p.is_file() and ".tokenstretch" not in p.parts:
                out[str(p)] = p.read_bytes()
    return out


# ---- Gemini fixtures -------------------------------------------------------------------

def _gemini_home(w: Path, settings: dict | None = None) -> Path:
    g = w / "home" / ".gemini"
    g.mkdir(parents=True, exist_ok=True)
    (g / "settings.json").write_text(json.dumps(settings if settings is not None else {"model": {"name": "gemini-2.5-pro"}}))
    # Credential canaries: must only ever be stat'd.
    for name in ("oauth_creds.json", "google_accounts.json", "mcp-oauth-tokens.json", ".env"):
        (g / name).write_text("CANARY_CREDENTIAL_DO_NOT_READ_GEMINI_OAUTH")
    return g


def _msg(mid, ts, inp, cached, out, thoughts=0, tool=0, model="gemini-2.5-flash", calls=0):
    return {"id": mid, "timestamp": ts, "type": "gemini", "content": "SECRET PROMPT TEXT", "model": model,
            "toolCalls": [{"id": f"c{i}", "name": "read_file", "args": {}, "status": "success", "timestamp": ts}
                          for i in range(calls)],
            "tokens": {"input": inp, "output": out, "cached": cached, "thoughts": thoughts, "tool": tool,
                       "total": inp + out + thoughts + tool}}


def _gemini_chat(w: Path, project_root: Path, *, slug="myproj", name="session-2026-11-10T10-00-abcd1234.jsonl",
                 lines=None) -> Path:
    proj = w / "home" / ".gemini" / "tmp" / slug
    chats = proj / "chats"
    chats.mkdir(parents=True, exist_ok=True)
    (proj / ".project_root").write_text(str(project_root) + "\n")
    rows = lines if lines is not None else [
        {"sessionId": "sess-gem-1", "projectHash": "h", "startTime": "2026-11-10T10:00:00Z",
         "lastUpdated": "2026-11-10T10:05:00Z", "kind": "main"},
        {"id": "u1", "timestamp": "2026-11-10T10:00:01Z", "type": "user", "content": "SECRET PROMPT TEXT"},
        # first append without tokens, then the same id again with tokens (last wins)
        {**_msg("g1", "2026-11-10T10:00:02Z", 0, 0, 0), "tokens": None},
        _msg("g1", "2026-11-10T10:00:02Z", 12000, 8000, 300, thoughts=50, tool=100, calls=2),
        _msg("g2", "2026-11-10T10:01:00Z", 20000, 15000, 400),
        {"$rewindTo": "g2"},
    ]
    path = chats / name
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


def test_gemini_and_copilot_are_tier1():
    assert GEM.tier == 1 and COP.tier == 1
    for ad in (GEM, COP):
        assert ad.headless_patterns and ad.bench_key_env


def test_gemini_chat_log_parse_dedupe_and_token_mapping(tmp_path):
    w = _world(tmp_path)
    _gemini_home(w)
    path = _gemini_chat(w, w / "root")
    ctx = _ctx(w)
    with credential_guard():
        recs = GEM.read_usage_logs(ctx, since_days=4000)
    assert len(recs) == 2  # g1 counted once (last copy), user lines ignored
    g1 = next(r for r in recs if r.extra["message_id"] == "g1")
    # promptTokenCount includes cached; tool prompt tokens are added to input.
    assert g1.input_tokens == 12000 + 100 - 8000 and g1.cache_read_tokens == 8000
    assert g1.output_tokens == 300 and g1.reasoning_tokens == 50
    assert g1.session_id == "sess-gem-1" and g1.extra["cwd"] == str(w / "root") and g1.extra["tool_calls"] == 2
    assert g1.unverified_semantics is False
    assert "SECRET" not in json.dumps([r.to_json() for r in recs])
    assert GEM.sniff_log(path, path.read_text().splitlines()[:1])


def test_gemini_legacy_json_and_subagent_and_set_messages(tmp_path):
    w = _world(tmp_path)
    _gemini_home(w)
    chats = w / "home" / ".gemini" / "tmp" / hashlib.sha256(str(w / "root").encode()).hexdigest() / "chats"
    chats.mkdir(parents=True)
    (chats / "session-old.json").write_text(json.dumps({
        "sessionId": "legacy", "projectHash": "x", "startTime": "2026-11-01T00:00:00Z", "lastUpdated": "2026-11-01T00:00:00Z",
        "messages": [_msg("m1", "2026-11-01T00:00:01Z", 1000, 0, 10)]}))
    sub = chats / "sess-parent"
    sub.mkdir()
    (sub / "agent-1.jsonl").write_text(
        json.dumps({"sessionId": "agent-1", "projectHash": "x", "kind": "subagent"}) + "\n"
        + json.dumps({"$set": {"messages": [_msg("s1", "2026-11-01T00:00:02Z", 500, 100, 5)]}}) + "\n")
    with credential_guard():
        recs = GEM.read_usage_logs(_ctx(w), since_days=4000)
    by = {r.session_id: r for r in recs}
    assert by["legacy"].input_tokens == 1000 and not by["legacy"].is_subagent
    assert by["agent-1"].is_subagent and by["agent-1"].cache_read_tokens == 100
    # Legacy hash dir: project label resolved by hashing the project root.
    sessions = GEM.watch_sessions(_ctx(w), limit=5)
    assert {s["project"] for s in sessions} == {"root"}


def test_gemini_incremental_cache_matches_full_read(tmp_path):
    w = _world(tmp_path)
    _gemini_home(w)
    path = _gemini_chat(w, w / "root")
    ctx = _ctx(w)
    cache = LogCache()
    assert len(cache.records(ctx, [GEM], since_days=4000)) == 2
    with path.open("a") as fh:
        fh.write(json.dumps(_msg("g3", "2026-11-10T10:02:00Z", 30000, 20000, 500)) + "\n")
        fh.write(json.dumps(_msg("g2", "2026-11-10T10:01:00Z", 20000, 15000, 401)) + "\n")  # re-append, last wins
    recs = cache.records(ctx, [GEM], since_days=4000)
    assert cache.stats["appended"] == 1
    assert len(recs) == 3 and sum(r.output_tokens for r in recs) == 300 + 401 + 500
    assert sorted(r.output_tokens for r in recs) == sorted(r.output_tokens for r in GEM.read_usage_logs(ctx, since_days=4000))


def test_gemini_watch_and_live_sessions(tmp_path):
    w = _world(tmp_path)
    _gemini_home(w)
    _gemini_chat(w, w / "root")
    ctx = _ctx(w)
    with credential_guard():
        ws = GEM.watch_sessions(ctx, limit=5)
        live = GEM.live_sessions(ctx, limit=5)
    assert len(ws) == 1
    s = ws[0]
    assert s["project"] == "root" and s["clear_hint"] == "/clear" and s["compact_hint"] == "/compress"
    assert [t["history_tokens"] for t in s["turns"]] == [12100, 20000]
    assert s["turns"][0]["tool_calls"] == 2 and s["turns"][0]["reads"] == []
    assert live[0]["history_tokens"] == 20000 and live[0]["model"] == "gemini-2.5-flash"
    assert GEM.watch_sessions(ctx, limit=5, max_age_s=0.000001) == []


STREAM = "\n".join([
    json.dumps({"type": "init", "timestamp": "t", "session_id": "s", "model": "gemini-2.5-flash"}),
    json.dumps({"type": "message", "role": "assistant", "content": "hi"}),
    json.dumps({"type": "result", "status": "success", "stats": {
        "total_tokens": 1500, "input_tokens": 1200, "output_tokens": 300, "cached": 1000, "input": 200,
        "duration_ms": 10, "tool_calls": 1,
        "models": {"gemini-2.5-flash": {"total_tokens": 1500, "input_tokens": 1200, "output_tokens": 300,
                                        "cached": 1000, "input": 200}}}}),
])


def test_gemini_headless_parse_and_bench_sandbox(tmp_path):
    rec = GEM.parse_headless_output(STREAM, "t1")
    assert rec.input_tokens == 200 and rec.cache_read_tokens == 1000 and rec.output_tokens == 300
    assert rec.model == "gemini-2.5-flash" and rec.success is True
    assert GEM.parse_headless_output("not json", "t1") is None
    w = _world(tmp_path)
    ctx = _ctx(w)
    with pytest.raises(HeadlessRefused):
        GEM.run_headless_task(ctx, {"prompt": "hi"}, opts={})
    with pytest.raises(HeadlessRefused):
        GEM.run_headless_task(ctx, {"prompt": "hi"}, opts={"bench": True, "home": os.path.expanduser("~")})
    runner = FakeHeadlessRunner([HeadlessResult(["gemini"], str(w / "root"), 0, STREAM, "")])
    sandbox_home = w / "sandbox-home"
    out = GEM.run_headless_task(ctx, {"prompt": "hi", "model": "flash"},
                                opts={"bench": True, "home": str(sandbox_home), "sandboxDir": str(w / "root"), "runner": runner})
    call = runner.calls[0]
    assert call["argv"][:5] == ["gemini", "-p", "hi", "--output-format", "stream-json"]
    assert call["argv"][-2:] == ["--model", "flash"]
    assert call["env"]["GEMINI_CLI_HOME"] == str(sandbox_home) and call["env"]["HOME"] == str(sandbox_home)
    # Usage statistics are off in the sandbox before the run; the real HOME is untouched.
    assert json.loads((sandbox_home / ".gemini" / "settings.json").read_text())["privacy"]["usageStatisticsEnabled"] is False
    assert out["usage"][0].input_tokens == 200


def test_gemini_install_hooks_apply_and_undo(tmp_path):
    w = _world(tmp_path)
    _gemini_home(w, {"model": {"name": "flash"}, "hooks": {"BeforeTool": [{"matcher": "run_shell_command",
                                                                           "hooks": [{"type": "command", "command": "mine"}]}]}})
    (w / "root" / ".gitignore").write_text("node_modules/\n")
    ctx = _ctx(w)
    before = _snapshot(w)
    with credential_guard():
        res = GEM.install_hooks(ctx, HookPlan(items=list(CLEAN_HOOK_PLAN_ITEMS)), dry_run=True)
    paths = {Path(c["path"]).name: c for c in res["changes"]}
    assert set(paths) == {"gemini-session-start.sh", "settings.json", ".geminiignore"}
    settings = json.loads(paths["settings.json"]["content"])
    assert settings["hooks"]["BeforeTool"][0]["hooks"][0]["command"] == "mine"  # user hooks kept
    hook = settings["hooks"]["SessionStart"][0]["hooks"][0]
    assert hook["type"] == "command" and hook["command"].startswith("sh ") and hook["timeout"] == 5000
    ignore = paths[".geminiignore"]["content"]
    assert "dist/" in ignore and "node_modules/" not in ignore  # already in .gitignore
    assert json.loads(paths["gemini-session-start.sh"]["content"].split("echo '")[1].rsplit("'", 1)[0])["systemMessage"]
    # The core keeps the script mode; apply + undo restores everything.
    changes = [c for c in plan_file_changes(ctx) if "gemini" in c["path"] or ".geminiignore" in c["path"]
               or c["path"].endswith(".gemini/settings.json")]
    assert any(c.get("mode") == 0o755 for c in changes)
    applied = apply_changes(ctx, changes, stamp="gemtest")
    script = w / "home" / ".tokenstretch" / "hooks" / "gemini-session-start.sh"
    assert script.is_file() and os.access(script, os.X_OK)
    assert GEM.install_hooks(ctx, HookPlan(items=list(CLEAN_HOOK_PLAN_ITEMS)))["changes"] == []  # idempotent
    with credential_guard():
        GEM.undo(ctx, applied["manifest_path"])
    assert _snapshot(w) == before


def test_gemini_install_hooks_skip_unreadable_settings_and_undetected(tmp_path):
    w = _world(tmp_path)
    assert GEM.install_hooks(_ctx(w), HookPlan(items=list(CLEAN_HOOK_PLAN_ITEMS)))["changes"] == []
    g = _gemini_home(w)
    (g / "settings.json").write_text("// comment\n{\"model\": {}}\n")
    res = GEM.install_hooks(_ctx(w), HookPlan(items=["session_start_note"]))
    assert not any(c["path"].endswith("settings.json") for c in res["changes"])
    assert GEM.plan_default_model(_ctx(w), "mid") is None


def test_gemini_setup_items_and_default_model(tmp_path):
    w = _world(tmp_path)
    _gemini_home(w)
    (w / "root" / "GEMINI.md").write_text("# Project\n")
    ctx = _ctx(w)
    with credential_guard():
        items = GEM.setup_items(ctx)
        item = GEM.plan_default_model(ctx, "mid")
    ids = {i.id for i in items}
    assert ids == {"gemini-usage-stats-off", "gemini-rules-block"}
    stats = next(i for i in items if i.id == "gemini-usage-stats-off")
    new = json.loads(stats.changes[0]["content"])
    assert new["privacy"]["usageStatisticsEnabled"] is False and new["model"]["name"] == "gemini-2.5-pro"
    assert json.loads(item.changes[0]["content"])["model"]["name"] == "flash"
    assert GEM.model_tiers() == {"light": "flash-lite", "mid": "flash", "top": "pro"}
    assert GEM.is_top_model("gemini-2.5-pro") and not GEM.is_top_model("gemini-2.5-flash")
    lines: list[str] = []
    before = _snapshot(w)
    assert run_default(ctx, "mid", tool="gemini", apply=False, out=lines.append, adapters=[GEM]) == 0
    assert any("Dry run" in ln for ln in lines) and _snapshot(w) == before
    # No GEMINI.md: setup never creates a new always-loaded file.
    (w / "root" / "GEMINI.md").unlink()
    assert "gemini-rules-block" not in {i.id for i in GEM.setup_items(ctx)}


def test_gemini_doctor_checks(tmp_path):
    w = _world(tmp_path)
    _gemini_home(w, {
        "model": {"name": "gemini-2.5-flash", "compressionThreshold": 0.95},
        "billing": {"overageStrategy": "always"},
        "telemetry": {"enabled": True, "target": "gcp"},
        "privacy": {"usageStatisticsEnabled": False},
        "general": {"enableAutoUpdate": False},
    })
    long_rows = [{"sessionId": f"s{i}", "projectHash": "h"} for i in range(1)] + [
        _msg(f"m{i}", f"2026-11-10T10:0{i}:00Z", 200_000, 150_000, 100) for i in range(3)]
    _gemini_chat(w, w / "root", lines=long_rows)
    with credential_guard():
        findings = collect_findings(_ctx(w), adapters=[GEM])
    ids = {f.id for f in findings}
    assert {"W14", "W16", "W20", "P01"} <= ids and "P05" not in ids
    p01 = [f for f in findings if f.id == "P01"]
    assert len(p01) == 1 and p01[0].severity == "high" and "prompts" in p01[0].message
    # Env override wins; a local outfile with prompts is a local-retention note, not off-box telemetry.
    _gemini_home(w, {"telemetry": {"enabled": True, "target": "gcp"}})
    ctx = _ctx(w, env={"GEMINI_TELEMETRY_TARGET": "local", "GEMINI_TELEMETRY_OUTFILE": "/tmp/t.json"})
    with credential_guard():
        found = collect_findings(ctx, adapters=[GEM])
    assert not any(f.id == "P01" and f.severity == "high" for f in found)
    assert any(f.id == "P03" and "telemetry" in f.message.lower() for f in found)
    assert any(f.id == "P05" for f in found)


# ---- Copilot ---------------------------------------------------------------------------

def _copilot_home(w: Path, settings: dict | None = None) -> Path:
    c = w / "home" / ".copilot"
    c.mkdir(parents=True, exist_ok=True)
    (c / "settings.json").write_text(json.dumps(settings or {}))
    (c / "config.json").write_text("CANARY_CREDENTIAL_DO_NOT_READ_COPILOT_TOKEN")
    return c


SHUTDOWN = {"type": "session.shutdown", "id": "e1", "timestamp": "2026-11-09T18:30:00.000Z",
            "data": {"modelMetrics": {"claude-sonnet-4.5": {"requests": {"count": 3}, "usage": {
                "inputTokens": 1000, "outputTokens": 200, "cacheReadTokens": 5000, "cacheWriteTokens": 100,
                "reasoningTokens": 0}}}}}


def _chat_span(sid, ts_ns, inp, cache, out, envelope=False):
    attrs = {"gen_ai.operation.name": "chat", "gen_ai.conversation.id": sid, "gen_ai.response.model": "gpt-5-mini",
             "gen_ai.usage.input_tokens": inp, "gen_ai.usage.cache_read.input_tokens": cache,
             "gen_ai.usage.output_tokens": out, "github.copilot.nano_aiu": 5}
    if not envelope:
        return {"name": "chat gpt-5-mini", "startTimeUnixNano": str(ts_ns), "attributes": attrs}
    return {"resourceSpans": [{"scopeSpans": [{"spans": [{
        "name": "chat gpt-5-mini", "traceId": "t", "spanId": "s", "startTimeUnixNano": str(ts_ns),
        "attributes": [{"key": k, "value": ({"intValue": v} if isinstance(v, int) else {"stringValue": v})}
                       for k, v in attrs.items()]}]}]}]}


def test_copilot_events_and_otel_with_dedupe(tmp_path):
    w = _world(tmp_path)
    c = _copilot_home(w)
    for sid in ("sess-a", "sess-b"):
        ev = c / "session-state" / sid / "events.jsonl"
        ev.parent.mkdir(parents=True)
        ev.write_text(json.dumps({"type": "session.start", "data": {}}) + "\n" + json.dumps(SHUTDOWN) + "\n")
    otel = c / "otel" / "spans.jsonl"
    otel.parent.mkdir(parents=True)
    t0 = 1_794_249_000 * 10**9
    otel.write_text("\n".join(json.dumps(x) for x in (
        _chat_span("sess-a", t0, 300, 2000, 50),
        _chat_span("sess-a", t0 + 10**9, 400, 2500, 60, envelope=True),
        {"name": "invoke_agent", "attributes": {"gen_ai.operation.name": "invoke_agent", "gen_ai.conversation.id": "sess-a",
                                                 "gen_ai.usage.input_tokens": 700}},
    )) + "\n")
    with credential_guard():
        recs = COP.read_usage_logs(_ctx(w), since_days=4000)
    # sess-a: two per-turn chat spans replace its shutdown totals; sess-b keeps its shutdown totals.
    a = [r for r in recs if r.session_id == "sess-a"]
    b = [r for r in recs if r.session_id == "sess-b"]
    assert len(a) == 2 and all(r.extra["span"] == "chat" for r in a)
    assert sum(r.input_tokens for r in a) == 700 and sum(r.cache_read_tokens for r in a) == 4500
    assert len(b) == 1 and b[0].input_tokens == 1000 and b[0].turns == 3
    assert all(r.unverified_semantics for r in recs)
    with credential_guard():
        ws = COP.watch_sessions(_ctx(w), limit=5)
    assert [t["history_tokens"] for t in ws[0]["turns"]] == [2300, 2900]


def test_copilot_watch_unsupported_without_otel(tmp_path):
    w = _world(tmp_path)
    _copilot_home(w)
    assert COP.watch_sessions(_ctx(w)) is None
    assert COP.live_sessions(_ctx(w)) == []


class _OtelWritingRunner:
    """Fake runner that writes what Copilot would write to the OTel file path it was given."""

    def __init__(self):
        self.calls = []

    def run(self, argv, *, cwd, env=None, timeout=120):
        self.calls.append({"argv": list(argv), "env": dict(env or {})})
        sid = argv[argv.index("--session-id") + 1]
        Path(env["COPILOT_OTEL_FILE_EXPORTER_PATH"]).write_text(json.dumps(_chat_span(sid, 10**18, 100, 900, 20)) + "\n")
        return HeadlessResult(list(argv), str(cwd), 0, '{"type":"workflow.result"}\n', "")


def test_copilot_headless_bench_reads_sandbox_otel(tmp_path):
    w = _world(tmp_path)
    ctx = _ctx(w)
    with pytest.raises(HeadlessRefused):
        COP.run_headless_task(ctx, {"prompt": "hi"}, opts={})
    runner = _OtelWritingRunner()
    home = w / "sandbox-home"
    out = COP.run_headless_task(ctx, {"prompt": "hi", "model": "gpt-5-mini", "id": "t1"},
                                opts={"bench": True, "home": str(home), "sandboxDir": str(w / "root"), "runner": runner})
    call = runner.calls[0]
    assert call["argv"][:5] == ["copilot", "-p", "hi", "--output-format", "json"]
    assert "--disable-client-telemetry" in call["argv"] and "--sync-session=off" in call["argv"]
    assert "--model=gpt-5-mini" in call["argv"] and "--allow-all-tools" not in call["argv"]
    env = call["env"]
    assert env["COPILOT_HOME"] == str(home / ".copilot") and env["COPILOT_OTEL_EXPORTER_TYPE"] == "file"
    assert env["OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT"] == "NO_CONTENT"
    assert str(home) in env["COPILOT_OTEL_FILE_EXPORTER_PATH"]
    rec = out["usage"][0]
    assert rec.source == "headless" and rec.task_id == "t1" and rec.input_tokens == 100 and rec.cache_read_tokens == 900


def test_copilot_doctor_checks_never_open_config_json(tmp_path):
    w = _world(tmp_path)
    _copilot_home(w, {"model": "claude-opus-4.5", "effortLevel": "xhigh", "storeTokenPlaintext": True,
                      "remoteSessions": True, "continueOnAutoMode": True, "defaultPermissionMode": "allow-all",
                      "subagents": {"maxConcurrency": 16}})
    ctx = _ctx(w, env={"OTEL_EXPORTER_OTLP_ENDPOINT": "https://otel.example.com",
                       "OTEL_INSTRUMENTATION_GENAI_CAPTURE_CONTENT": "SPAN_ONLY"})
    with credential_guard():
        findings = collect_findings(ctx, adapters=[COP])
        cfg = COP.read_config(ctx)
    ids = {f.id for f in findings}
    assert {"W05", "W06", "W13", "W16", "W18", "P01", "P02", "P05", "P06"} <= ids
    assert cfg["effective"]["effort"]["value"] == "xhigh"
    p06 = next(f for f in findings if f.id == "P06")
    assert p06.severity == "high" and str(p06.evidence["config_json_present"]) == "True"
    p01 = [f for f in findings if f.id == "P01"]
    assert any(f.severity == "high" for f in p01)  # OTLP HTTP export with content capture
    assert any(f.severity == "high" and "remote" in f.message.lower() for f in p01) or any(
        "HTTP endpoint" in f.message for f in p01)
    assert any(f.id == "P02" and f.severity == "med" for f in findings)  # remoteSessions forces export


def test_copilot_client_telemetry_alias_and_env_overrides(tmp_path):
    w = _world(tmp_path)
    _copilot_home(w, {"autoUpdate": False, "model": "gpt-5-mini"})
    ctx = _ctx(w, env={"COPILOT_MODEL": "claude-opus-4.5"}, rc="alias copilot='copilot --disable-client-telemetry'\n")
    with credential_guard():
        findings = collect_findings(ctx, adapters=[COP])
        eff = COP.read_config(ctx)["effective"]
    assert not any(f.id == "P01" for f in findings) and not any(f.id == "P05" for f in findings)
    assert eff["model"]["value"] == "claude-opus-4.5" and eff["model"]["from"].endswith("COPILOT_MODEL")
    assert eff["telemetry"]["value"] is False


def test_copilot_setup_rules_and_install_steps(tmp_path):
    w = _world(tmp_path)
    _copilot_home(w)
    ctx = _ctx(w)
    assert COP.setup_items(ctx) == []  # no copilot-instructions.md: nothing created
    gh = w / "root" / ".github"
    gh.mkdir()
    (gh / "copilot-instructions.md").write_text("# Rules\n")
    items = COP.setup_items(ctx)
    assert [i.id for i in items] == ["copilot-rules-block"] and "/clear" in items[0].changes[0]["content"]
    before = _snapshot(w)
    applied = apply_changes(ctx, items[0].changes, stamp="coptest")
    assert COP.setup_items(ctx) == []
    with credential_guard():
        COP.undo(ctx, applied["manifest_path"])
    assert _snapshot(w) == before
    steps = COP.install_steps("rtk-oss", ctx, {})
    assert steps[0].command == ["rtk", "init", "-g", "--copilot"] and steps[0].manual
    assert GEM.install_steps("rtk-oss", ctx, {})[0].command == ["rtk", "init", "-g", "--gemini"]
    assert COP.install_hooks(ctx, HookPlan(items=list(CLEAN_HOOK_PLAN_ITEMS)))["changes"] == []
    assert COP.model_tiers() == {}
    lines: list[str] = []
    assert run_default(ctx, "mid", tool="copilot", out=lines.append, adapters=[COP]) == 0
    assert "not supported yet" in lines[0]


def test_bench_includes_detected_gemini_and_copilot(tmp_path, monkeypatch):
    w = _world(tmp_path)
    _gemini_home(w)
    _copilot_home(w)
    (w / "root" / "README.md").write_text("hi\n")
    ctx = _ctx(w)
    rep = prepare_bench(ctx, {"prompt": "hi"}, dest=w / "prep")
    adapters = {c["adapter"] for c in rep.commands["before"]}
    assert "gemini-cli" in adapters and "copilot-cli" not in adapters  # Copilot logs cannot be scoped to a repo
    gem_cmd = next(c for c in rep.commands["before"] if c["adapter"] == "gemini-cli")
    assert gem_cmd["argv"][:2] == ["gemini", "-p"]
    for name in ("ANTHROPIC_API_KEY", "CODEX_API_KEY", "GEMINI_API_KEY", "COPILOT_GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    report = live_bench(ctx, {"prompt": "hi"}, runner=FakeHeadlessRunner())
    text = " ".join(report.notices)
    assert "GEMINI_API_KEY is not set" in text and "COPILOT_GITHUB_TOKEN is not set" in text


def test_dashboard_weekly_report_include_gemini_and_copilot(tmp_path):
    from tokenstretch.weekly import read_records, sessions
    w = _world(tmp_path)
    _gemini_home(w)
    _gemini_chat(w, w / "root")
    c = _copilot_home(w)
    ev = c / "session-state" / "sess-b" / "events.jsonl"
    ev.parent.mkdir(parents=True)
    ev.write_text(json.dumps(SHUTDOWN) + "\n")
    ctx = _ctx(w)
    with credential_guard():
        recs = read_records(ctx, days=4000)
        rows = sessions(ctx)
    assert {r.adapter_id for r in recs} >= {"gemini-cli", "copilot-cli"}
    assert any(r["adapter"] == "gemini-cli" and r["clear_hint"] == "/clear" for r in rows)
