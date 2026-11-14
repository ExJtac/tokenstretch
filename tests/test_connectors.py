"""`connectors`: calls per connector from the logs, and the off/on switch for the user file."""
import json
import os
from pathlib import Path

from tokenstretch import cli
from tokenstretch import connectors as cn
from tokenstretch.auto import session_start

NOW = 1_800_000_000.0


def _home(tmp_path, sessions=6):
    (tmp_path / ".claude.json").write_text(json.dumps({
        "theme": "dark",
        "mcpServers": {"mail": {"command": "mail-server", "env": {"KEY": "canary-value"}}, "crm": {"url": "https://x"},
                       "my tool": {"command": "t"}},
        "projects": {"/work/app": {"mcpServers": {"local-db": {"command": "db"}}}}}))
    root = tmp_path / "proj"
    root.mkdir()
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": {"shared": {"command": "s"}}}))
    plug = tmp_path / ".claude" / "plugins" / "synced" / "acct" / "viewer"
    (plug / ".claude-plugin").mkdir(parents=True)
    (plug / ".mcp.json").write_text(json.dumps({"mcpServers": {"pdf": {}}}))
    d = tmp_path / ".claude" / "projects" / "p"
    d.mkdir(parents=True)
    for i in range(sessions):
        calls = [("mcp__mail__send", "2026-01-0%d" % (i + 1))] if i < 2 else []
        if i == 0:
            calls += [("mcp__my_tool__run", "2026-01-09"), ("mcp__plugin_viewer_pdf__open", "2026-01-03"),
                      ("mcp__appthing__go", "2026-01-04"), ("Read", "2026-01-05")]
        f = d / f"s{i}.jsonl"
        f.write_text("".join(json.dumps({"type": "assistant", "timestamp": day + "T00:00:00Z", "message": {
            "content": [{"type": "tool_use", "name": name, "input": {}}]}}) + "\n" for name, day in calls) or "{}\n")
        os.utime(f, (NOW - 86400, NOW - 86400))
    return tmp_path, root


def _rows(tmp_path, **kw):
    home, root = _home(tmp_path, **kw)
    data = cn.build(home, root, NOW)
    return home, root, data, {r["name"]: r for r in data["rows"]}


def test_report_counts_calls_and_names_the_unused(tmp_path):
    _home_, _root, data, rows = _rows(tmp_path)
    assert rows["mail"]["calls"] == 2 and rows["mail"]["sessions"] == 2 and rows["mail"]["last"] == "2026-01-02"
    assert rows["my tool"]["calls"] == 1                       # a space in the name is `_` in the logs
    assert rows["pdf"]["calls"] == 1 and rows["pdf"]["plugin"] == "viewer"
    assert rows["appthing"]["scope"] == "app" and not rows["appthing"]["unused"]
    assert rows["crm"]["unused"] and rows["local-db"]["unused"] and rows["shared"]["unused"]
    assert data["can_switch_off"] == ["crm"]                    # only the user file is switched here
    text = "\n".join(cn.format_report(data))
    assert "You can switch off here: crm." in text and "set in a project file: local-db, shared" in text
    assert "canary-value" not in text and "canary-value" not in json.dumps(data)


def test_too_few_sessions_or_incomplete_logs_claim_nothing(tmp_path):
    _h, _r, data, rows = _rows(tmp_path, sessions=3)
    assert not any(r["unused"] for r in rows.values()) and data["can_switch_off"] == []
    assert "too little" in "\n".join(cn.format_report(data))
    home, root = tmp_path, tmp_path / "proj"
    part = cn.build(home, root, NOW, cn.used(home, NOW, max_bytes=1))
    assert part["complete"] is False and not any(r["unused"] for r in part["rows"])


