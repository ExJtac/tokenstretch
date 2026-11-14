"""Undo of a settings file that changed after setup: only this tool's entries go, later edits stay."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from test_setup import SETTINGS, _run, _world

from tokenstretch.cli import main
from tokenstretch.installer import undo_manifest
from tokenstretch.undo_keys import OWN_COMMAND, revert_own_entries

MINE = {"type": "command", "command": "~/bin/my-hook.sh"}


def _undo(w: Path, *extra: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(["undo", "--home", str(w / "home"), "--root", str(w / "root"), *extra])
    return code, out.getvalue(), err.getvalue()


def _edit_after_setup(path: Path) -> dict:
    """What a user does after setup: a hook of their own, a new key, and a different model."""
    s = json.loads(path.read_text())
    s["hooks"]["UserPromptSubmit"].append({"hooks": [MINE]})          # a group of their own
    s["hooks"]["Stop"][0]["hooks"].append(MINE)                       # added to the group setup made
    s["permissions"] = {"allow": ["Bash(ls:*)"]}
    s["env"]["MY_FLAG"] = "1"
    s["model"] = "haiku"                                              # changed again after setup
    path.write_text(json.dumps(s, indent=2) + "\n", encoding="utf-8")
    return s


def test_undo_of_changed_settings_removes_only_own_entries(tmp_path):
    w = _world(tmp_path)
    path = w / "home" / ".claude" / "settings.json"
    _run(w, yes=True, installs=False)
    _edit_after_setup(path)
    code, out, err = _undo(w)
    assert code == 0 and err == ""
    assert "changed since setup, so it was not replaced" in out and "your later edits were kept" in out
    s = json.loads(path.read_text())
    assert s == {
        "model": "haiku",                                             # the user's later choice stays
        "theme": SETTINGS["theme"],
        "hooks": {"UserPromptSubmit": [{"hooks": [MINE]}], "Stop": [{"hooks": [MINE]}]},
        "permissions": {"allow": ["Bash(ls:*)"]},
        "env": {"MY_FLAG": "1"},                                      # setup's own env key is gone
    }
    assert "tokenstretch" not in path.read_text()
    # A second undo finds nothing of ours and changes nothing.
    before = path.read_bytes()
    assert _undo(w)[0] == 0 and path.read_bytes() == before


def test_a_key_still_as_setup_left_it_goes_back_to_the_old_value(tmp_path):
    w = _world(tmp_path)
    path = w / "home" / ".claude" / "settings.json"
    _run(w, yes=True, installs=False)
    s = json.loads(path.read_text())
    assert s["model"] == "sonnet" != SETTINGS["model"]
    s["theme"] = "light"                                              # one edit, elsewhere
    path.write_text(json.dumps(s), encoding="utf-8")
    assert _undo(w)[0] == 0
    assert json.loads(path.read_text()) == {**SETTINGS, "theme": "light"}


def test_no_change_since_setup_still_restores_the_whole_file(tmp_path):
    w = _world(tmp_path)
    path = w / "home" / ".claude" / "settings.json"
    original = path.read_bytes()
    _run(w, yes=True, installs=False)
    code, out, _ = _undo(w)
    assert code == 0 and "changed since setup" not in out and path.read_bytes() == original


def test_force_still_restores_the_whole_file_and_drops_later_edits(tmp_path):
    w = _world(tmp_path)
    path = w / "home" / ".claude" / "settings.json"
    original = path.read_bytes()
    _run(w, yes=True, installs=False)
    _edit_after_setup(path)
    assert _undo(w, "--force")[0] == 0 and path.read_bytes() == original


def test_older_manifest_without_setups_copy_matches_by_command_string(tmp_path):
    w = _world(tmp_path)
    path = w / "home" / ".claude" / "settings.json"
    res, _ = _run(w, yes=True, installs=False)
    for m in res.manifests:                                           # as a version before the copy was kept
        data = json.loads(Path(m).read_text())
        for rec in data["files"]:
            assert rec["path"].endswith(".json") == bool(rec.get("after"))
            rec.pop("after", None)
        Path(m).write_text(json.dumps(data), encoding="utf-8")
    _edit_after_setup(path)
    assert _undo(w)[0] == 0
    s = json.loads(path.read_text())
    assert "tokenstretch" not in json.dumps(s["hooks"]) and "statusLine" not in s
    assert s["hooks"] == {"UserPromptSubmit": [{"hooks": [MINE]}], "Stop": [{"hooks": [MINE]}]}
    assert s["permissions"] == {"allow": ["Bash(ls:*)"]} and s["env"]["MY_FLAG"] == "1"
    # Without the copy, a plain key cannot be told from the user's own, so it is left alone.
    assert s["env"]["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "200000"


def test_setups_copy_is_private_and_matches_what_was_written(tmp_path):
    w = _world(tmp_path)
    res, _ = _run(w, yes=True, installs=False)
    seen = 0
    for m in res.manifests:
        data = json.loads(Path(m).read_text())
        for rec in data["files"]:
            if rec.get("after"):
                copy = Path(data["backup_dir"]) / rec["after"]
                assert __import__("hashlib").sha256(copy.read_bytes()).hexdigest() == rec["sha256_after"]
                if __import__("os").name == "posix":
                    assert copy.stat().st_mode & 0o777 == 0o600
                seen += 1
    assert seen


def test_changed_file_that_is_not_json_still_refuses(tmp_path):
    w = _world(tmp_path)
    path = w / "home" / ".claude" / "settings.json"
    _run(w, yes=True, installs=False)
    path.write_text("{ not json", encoding="utf-8")
    code, _, err = _undo(w)
    assert code == 2 and "changed since the install" in err and path.read_text() == "{ not json"   # nothing written


def test_wrapped_status_line_goes_back_to_the_users_own():
    theirs = {"type": "command", "command": "~/bin/line.sh"}
    before = {"statusLine": theirs, "hooks": {"Stop": [{"hooks": [MINE]}]}}
    ours = {"type": "command", "command": "tokenstretch nudge 2>/dev/null || true", "timeout": 5}
    after = {"statusLine": {"type": "command", "command": "tokenstretch statusline --wrap '~/bin/line.sh'"},
             "hooks": {"Stop": [{"hooks": [MINE]}, {"hooks": [ours]}]}}
    now = {**after, "extra": True}
    for known in (after, None):
        assert revert_own_entries(now, before, known) == {**before, "extra": True}


def test_own_entries_from_an_earlier_run_are_not_this_runs_to_remove():
    ours = {"type": "command", "command": "/opt/bin/tokenstretch auto 2>/dev/null || true"}
    before = {"hooks": {"SessionStart": [{"hooks": [ours]}]}}
    now = {"hooks": {"SessionStart": [{"hooks": [ours]}]}, "x": 1}
    assert revert_own_entries(now, before, None) == now


def test_own_command_pattern():
    for cmd in ("tokenstretch nudge 2>/dev/null || true", "/Users/a/.local/bin/tokenstretch auto",
                "PYTHONPATH=/x /usr/bin/python3 -m tokenstretch statusline --wrap 'a'",
                "'/a b/tokenstretch' nudge"):
        assert OWN_COMMAND.search(cmd), cmd
    for cmd in ("~/bin/my-hook.sh", "echo tokenstretch", "cat ~/.tokenstretch/x.json", "mytokenstretch run",
                "tokenstretch-advisor go"):
        assert not OWN_COMMAND.search(cmd), cmd


def test_file_setup_created_is_deleted_only_when_nothing_else_is_in_it(tmp_path):
    for keep in (False, True):
        w = _world(tmp_path / str(keep))
        path = w / "home" / ".claude" / "settings.json"
        path.unlink()                                                 # setup creates the file
        res, _ = _run(w, yes=True, installs=False)
        s = json.loads(path.read_text())
        s["hooks"]["Stop"].append({"hooks": [MINE]}) if keep else s.pop("statusLine")
        path.write_text(json.dumps(s), encoding="utf-8")
        assert _undo(w)[0] == 0
        if keep:
            assert json.loads(path.read_text()) == {"hooks": {"Stop": [{"hooks": [MINE]}]}}
        else:
            assert not path.exists()
