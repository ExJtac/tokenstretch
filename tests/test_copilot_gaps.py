"""Copilot CLI follow-ups from adapter-gaps v0.4: --usage-output-file bench usage, cache-inclusive input,
repo scoping via session.start / workspace.yaml, autoTier, the recommended tier map; OpenCode T01."""
from __future__ import annotations

import json
from pathlib import Path

from conftest import credential_guard

from tokenstretch.adapters import REGISTRY
from tokenstretch.bench import HeadlessResult, _in_scope
from tokenstretch.context import build_context
from tokenstretch.models import format_suggestion, suggest_tier, tool_picks
from tokenstretch.setup import collect_findings

COP = next(a for a in REGISTRY if a.id == "copilot-cli")
OPC = next(a for a in REGISTRY if a.id == "opencode")


def _world(tmp_path: Path, settings: dict | None = None) -> Path:
    for d in ("home", "root", "sys"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / "sys" / "env.json").write_text("{}\n")
    c = tmp_path / "home" / ".copilot"
    c.mkdir(parents=True, exist_ok=True)
    (c / "settings.json").write_text(json.dumps(settings if settings is not None else {"model": "claude-opus-5.5"}))
    (c / "config.json").write_text("CANARY_CREDENTIAL_DO_NOT_READ_COPILOT")
    return tmp_path


def _ctx(w: Path):
    return build_context(w / "home", w / "root", w / "sys")


USAGE_FILE = {
    "sessionStartTime": "2026-11-11T20:00:00.000Z", "currentModel": "claude-sonnet-5.5",
    "modelMetrics": {"claude-sonnet-5.5": {"requests": {"count": 3, "cost": 1}, "usage": {
        "inputTokens": 15000, "outputTokens": 800, "cacheReadTokens": 12000, "cacheWriteTokens": 900,
        "reasoningTokens": 200}, "totalNanoAiu": 650000000}},
    "agentMetrics": {"main": {"modelMetrics": {"claude-sonnet-5.5": {"requests": {"count": 3}, "usage": {
        "inputTokens": 15000, "outputTokens": 800, "cacheReadTokens": 12000, "cacheWriteTokens": 900,
        "reasoningTokens": 200}}}}},
}


class _UsageFileRunner:
    def __init__(self):
        self.calls = []

    def run(self, argv, *, cwd, env=None, timeout=120):
        self.calls.append(list(argv))
        Path(argv[argv.index("--usage-output-file") + 1]).write_text(json.dumps(USAGE_FILE, indent=2))
        return HeadlessResult(list(argv), str(cwd), 0, '{"type":"result","usage":{"premiumRequests":1}}\n', "")


def test_bench_reads_usage_output_file_without_double_counting_agents(tmp_path):
    w = _world(tmp_path)
    runner = _UsageFileRunner()
    out = COP.run_headless_task(_ctx(w), {"prompt": "hi", "id": "t1"},
                                opts={"bench": True, "home": str(w / "sbx"), "sandboxDir": str(w / "root"), "runner": runner})
    argv = runner.calls[0]
    assert str(w / "sbx") in argv[argv.index("--usage-output-file") + 1]
    (rec,) = out["usage"]
    # input includes cache read+write; reasoning is inside output; agentMetrics not added again.
    assert (rec.input_tokens, rec.cache_read_tokens, rec.cache_write_tokens) == (2100, 12000, 900)
    assert (rec.output_tokens, rec.reasoning_tokens) == (600, 200)
    assert rec.source == "headless" and rec.task_id == "t1" and rec.model == "claude-sonnet-5.5"


