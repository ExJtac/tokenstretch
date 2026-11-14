"""`doctor --new` and the watch checkup pass: only findings that were not there at the last check."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.conftest import CANARIES, copy_fixture, credential_guard
from tokenstretch import checkup, cli
from tokenstretch.context import build_context
from tokenstretch.findings import Finding
from tokenstretch.watch import Watcher, run_watch


def f(fid="W01", source="/x/CLAUDE.md", severity="med", message="12 lines", adapter="claude-code") -> Finding:
    return Finding(id=fid, severity=severity, message=message, fix="trim it", source=source, adapter_id=adapter)


def test_key_ignores_the_message_but_not_the_place_or_severity():
    assert checkup.finding_key(f(message="12 lines")) == checkup.finding_key(f(message="900 lines"))
    assert checkup.finding_key(f()) != checkup.finding_key(f(source="/y/CLAUDE.md"))
    assert checkup.finding_key(f()) != checkup.finding_key(f(severity="high"))
    assert checkup.finding_key(f()) != checkup.finding_key(f(adapter="codex"))


def test_first_check_records_a_starting_point_and_reports_nothing(tmp_path):
    fresh, first = checkup.new_since_last(tmp_path, [f(), f("W02")], now=100)
    assert fresh == [] and first is True
    saved = json.loads(checkup.seen_file(tmp_path).read_text())
    assert saved["checked_at"] == 100 and len(saved["seen"]) == 2


def test_only_new_findings_are_reported_once(tmp_path):
    checkup.new_since_last(tmp_path, [f()], now=100)
    fresh, first = checkup.new_since_last(tmp_path, [f(), f("W09", severity="high")], now=200)
    assert [x.id for x in fresh] == ["W09"] and first is False
    assert checkup.new_since_last(tmp_path, [f(), f("W09", severity="high")], now=300) == ([], False)
    saved = json.loads(checkup.seen_file(tmp_path).read_text())["seen"]
    assert sorted(saved.values()) == [100, 200]        # first-seen times are kept


def test_a_finding_that_went_away_and_came_back_is_new_again(tmp_path):
    checkup.new_since_last(tmp_path, [f(), f("W02")], now=1)
    checkup.new_since_last(tmp_path, [f()], now=2)
    fresh, _ = checkup.new_since_last(tmp_path, [f(), f("W02")], now=3)
    assert [x.id for x in fresh] == ["W02"]


def test_record_false_leaves_the_state_alone(tmp_path):
    checkup.new_since_last(tmp_path, [f()], now=1)
    before = checkup.seen_file(tmp_path).read_text()
    fresh, _ = checkup.new_since_last(tmp_path, [f(), f("W02")], now=2, record=False)
    assert len(fresh) == 1 and checkup.seen_file(tmp_path).read_text() == before


def test_a_damaged_state_file_counts_as_a_first_check(tmp_path):
    checkup.seen_file(tmp_path).parent.mkdir(parents=True)
    checkup.seen_file(tmp_path).write_text("{not json")
    assert checkup.new_since_last(tmp_path, [f()], now=1) == ([], True)


def test_state_file_holds_hashes_only(tmp_path):
    checkup.new_since_last(tmp_path, [f(source="/secret/place/CLAUDE.md", message="private words")], now=1)
    text = checkup.seen_file(tmp_path).read_text()
    assert "secret" not in text and "private" not in text and "W01" not in text


def test_notice_is_one_short_line():
    line = checkup.notice([f("W09", severity="high", message="x" * 500), f()])
    assert line.startswith("HIGH W09: ") and line.endswith("(+1 more)") and len(line) < 200


def _doctor(dest: Path, *extra: str) -> tuple[int, str, str]:
    import io
    from contextlib import redirect_stderr, redirect_stdout
    out, err = io.StringIO(), io.StringIO()
    argv = ["doctor", "--home", str(dest / "home"), "--root", str(dest / "root"),
            "--sys-snapshot", str(dest / "sys"), *extra]
    with credential_guard(), redirect_stdout(out), redirect_stderr(err):
        code = cli.main(argv)
    for canary in CANARIES:
        assert canary not in out.getvalue() and canary not in err.getvalue()
    return code, out.getvalue(), err.getvalue()


def _forget_one(home: Path) -> None:
    path = checkup.seen_file(home)
    data = json.loads(path.read_text())
    data["seen"].pop(sorted(data["seen"])[0])
    path.write_text(json.dumps(data))


def test_doctor_new_end_to_end(tmp_path, monkeypatch):
    dest = copy_fixture("bloated", tmp_path)
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr("tokenstretch.watch._notify", lambda title, message: shown.append((title, message)))

    code, out, _ = _doctor(dest, "--new", "--notify")
    assert code == 0 and out.startswith("First check: ") and shown == []

    code, out, _ = _doctor(dest, "--new", "--notify")
    assert code == 0 and out.startswith("No new findings since the last check") and shown == []

    _forget_one(dest / "home")
    code, out, _ = _doctor(dest, "--new", "--notify")
    assert out.startswith("1 new finding(s) since the last check:") and "  fix: " in out
    assert len(shown) == 1 and "1 new finding(s)" in shown[0][0]

    _forget_one(dest / "home")
    code, out, _ = _doctor(dest, "--new", "--json")
    assert len(json.loads(out)) == 1

    code, out, _ = _doctor(dest, "--json")              # the plain checkup is unchanged and leaves the state alone
    assert len(json.loads(out)) > 5


def test_doctor_new_refuses_privacy_only(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    code, out, err = _doctor(dest, "--new", "--privacy")
    assert code == 2 and "--privacy" in err and not checkup.seen_file(dest / "home").exists()


def _ctx(dest: Path):
    return build_context(dest / "home", dest / "root", dest / "sys", None)


def test_watch_checkup_shows_new_findings_once(tmp_path):
    dest = copy_fixture("bloated", tmp_path)
    clock = [1000.0]
    said: list[tuple[str, str]] = []
    w = Watcher(_ctx(dest), adapters=[], now=lambda: clock[0], notifier=lambda t, m: said.append((t, m)))
    assert w.checkup_scan() == []                       # off unless asked for
    w.checkup_every_s = 1800
    assert w.checkup_scan() == []                       # first pass: starting point only
    _forget_one(dest / "home")
    assert w.checkup_scan() == []                       # not due yet
    clock[0] += 1801
    warnings = w.scan()
    assert len(warnings) == 1 and warnings[0].key[0] == "checkup"
    assert warnings[0].what.startswith("new finding (") and warnings[0].fix
    assert len(said) == 1 and said[0][0].endswith("new finding")
    clock[0] += 1801
    assert w.scan() == []


def test_watch_survives_a_failing_checkup(tmp_path, monkeypatch):
    dest = copy_fixture("clean", tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("broken check")
    monkeypatch.setattr(checkup, "collect", boom)
    w = Watcher(_ctx(dest), adapters=[])
    w.checkup_every_s = 60
    assert w.scan() == []


def test_run_watch_checkup_is_off_by_default_and_on_from_the_cli(tmp_path):
    dest = copy_fixture("clean", tmp_path)
    run_watch(_ctx(dest), once=True, out=lambda _l: None, adapters=[])
    assert not checkup.seen_file(dest / "home").exists()
    assert cli.main(["watch", "--once", "--home", str(dest / "home"), "--root", str(dest / "root"),
                     "--sys-snapshot", str(dest / "sys")]) == 0
    assert checkup.seen_file(dest / "home").exists()
    assert cli.main(["watch", "--once", "--checkup", "0", "--home", str(dest / "home"), "--root", str(dest / "root"),
                     "--sys-snapshot", str(dest / "sys")]) == 0


@pytest.mark.parametrize("name", ["bloated", "clean"])
def test_collect_matches_the_plain_doctor_list(tmp_path, name):
    dest = copy_fixture(name, tmp_path)
    _, out, _ = _doctor(dest, "--json")
    with credential_guard():
        got = checkup.collect(_ctx(dest))
    assert [x["id"] for x in json.loads(out)] == [x.id for x in got]
