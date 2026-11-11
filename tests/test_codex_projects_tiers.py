"""Codex: project labels from the rollout's own cwd, the documented model tier map, and
`model default --tool codex` (dry run, apply with backup, undo). Also: no duplicate privacy findings."""
from __future__ import annotations

import io
import json
import tomllib
from contextlib import redirect_stdout
from pathlib import Path

from conftest import CANARIES, credential_guard

from tokenstretch.adapters import REGISTRY
from tokenstretch.cli import main
from tokenstretch.context import build_context
from tokenstretch.files import set_toml_root_string
from tokenstretch.models import tool_picks
from tokenstretch.setup import collect_status, format_status
from tokenstretch.watch import _short_project, run_watch
from tokenstretch.weekly import sessions

CODEX = next(a for a in REGISTRY if a.id == "codex")


def _world(tmp_path: Path, config: str | None = 'model = "gpt-6-astra"\n') -> Path:
    for d in ("home", "root", "sys"):
        (tmp_path / d).mkdir(parents=True)
    (tmp_path / "sys" / "env.json").write_text("{}\n")
    codex = tmp_path / "home" / ".codex"
    codex.mkdir()
    (codex / "auth.json").write_text("CANARY_CREDENTIAL_DO_NOT_READ_CODEX")
    if config is not None:
        (codex / "config.toml").write_text(config, encoding="utf-8")
    return tmp_path


def _ctx(w: Path):
    return build_context(w / "home", w / "root", w / "sys")


def _rollout(w: Path, name: str, meta: dict, turn: dict, day=("2026", "11", "10")) -> Path:
    d = w / "home" / ".codex" / "sessions" / Path(*day)
    d.mkdir(parents=True, exist_ok=True)
    lines = [{"timestamp": "2026-11-10T17:00:00Z", "type": "session_meta", "payload": meta},
             {"timestamp": "2026-11-10T17:00:01Z", "type": "turn_context", "payload": turn},
             {"timestamp": "2026-11-10T17:00:02Z", "type": "event_msg", "payload": {
                 "type": "token_count", "info": {
                     "last_token_usage": {"input_tokens": 1200, "cached_input_tokens": 1000, "output_tokens": 40},
                     "total_token_usage": {"input_tokens": 1200, "cached_input_tokens": 1000, "output_tokens": 40}}}}]
    p = d / f"rollout-2026-11-10T17-00-00-{name}.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return p


def _cli(w: Path, *args: str) -> str:
    buf = io.StringIO()
    with credential_guard(), redirect_stdout(buf):
        assert main([*args, "--home", str(w / "home"), "--root", str(w / "root"),
                     "--sys-snapshot", str(w / "sys")]) == 0
    out = buf.getvalue()
    for c in CANARIES:
        assert c not in out
    return out


def test_codex_sessions_labeled_by_recorded_cwd(tmp_path):
    w = _world(tmp_path)
    home = str(w / "home")
    _rollout(w, "a", {"id": "sess-a", "cwd": f"{home}/code/shop-api"}, {"cwd": f"{home}/elsewhere", "model": "gpt-6.1-sol"})
    _rollout(w, "b", {"id": "sess-b"}, {"cwd": "/srv/build/tools", "model": "gpt-6.1-sol"})
    _rollout(w, "c", {"id": "sess-c"}, {"model": "gpt-6.1-sol"}, day=("2026", "11", "09"))
    with credential_guard():
        ws = CODEX.watch_sessions(_ctx(w), limit=10)
    by = {Path(s["path"]).stem[-1]: s["project"] for s in ws}
    assert by["a"] == "~/code/shop-api"     # session_meta wins, home shown as ~
    assert by["b"] == "/srv/build/tools"    # turn_context fallback
    assert by["c"] == "2026-11-09"          # no cwd anywhere: the date folder
    rows = sessions(_ctx(w), adapters=[CODEX])
    assert {r["project"] for r in rows} == {"~/code/shop-api", "/srv/build/tools", "2026-11-09"}
    status = "\n".join(format_status(_ctx(w), collect_status(_ctx(w))))
    assert "~/code/shop-api" in status
    lines: list[str] = []
    with credential_guard():
        run_watch(_ctx(w), once=True, out=lines.append)
    assert not any(c in "\n".join(lines) for c in CANARIES)


def test_short_project_paths_and_encoded_names():
    assert _short_project("/Users/me/code/shop-api") == "~/code/shop-api"
    assert _short_project("/home/me") == "~"
    assert _short_project("-Users-me-code-shop") == "~-code-shop"
    long = "/Users/me/" + "/".join(["segment"] * 10) + "/repo"
    short = _short_project(long)
    assert short.startswith("…") and short.endswith("/repo") and len(short) <= 41 and "/segment/repo" in short


def test_codex_tier_map_and_suggest():
    assert CODEX.model_tiers() == {"light": "gpt-6-luna", "mid": "gpt-6.1-sol", "top": "gpt-6-astra"}
    assert CODEX.is_top_model("gpt-6-astra") and not CODEX.is_top_model("gpt-6.1-sol")
    picks = {p["adapter"]: p for p in tool_picks(None, "light", adapters=[CODEX])}
    assert picks["codex"]["model"] == "gpt-6-luna" and picks["codex"]["switch"] == "/model"