def test_off_is_a_dry_run_until_apply_and_on_restores_the_entry(tmp_path):
    home, _root = _home(tmp_path)
    before = json.loads((home / ".claude.json").read_text())
    res = cn.switch(home, ["crm", "shared", "nope"], on=False)
    assert res == {"moved": ["crm"], "applied": False, "skipped": {
        "shared": "it is not in your user file (project, plugin and app connectors are not edited)",
        "nope": "it is not in your user file (project, plugin and app connectors are not edited)"}}
    assert json.loads((home / ".claude.json").read_text()) == before and not (home / cn.PARK).exists()

    assert cn.switch(home, ["crm", "mail"], on=False, apply=True)["applied"]
    now = json.loads((home / ".claude.json").read_text())
    assert list(now["mcpServers"]) == ["my tool"] and now["theme"] == "dark" and now["projects"] == before["projects"]
    assert cn.parked(home) == ["crm", "mail"]
    off_text = (home / cn.PARK).read_text()
    assert set(json.loads(off_text)) == {"off"} and sorted(json.loads(off_text)["off"]) == ["crm", "mail"]
    assert not any(word in off_text for word in ("canary-value", "KEY", "env", "mail-server", "https://x", "url"))
    assert oct((home / cn.PARK).stat().st_mode & 0o777) == "0o600" or os.name == "nt"
    assert len(list((home / ".tokenstretch" / "backups").iterdir())) == 1
    rows = {r["name"]: r for r in cn.build(home, None, NOW)["rows"]}
    assert rows["crm"]["state"] == "off" and not rows["crm"]["unused"]

    assert cn.switch(home, ["crm", "mail"], on=True, apply=True)["moved"] == ["crm", "mail"]
    after = json.loads((home / ".claude.json").read_text())
    assert after["mcpServers"] == before["mcpServers"] and cn.parked(home) == []
    assert cn.switch(home, ["crm"], on=True)["skipped"] == {"crm": "it is not switched off"}


def test_new_session_is_told_what_is_off(tmp_path):
    home, _root = _home(tmp_path)
    assert cn.off_note(home) == ""
    cn.switch(home, ["crm"], on=False, apply=True)
    reply = session_start({"source": "startup", "cwd": str(tmp_path)}, NOW, home)
    text = reply["hookSpecificOutput"]["additionalContext"]
    assert "switched off to keep sessions light: crm." in text and "connectors on NAME --apply" in text


def test_cli_off_unused_and_on(tmp_path, capsys):
    home, root = _home(tmp_path)
    for f in (home / ".claude" / "projects" / "p").iterdir():
        os.utime(f, None)                                       # the command uses the real clock
    base = ["--home", str(home), "--root", str(root)]
    assert cli.main(["connectors", *base]) == 0
    assert "crm: your user file  <- no call found" in capsys.readouterr().out
    assert cli.main(["connectors", "off", "--unused", *base]) == 0
    out = capsys.readouterr().out
    assert "Would switch off: crm" in out and "Add `--apply`" in out and not (home / cn.PARK).exists()
    assert cli.main(["connectors", "off", "--unused", "--apply", *base]) == 0
    assert "Switched off: crm" in capsys.readouterr().out and "crm" in cn.parked(home)
    assert cli.main(["connectors", "off", "--unused", *base]) == 0
    assert "Nothing to switch" in capsys.readouterr().out
    assert cli.main(["connectors", "on", "crm", "--apply", *base]) == 0 and cn.parked(home) == []
    assert cli.main(["connectors", "on", "crm", *base]) == 1
    assert cli.main(["connectors", "on", *base]) == 2


def _mode(path):
    return oct(path.stat().st_mode & 0o777)


def _undo(home):
    return cli.main(["undo", "--home", str(home), "--root", str(home)])