def test_sessions_scoped_by_session_start_and_workspace_yaml(tmp_path):
    w = _world(tmp_path)
    state = w / "home" / ".copilot" / "session-state"
    shutdown = {"type": "session.shutdown", "timestamp": "2026-11-11T20:30:00Z", "data": {"modelMetrics": {
        "claude-sonnet-5.5": {"requests": {"count": 1}, "usage": {"inputTokens": 100, "outputTokens": 10}}}}}
    a = state / "sess-a"
    a.mkdir(parents=True)
    (a / "events.jsonl").write_text(json.dumps({"type": "session.start", "data": {"context": {
        "cwd": "/tmp/bench-1/before/repo", "gitRoot": "/tmp/bench-1/before/repo"}}}) + "\n" + json.dumps(shutdown) + "\n")
    b = state / "sess-b"
    b.mkdir(parents=True)
    (b / "events.jsonl").write_text(json.dumps({"type": "session.start", "data": {}}) + "\n" + json.dumps(shutdown) + "\n")
    (b / "workspace.yaml").write_text('id: sess-b\ncwd: "/tmp/bench-1/after/repo"\ngit_root: /tmp/bench-1/after/repo\n'
                                      "summary: |\n  cwd: /not/this\n")
    with credential_guard():
        recs = {r.session_id: r for r in COP.read_usage_logs(_ctx(w), since_days=4000)}
    assert recs["sess-a"].extra["cwd"] == "/tmp/bench-1/before/repo"
    assert recs["sess-b"].extra["cwd"] == "/tmp/bench-1/after/repo"
    assert _in_scope(recs["sess-a"], "/tmp/bench-1/before/repo", "bench-1")
    assert not _in_scope(recs["sess-b"], "/tmp/bench-1/before/repo", "bench-1")
    assert getattr(COP, "supports_prepare", True)


def test_auto_tier_doctor_and_model_default(tmp_path):
    w = _world(tmp_path, {"model": "auto", "autoTier": "intelligence"})
    with credential_guard():
        findings = collect_findings(_ctx(w), adapters=[COP])
    w05 = [f for f in findings if f.id == "W05"]
    assert len(w05) == 1 and "intelligence" in w05[0].message
    item = COP.plan_default_model(_ctx(w), "mid")
    new = json.loads(item.changes[0]["content"])
    assert new == {"model": "auto", "autoTier": "balance"} and item.changes[0]["key"] == "autoTier"
    assert any("recommended mapping, not a GitHub list" in n for n in item.notes)
    # A plain model: the tier map (a recommendation) sets `model`.
    w2 = _world(tmp_path / "b", {"model": "claude-opus-5.5", "theme": "dark"})
    item = COP.plan_default_model(_ctx(w2), "light")
    assert json.loads(item.changes[0]["content"]) == {"model": "claude-haiku-5.5", "theme": "dark"}
    # JSONC with comments is never rewritten.
    (w2 / "home" / ".copilot" / "settings.json").write_text('// mine\n{"model": "claude-opus-5.5"}\n')
    assert COP.plan_default_model(_ctx(w2), "light") is None


def test_copilot_suggest_labels_tier_map_as_recommendation():
    picks = tool_picks(None, "top", adapters=[COP])
    text = "\n".join(format_suggestion(suggest_tier("redesign the architecture"), picks))
    assert "claude-opus-5.5 or gpt-6-astra" in text and "recommended mapping, not a GitHub list" in text
    assert COP.is_top_model("claude-fable-5.2") and COP.is_top_model("gpt-6-astra") and not COP.is_top_model("gpt-6.1-sol")


def test_opencode_gemini_oauth_plugin_is_high_tos(tmp_path):
    for d in ("home", "root", "sys"):
        (tmp_path / d).mkdir()
    (tmp_path / "sys" / "env.json").write_text("{}\n")
    cfg = tmp_path / "home" / ".config" / "opencode"
    cfg.mkdir(parents=True)
    (cfg / "opencode.jsonc").write_text('{\n  // plugins\n  "plugin": ["opencode-gemini-auth@1.4.0", "opencode-helicone-session"]\n}\n')
    auth = tmp_path / "home" / ".local" / "share" / "opencode"
    auth.mkdir(parents=True)
    (auth / "auth.json").write_text("CANARY_CREDENTIAL_DO_NOT_READ_OPENCODE")
    ctx = build_context(tmp_path / "home", tmp_path / "root", tmp_path / "sys")
    with credential_guard():
        t01 = [f for f in OPC.checks(ctx) if f.id == "T01"]
    assert len(t01) == 1 and t01[0].severity == "high" and "opencode-gemini-auth" in t01[0].message
    # Cline / Roo are not flagged on the name alone (unverified); a plain OpenCode config is clean.
    (cfg / "opencode.jsonc").write_text('{"plugin": ["opencode-helicone-session"], "provider": {"cline": {}}}\n')
    assert not [f for f in OPC.checks(ctx) if f.id == "T01"]