def test_codex_model_default_dry_run_apply_undo(tmp_path):
    config = ('# mine\nmodel = "gpt-6-astra"\napproval_policy = "on-request"\n\n'
              '[profiles.deep]\nmodel = "gpt-6-astra"\n\n[mcp_servers.docs]\ncommand = "docs-mcp"\n')
    w = _world(tmp_path, config)
    cfg = w / "home" / ".codex" / "config.toml"
    original = cfg.read_bytes()
    out = _cli(w, "model", "default", "--tier", "mid", "--tool", "codex")
    assert "Dry run" in out and "model: gpt-6-astra -> gpt-6.1-sol" in out
    assert cfg.read_bytes() == original
    assert not (w / "home" / ".tokenstretch" / "manifests").exists()

    out = _cli(w, "model", "default", "--tier", "mid", "--tool", "codex", "--apply")
    assert "backed up" in out
    data = tomllib.loads(cfg.read_text(encoding="utf-8"))
    assert data["model"] == "gpt-6.1-sol"
    assert data["profiles"]["deep"]["model"] == "gpt-6-astra"          # table key untouched
    assert data["approval_policy"] == "on-request" and "# mine" in cfg.read_text()
    assert "already on the mid tier (gpt-6.1-sol)" in _cli(w, "model", "default", "--tier", "mid", "--tool", "codex")
    _cli(w, "undo")
    assert cfg.read_bytes() == original


def test_codex_model_default_new_key_goes_before_tables_and_bad_toml_is_left_alone(tmp_path):
    w = _world(tmp_path, '[mcp_servers.docs]\ncommand = "docs-mcp"\n')
    item = CODEX.plan_default_model(_ctx(w), "light")
    new = tomllib.loads(item.changes[0]["content"])
    assert new["model"] == "gpt-6-luna" and new["mcp_servers"] == {"docs": {"command": "docs-mcp"}}
    (w / "home" / ".codex" / "config.toml").write_text("model = = broken\n")
    assert CODEX.plan_default_model(_ctx(w), "light") is None
    w2 = _world(tmp_path / "x", None)
    item = CODEX.plan_default_model(_ctx(w2), "top")
    assert item.changes[0]["kind"] == "create" and item.changes[0]["content"] == 'model = "gpt-6-astra"\n'


def test_set_toml_root_string_never_writes_into_a_table():
    assert set_toml_root_string('a = 1\n[t]\nk = "v"', "openai_base_url", "http://127.0.0.1:1/v1") == (
        'a = 1\nopenai_base_url = "http://127.0.0.1:1/v1"\n[t]\nk = "v"')
    assert tomllib.loads(set_toml_root_string('[t]\nmodel = "x"\n', "model", "y")) == {"model": "y", "t": {"model": "x"}}


def test_no_adapter_repeats_a_privacy_finding_in_doctor(tmp_path):
    """doctor runs checks() + privacy_checks(); no adapter may return the same finding from both."""
    fixtures = Path(__file__).parent / "fixtures"
    for name in ("cursor", "aider", "opencode", "cline", "bloated", "tos-risk"):
        base = fixtures / name
        if not base.is_dir():
            continue
        home = base / "home" if (base / "home").is_dir() else base
        root = base / "root" if (base / "root").is_dir() else base
        sysd = base / "sys" if (base / "sys").is_dir() else base
        ctx = build_context(home, root, sysd)
        for ad in REGISTRY:
            with credential_guard():
                both = ad.checks(ctx) + ad.privacy_checks(ctx)
            keys = [(f.id, f.message) for f in both]
            assert len(keys) == len(set(keys)), (name, ad.id, keys)


# ---- adapter-gaps v0.4 follow-ups --------------------------------------------------------

def test_gemini_buckets_add_up_to_total():
    from tokenstretch.adapters.gemini_cli import _gemini_record
    tokens = {"input": 10_000, "cached": 8_000, "output": 300, "thoughts": 120, "tool": 450, "total": 10_870}
    r = _gemini_record("m1", {"tokens": tokens, "model": "gemini-3.8-flash"}, "s", None, Path("x.jsonl"), None, "gemini-cli")
    assert (r.input_tokens, r.cache_read_tokens, r.output_tokens, r.reasoning_tokens) == (2_450, 8_000, 300, 120)
    assert r.input_tokens + r.cache_read_tokens + r.cache_write_tokens + r.output_tokens + r.reasoning_tokens == tokens["total"]
    assert not r.unverified_semantics


def test_codex_light_alternative_terra_in_suggest():
    from tokenstretch.models import format_suggestion, suggest_tier
    picks = tool_picks(None, "light", adapters=[CODEX])
    assert picks[0]["alternatives"] == ["gpt-6.1-terra"]
    text = "\n".join(format_suggestion(suggest_tier("rename foo"), picks))
    assert "gpt-6-luna or gpt-6.1-terra" in text
    assert tool_picks(None, "mid", adapters=[CODEX])[0]["alternatives"] == []
