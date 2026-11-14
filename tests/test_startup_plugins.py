"""Plugins and app-added skills in the start-up report: sizes, use counts, and honest "no use" claims."""
import json
import os

from tokenstretch import startup_plugins as sp

NOW = 1_800_000_000.0


def _skill(d, name, desc="does a thing", extra=""):
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n{extra}---\nbody\n")


def _home(tmp_path):
    c = tmp_path / ".claude"
    term = tmp_path / "cache" / "tool" / "1.0"
    _skill(term / "skills" / "alpha", "alpha")
    _skill(term / "skills" / "manual", "manual", extra="disable-model-invocation: true\n")
    off = tmp_path / "cache" / "off" / "1.0"
    _skill(off / "skills" / "zeta", "zeta")
    hooked = tmp_path / "cache" / "hooked" / "1.0"
    _skill(hooked / "skills" / "eta", "eta")
    (hooked / "hooks").mkdir()
    (c / "plugins").mkdir(parents=True)
    (c / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": {
        "tool@market": [{"installPath": str(term)}],
        "off@market": [{"installPath": str(off)}],
        "hooked@market": [{"installPath": str(hooked)}],
        "gone@market": [{"installPath": str(tmp_path / "missing")}]}}))
    (c / "settings.json").write_text(json.dumps({"enabledPlugins": {
        "tool@market": True, "off@market": False, "hooked@market": True, "gone@market": True}}))
    app = c / "plugins" / "synced" / "acct" / "viewer"
    (app / ".claude-plugin").mkdir(parents=True)
    _skill(app / "skills" / "open", "open", desc="x" * 400)
    (app / ".mcp.json").write_text(json.dumps({"mcpServers": {"one": {}, "two": {}}}))
    _skill(c / "skills" / "synced" / "acct" / "sheet", "sheet")
    _skill(c / "skills" / "synced" / "acct" / "slides", "slides")
    return tmp_path


def _log(home, lines, name="s.jsonl", age_days=1):
    d = home / ".claude" / "projects" / "p"
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    t = NOW - age_days * 86400
    os.utime(f, (t, t))
    return f


def _call(name, **inp):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}}


def test_found_lists_enabled_plugins_and_app_skills(tmp_path):
    got = {(i["kind"], i["name"], i["where"]): i for i in sp.found(_home(tmp_path))}
    assert set(got) == {("plugin", "tool", "terminal"), ("plugin", "hooked", "terminal"), ("plugin", "viewer", "app"),
                        ("skill", "sheet", "app"), ("skill", "slides", "app")}   # off and missing ones are left out
    assert got[("plugin", "tool", "terminal")]["skills"] == ["alpha"]            # manual-only skill is not listed
    assert got[("plugin", "viewer", "app")]["connectors"] == 2
    assert got[("plugin", "viewer", "app")]["bytes"] == 404
    assert got[("plugin", "hooked", "terminal")]["hooks"] is True


def test_use_is_counted_from_logs(tmp_path):
    home = _home(tmp_path)
    _log(home, [_call("Skill", skill="tool:alpha"), _call("Skill", skill="anthropic-skills:sheet"),
                _call("mcp__plugin_viewer_one__show", path="x"),
                {"type": "user", "message": {"content": "<command-name>/tool:alpha</command-name>"}}])
    _log(home, [_call("Skill", skill="slides")], name="old.jsonl", age_days=30)   # too old to count
    parts = {p["name"]: p for p in sp.parts(home, NOW)}
    assert parts["tool"]["calls"] == 2 and "used 2 times" in parts["tool"]["kind"]
    assert parts["viewer"]["calls"] == 1 and "2 connectors" in parts["viewer"]["kind"]
    assert parts["sheet"]["calls"] == 1 and not parts["sheet"]["unused"]
    assert parts["slides"]["unused"] and "no use found" in parts["slides"]["kind"]
    assert parts["viewer"]["tokens"] == 101
    assert '"tool@market": false' in parts["tool"]["fix"]
    assert "desktop app" in parts["viewer"]["fix"] and "desktop app" in parts["slides"]["fix"]


def test_helper_started_from_a_plugin_counts(tmp_path):
    home = _home(tmp_path)
    _log(home, [_call("Agent", subagent_type="tool:builder", prompt="x")])
    assert {p["name"]: p for p in sp.parts(home, NOW)}["tool"]["calls"] == 1


def test_plugin_with_hooks_is_never_called_unused(tmp_path):
    home = _home(tmp_path)
    _log(home, [_call("Read", file_path="x")])
    p = {p["name"]: p for p in sp.parts(home, NOW)}["hooked"]
    assert not p["unused"] and "hooks" in p["kind"] and p["fix"].startswith("check whether you use it")


def test_incomplete_log_read_claims_nothing(tmp_path):
    home = _home(tmp_path)
    _log(home, [_call("Skill", skill="sheet")] * 50)
    use = sp.used(home, NOW, max_bytes=10)
    assert use["complete"] is False
    parts = sp.parts(home, NOW, use)
    assert not any(p["unused"] for p in parts)
    assert all("use not counted" in p["kind"] or "hooks" in p["kind"] for p in parts)


def test_nothing_installed_gives_no_parts(tmp_path):
    assert sp.parts(tmp_path, NOW) == []


def test_startup_report_names_the_unused(tmp_path):
    from tokenstretch.startup import format_report
    d = {"adapter": "claude-code", "name": "Claude Code", "measured_start": None, "recent": [], "known": 100,
         "rest": None, "price": None, "unsized": [], "changes": [],
         "parts": [{"kind": "skill added by the desktop app", "name": "slides", "tokens": 100, "fix": "f", "unused": True}],
         "unused": {"names": ["slides"], "tokens": 100}}
    text = "\n".join(format_report([d]))
    assert "No use found in the last 14 days: slides." in text and "are not counted" in text


def test_app_plugin_fix_gives_the_app_steps(tmp_path):
    home = _home(tmp_path)
    _log(home, [_call("Read", file_path="x")])
    fix = {p["name"]: p for p in sp.parts(home, NOW)}["viewer"]["fix"]
    assert "Settings > Customize > Plugins" in fix and '"Yours"' in fix and "can change" in fix


def test_app_skill_fix_gives_the_help_page_steps(tmp_path):
    home = _home(tmp_path)
    _log(home, [_call("Read", file_path="x")])
    got = {p["name"]: p for p in sp.parts(home, NOW)}
    assert "Customize > Skills" in got["sheet"]["fix"] and "help page" in got["sheet"]["fix"]
    assert got["sheet"]["item"] is None and got["viewer"]["item"] == "plugin:viewer"


def test_dashboard_lists_the_unused_with_their_steps(tmp_path, monkeypatch):
    from tokenstretch import dashboard_panels as dp, startup
    row = {"name": "Claude Code", "measured_start": None, "known": 100, "rest": None, "since_last": None, "price": None,
           "parts": [{"kind": "plugin (app)", "name": "viewer", "tokens": 100, "fix": sp.APP_PLUGIN_STEPS}],
           "unused": {"names": ["viewer"], "tokens": 100,
                      "items": [{"name": "viewer", "tokens": 100, "fix": sp.APP_PLUGIN_STEPS}]}}
    monkeypatch.setattr(startup, "build", lambda *a, **k: [row])
    got = dp.build_startup(None, None, lambda t: t)
    assert got["unused_days"] == sp.USE_DAYS
    assert got["tools"][0]["unused"] == [{"name": "viewer", "tokens": 100, "fix": sp.APP_PLUGIN_STEPS}]
    assert "t.unused" in dp.PANEL_JS and "How to switch it off" in dp.PANEL_JS
