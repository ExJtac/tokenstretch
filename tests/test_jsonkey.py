"""`jsonkey`: one key out of a settings file, and nothing else from it."""
import json

from tokenstretch import jsonkey as jk
from tokenstretch.nudge import end_hook_on
from tokenstretch.startup_plugins import _plugin, found

SECRET = "canary-value-not-a-real-key"


def _settings(home, **extra):
    d = home / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    (d / "settings.json").write_text(json.dumps({"env": {"SOME_API_KEY": SECRET}, **extra}), encoding="utf-8")
    return d / "settings.json"


def test_read_key_returns_only_the_key_asked_for(tmp_path):
    path = _settings(tmp_path, enabledPlugins={"a@b": True}, hooks={"Stop": [{"hooks": [{"command": "x y"}]}]})
    assert jk.read_key(path, "enabledPlugins") == {"a@b": True}
    assert jk.read_key(path, "hooks", "Stop", "nope") is None and jk.read_key(path, "missing") is None
    assert jk.read_key(tmp_path / "none.json", "a") is None
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    assert jk.read_key(tmp_path / "bad.json", "a") is None
    (tmp_path / "list.json").write_text("[1, 2]", encoding="utf-8")
    assert jk.read_key(tmp_path / "list.json", "a") is None and jk.count_key(tmp_path / "list.json", "a") == 0
    assert jk.commands_at(path, "hooks", "Stop") == ["x y"] and jk.commands_at(path, "env") == []


def test_plugin_connector_count_is_a_number_and_no_entry(tmp_path):
    plug = tmp_path / "plug"
    plug.mkdir()
    (plug / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "mail": {"command": "m", "env": {"KEY": SECRET}}, "crm": {"url": "https://x", "headers": {"Authorization": SECRET}}}}),
        encoding="utf-8")
    got = _plugin(plug)
    assert got["connectors"] == 2 and SECRET not in json.dumps(got)
    (plug / ".mcp.json").write_text(json.dumps({"mcpServers": ["odd"]}), encoding="utf-8")
    assert _plugin(plug)["connectors"] == 0


def test_settings_reads_keep_nothing_from_env(tmp_path):
    _settings(tmp_path, enabledPlugins={"viewer@market": True},
              hooks={"Stop": [{"hooks": [{"type": "command", "command": "/x/bin/tokenstretch nudge"}]}]})
    plug = tmp_path / "viewer"
    (plug / "skills" / "open").mkdir(parents=True)
    (plug / "skills" / "open" / "SKILL.md").write_text("---\nname: open\ndescription: Open a file.\n---\n", encoding="utf-8")
    reg = tmp_path / ".claude" / "plugins"
    reg.mkdir(parents=True)
    (reg / "installed_plugins.json").write_text(json.dumps({"plugins": {"viewer@market": [{"installPath": str(plug)}]}}),
                                                encoding="utf-8")
    rows = found(tmp_path)
    assert [r["name"] for r in rows] == ["viewer"] and SECRET not in json.dumps(rows, default=str)
    assert end_hook_on(tmp_path) is True
    _settings(tmp_path, hooks={"Stop": "not a list"})
    assert end_hook_on(tmp_path) is False
