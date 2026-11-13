"""Claude Code usage dedupe by (message.id, requestId), subagent files, and OpenCode legacy dupes.

Fixture numbers come from a real live bench (tests/fixtures/logs/claude-dedupe/README.md).
Totals were cross-checked against `claude -p --output-format json` modelUsage for the before run.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tokenstretch.adapters.claude_code import ClaudeCodeAdapter
from tokenstretch.adapters.host_logs import drop_legacy_json_dupes
from tokenstretch.bench import from_logs, measure_bench
from tokenstretch.context import build_context
from tokenstretch.findings import UsageRecord

ROWS = json.loads((Path(__file__).parent / "fixtures" / "logs" / "claude-dedupe" / "rows.json").read_text())
FIELDS = ("input_uncached", "cache_write", "cache_read", "output")

# Deduplicated totals measured on the real files (per side: main session + subagent).
EXPECTED = {
    "before-main": dict(input_uncached=98, cache_write=22401, cache_read=100117, output=1644),
    "before-sub": dict(input_uncached=8, cache_write=16949, cache_read=48291, output=812),
    "after-main": dict(input_uncached=130, cache_write=40846, cache_read=128797, output=2847),
    "after-sub": dict(input_uncached=8, cache_write=11378, cache_read=54009, output=973),
}
RAW_CACHE_READ = {"before-main": 295011, "before-sub": 111746, "after-main": 718911, "after-sub": 128754}


def _line(kind_key, sid, row, ts, cwd, sidechain, agent_id=None):
    idx, inp, cw, cr, out, model, block = row
    obj = {
        "parentUuid": None, "isSidechain": sidechain, "type": "assistant", "cwd": cwd,
        "sessionId": sid, "version": "2.9.0", "requestId": f"req_{kind_key}_{idx}",
        "uuid": f"{kind_key}-{idx}-{block}-{id(row)}", "timestamp": ts,
        "message": {
            "id": f"msg_{kind_key}_{idx}", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": block, "text": "x"} if block != "tool_use" else {"type": "tool_use", "name": "Read", "input": {}}],
            "usage": {"input_tokens": inp, "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr,
                      "output_tokens": out, "service_tier": "standard"},
        },
    }
    if agent_id:
        obj["agentId"] = agent_id
    return json.dumps(obj)


def _plant(home: Path, bench: Path) -> dict[str, Path]:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    out = {}
    for side in ("before", "after"):
        cwd = str(bench / side / "repo")
        proj = home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)
        sid = f"sess-{side}"
        main = proj / f"{sid}.jsonl"
        sub = proj / sid / "subagents" / f"agent-{side}1.jsonl"
        sub.parent.mkdir(parents=True)
        user = json.dumps({"type": "user", "sessionId": sid, "cwd": cwd, "timestamp": ts,
                           "message": {"role": "user", "content": "List the files"}})
        main.write_text("\n".join([user] + [_line(f"{side}main", sid, r, ts, cwd, False) for r in ROWS[f"{side}-main"]]) + "\n")
        sub.write_text("\n".join(_line(f"{side}sub", sid, r, ts, cwd, True, f"{side}1") for r in ROWS[f"{side}-sub"]) + "\n")
        out[f"{side}-main"], out[f"{side}-sub"] = main, sub
    return out


def _sum(recs: list[UsageRecord]) -> dict[str, int]:
    return dict(
        input_uncached=sum(r.input_tokens for r in recs), cache_write=sum(r.cache_write_tokens for r in recs),
        cache_read=sum(r.cache_read_tokens for r in recs), output=sum(r.output_tokens for r in recs),
    )


def test_fixture_rows_reproduce_raw_inflation():
    for key, raw in RAW_CACHE_READ.items():
        assert sum(r[3] for r in ROWS[key]) == raw
    assert (len(ROWS["before-main"]), len({r[0] for r in ROWS["before-main"]})) == (12, 4)
    assert (len(ROWS["after-main"]), len({r[0] for r in ROWS["after-main"]})) == (26, 5)


def test_parse_log_dedupes_per_message_id(tmp_path):
    files = _plant(tmp_path / "home", tmp_path / "bench")
    ad = ClaudeCodeAdapter()
    for key, path in files.items():
        recs = ad.parse_log(path)
        assert len(recs) == len({r[0] for r in ROWS[key]}), key
        assert _sum(recs) == EXPECTED[key], key
        assert all(r.is_subagent == key.endswith("sub") for r in recs)
        assert {r.session_id for r in recs} == {f"sess-{key.split('-')[0]}"}


def test_streaming_rows_keep_max_output(tmp_path):
    files = _plant(tmp_path / "home", tmp_path / "bench")
    recs = ClaudeCodeAdapter().parse_log(files["before-sub"])
    # message 1 was written as output 5, 5, then 268; first-wins would undercount.
    assert [r.output_tokens for r in recs] == [107, 268, 147, 290]


def test_read_usage_logs_includes_subagents_and_dedupes(tmp_path):
    home = tmp_path / "home"
    _plant(home, tmp_path / "bench")
    ctx = build_context(home, tmp_path / "root", tmp_path / "sys")
    recs = ClaudeCodeAdapter().read_usage_logs(ctx)
    total = _sum(recs)
    want = {f: sum(EXPECTED[k][f] for k in EXPECTED) for f in FIELDS}
    assert total == want
    assert sum(1 for r in recs if r.is_subagent) == 8


def test_cross_file_duplicate_counted_once(tmp_path):
    files = _plant(tmp_path / "home", tmp_path / "bench")
    sub = files["before-sub"]
    # Older builds also wrote sidechain rows into the main transcript. Same key, count once.
    with files["before-main"].open("a") as fh:
        fh.write(sub.read_text().splitlines()[0] + "\n")
    ctx = build_context(tmp_path / "home", tmp_path / "root", tmp_path / "sys")
    recs = [r for r in ClaudeCodeAdapter().read_usage_logs(ctx) if r.session_id == "sess-before"]
    assert _sum(recs)["cache_write"] == EXPECTED["before-main"]["cache_write"] + EXPECTED["before-sub"]["cache_write"]


def test_bench_measure_and_from_logs_use_deduped_totals(tmp_path):
    home, bench = tmp_path / "home", tmp_path / "bench"
    _plant(home, bench)
    created = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    bench.mkdir(exist_ok=True)
    (bench / "manifest.json").write_text(json.dumps({
        "id": bench.name, "created": created,
        "before_repo": str(bench / "before" / "repo"), "after_repo": str(bench / "after" / "repo"),
    }))
    ctx = build_context(home, tmp_path / "root", tmp_path / "sys")
    rep = measure_bench(ctx, bench)
    for side, got in (("before", rep.before), ("after", rep.after)):
        for f in FIELDS:
            assert got[f] == EXPECTED[f"{side}-main"][f] + EXPECTED[f"{side}-sub"][f], (side, f)
    assert rep.before["cache_read"] == 148408 and rep.after["cache_read"] == 182806

    logs = from_logs(ctx)
    claude = next(s for s in logs.adapters if s.adapter_id == "claude-code")
    assert claude.cache_read == 148408 + 182806


def test_w13_subagent_share_uses_deduped_subagent_files(tmp_path):
    home = tmp_path / "home"
    files = _plant(home, tmp_path / "bench")
    ctx = build_context(home, tmp_path / "root", tmp_path / "sys")
    # Deduped share: sub 132,428 of 429,308 tokens = 31%, under the 40% threshold. No finding.
    assert not [f for f in ClaudeCodeAdapter()._w13(ctx) if f.severity == "med"]
    # With only the subagents/ files left, W13 must see them (old glob skipped them).
    files["before-main"].unlink()
    files["after-main"].unlink()
    med = [f for f in ClaudeCodeAdapter()._w13(ctx) if f.severity == "med"]
    assert med and med[0].evidence.get("value") == 100, med


def test_opencode_legacy_json_dupe_dropped():
    db = UsageRecord(adapter_id="opencode", source="log", task_id="msg_1", input_tokens=10, extra={"path": "opencode.db"})
    legacy = UsageRecord(adapter_id="opencode", source="log", task_id="msg_1", input_tokens=10, extra={"legacy_json": True})
    other = UsageRecord(adapter_id="opencode", source="log", task_id="msg_2", input_tokens=5, extra={"legacy_json": True})
    assert drop_legacy_json_dupes([db, legacy, other, other]) == [db, other]