def test_undo_puts_a_switch_back_and_every_copy_is_owner_only(tmp_path, capsys):
    home, _root = _home(tmp_path)
    user = home / ".claude.json"
    before = user.read_bytes()
    res = cn.switch(home, ["mail"], on=False, apply=True)
    kit = home / ".tokenstretch"
    manifest = json.loads(open(res["manifest"], encoding="utf-8").read())
    assert manifest["profile"] == "connectors" and [Path(r["path"]).name for r in manifest["files"]] == [
        "connectors-off.json", ".claude.json"]                  # the off file is written first
    assert "canary-value" not in json.dumps(manifest) and "mail" not in json.dumps(manifest)
    copies = [p for p in (kit / "backups").rglob("*") if p.is_file()]
    assert len(copies) == 3                                     # the user file before, and both files after
    if os.name != "nt":
        assert {_mode(p) for p in [*copies, user, home / cn.PARK]} == {"0o600"}
    assert not list(home.rglob("*.tmp"))

    assert _undo(home) == 0
    out = capsys.readouterr().out
    assert "canary-value" not in out
    assert user.read_bytes() == before and not (home / cn.PARK).exists()
    assert os.name == "nt" or _mode(user) == "0o600"


def test_undo_after_the_user_file_changed_moves_only_the_connector_back(tmp_path, capsys):
    home, _root = _home(tmp_path)
    user = home / ".claude.json"
    before = json.loads(user.read_text())
    cn.switch(home, ["crm"], on=False, apply=True)
    cn.switch(home, ["mail"], on=False, apply=True)             # the latest change: the one undo reverses
    now = json.loads(user.read_text())
    now["theme"], now["numStartups"] = "light", 7               # the tool rewrites this file all the time
    now["mcpServers"]["added later"] = {"command": "x"}
    user.write_text(json.dumps(now))

    assert _undo(home) == 0
    out = capsys.readouterr().out
    assert "only the entries" in out and "canary-value" not in out
    after = json.loads(user.read_text())
    assert after["mcpServers"] == {"my tool": {"command": "t"}, "added later": {"command": "x"},
                                   "mail": before["mcpServers"]["mail"]}
    assert after["theme"] == "light" and after["numStartups"] == 7 and after["projects"] == before["projects"]
    assert cn.parked(home) == ["crm"]                                  # the earlier switch stands
    assert cn.switch(home, ["crm"], on=True, apply=True)["moved"] == ["crm"]
    assert json.loads(user.read_text())["mcpServers"]["crm"] == before["mcpServers"]["crm"]
    assert os.name == "nt" or _mode(user) == "0o600"


def test_undo_reverses_a_switch_on(tmp_path):
    home, _root = _home(tmp_path)
    cn.switch(home, ["crm", "mail"], on=False, apply=True)
    cn.switch(home, ["crm"], on=True, apply=True)
    assert len(list((home / ".tokenstretch" / "manifests").iterdir())) == 2   # same second, two records
    assert _undo(home) == 0
    assert sorted(cn.parked(home)) == ["crm", "mail"]
    assert list(json.loads((home / ".claude.json").read_text())["mcpServers"]) == ["my tool"]


def test_cli_names_undo_and_prints_no_entry(tmp_path, capsys):
    home, root = _home(tmp_path)
    base = ["--home", str(home), "--root", str(root)]
    assert cli.main(["connectors", "off", "mail", "--apply", *base]) == 0
    out = capsys.readouterr().out
    assert "tokenstretch undo" in out and "readable by you only" in out and "canary-value" not in out
    assert cli.main(["connectors", "off", "crm", "--apply", "--json", *base]) == 0
    assert "canary-value" not in capsys.readouterr().out


def test_off_file_never_holds_env_values_or_headers(tmp_path):
    home, _root = _home(tmp_path)
    user = home / ".claude.json"
    data = json.loads(user.read_text())
    data["mcpServers"]["api"] = {"type": "http", "url": "https://api.invalid/mcp?token=canary-in-url",
                                 "headers": {"Authorization": "Bearer canary-header"},
                                 "env": {"API_TOKEN": "canary-env"}, "args": ["--key", "canary-arg"]}
    user.write_text(json.dumps(data))
    for names in (["api"], ["mail"], ["crm"]):                  # three switches, each rewrites the off file
        assert cn.switch(home, names, on=False, apply=True)["applied"]
        off_text = (home / cn.PARK).read_text()
        for secret in ("canary", "Bearer", "Authorization", "API_TOKEN", "headers", "env", "args", "https://"):
            assert secret not in off_text
    off = json.loads((home / cn.PARK).read_text())
    assert sorted(off["off"]) == ["api", "crm", "mail"] and all(isinstance(v, str) for v in off["off"].values())
    assert "canary" not in cn.off_note(home) and "canary" not in "\n".join(cn.format_report(cn.build(home, None, NOW)))
    # each one comes back exactly, from the backup of the switch that took it out
    assert cn.switch(home, ["api", "crm", "mail"], on=True, apply=True)["moved"] == ["api", "crm", "mail"]
    assert json.loads(user.read_text())["mcpServers"] == data["mcpServers"] and cn.parked(home) == []


def test_on_refuses_when_the_backup_is_gone_and_changes_nothing(tmp_path):
    import shutil
    home, _root = _home(tmp_path)
    cn.switch(home, ["mail"], on=False, apply=True)
    shutil.rmtree(home / ".tokenstretch" / "backups")
    user, off = (home / ".claude.json").read_bytes(), (home / cn.PARK).read_bytes()
    res = cn.switch(home, ["mail"], on=True, apply=True)
    assert res["moved"] == [] and res["applied"] is False and "saved entry was not found" in res["skipped"]["mail"]
    assert (home / ".claude.json").read_bytes() == user and (home / cn.PARK).read_bytes() == off
    assert cn.parked(home) == ["mail"]                          # still listed as off: nothing is lost silently


def test_off_file_from_an_older_version_is_read_and_rewritten_with_names_only(tmp_path):
    home, _root = _home(tmp_path)
    user = home / ".claude.json"
    data = json.loads(user.read_text())
    mail, crm = data["mcpServers"].pop("mail"), data["mcpServers"]["crm"]
    user.write_text(json.dumps(data))
    (home / cn.PARK).parent.mkdir()
    (home / cn.PARK).write_text(json.dumps({"mcpServers": {"mail": mail}}))    # what 0.9.7 and earlier wrote
    assert cn.parked(home) == ["mail"] and "mail" in cn.off_note(home)
    assert cn.switch(home, ["crm"], on=False, apply=True)["applied"]            # any switch rewrites the file
    off_text = (home / cn.PARK).read_text()
    assert "canary-value" not in off_text and sorted(json.loads(off_text)["off"]) == ["crm", "mail"]
    assert cn.switch(home, ["mail", "crm"], on=True, apply=True)["moved"] == ["mail", "crm"]
    servers = json.loads(user.read_text())["mcpServers"]
    assert servers["mail"] == mail and servers["crm"] == crm and cn.parked(home) == []


def test_on_straight_from_an_older_off_file_and_undo_of_it(tmp_path):
    home, _root = _home(tmp_path)
    user = home / ".claude.json"
    data = json.loads(user.read_text())
    mail = data["mcpServers"].pop("mail")
    user.write_text(json.dumps(data))
    (home / cn.PARK).parent.mkdir()
    old = json.dumps({"mcpServers": {"mail": mail}})
    (home / cn.PARK).write_text(old)
    before = user.read_bytes()
    assert cn.switch(home, ["mail"], on=True, apply=True)["moved"] == ["mail"]
    assert json.loads(user.read_text())["mcpServers"]["mail"] == mail and "canary" not in (home / cn.PARK).read_text()
    assert _undo(home) == 0
    assert user.read_bytes() == before and (home / cn.PARK).read_text() == old


def test_a_made_up_reference_in_the_off_file_never_reads_outside_the_manifests(tmp_path):
    home, _root = _home(tmp_path)
    (home / cn.PARK).parent.mkdir()
    for ref in ("../../.claude", "/etc/passwd", "", "..", "a/b"):
        (home / cn.PARK).write_text(json.dumps({"off": {"x": ref}}))
        res = cn.switch(home, ["x"], on=True, apply=True)
        assert res["moved"] == [] and "saved entry was not found" in res["skipped"]["x"]
