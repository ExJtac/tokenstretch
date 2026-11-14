"""`tokenstretch update`: every test injects the downloader and the installer runner. No network."""
from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from tokenstretch import update as up

REPO = Path(__file__).resolve().parents[1]
COMMIT = "a" * 40
INSTALLER = f'''#!/bin/sh
TS_PIN_TAG="v0.9.5"
TS_PIN_COMMIT="{COMMIT}"
TS_PIN_TARBALL_SHA256=""
'''
CHANGELOG = """# Changelog

## [Unreleased]

- Not out yet.

## [0.9.5] - 2026-11-20

- New `update` command.
- """ + "x" * 400 + """

## [0.9.4] - 2026-11-15

### Fixed

- A fix.

## [0.9.3] - 2026-11-12

- Already installed.
"""


def fetcher(pages=None, calls=None):
    pages = pages or {up.INSTALLER_URL: INSTALLER, f"{up.RAW_URL}/{COMMIT}/CHANGELOG.md": CHANGELOG}

    def get(url):
        if calls is not None:
            calls.append(url)
        if url not in pages:
            raise up.UpdateError(f"could not download {url}")
        return pages[url]
    return get


def run(tmp_path, **kw):
    lines: list[str] = []
    ran: list[list[str]] = []

    exit_code = kw.pop("_exit", 0)

    def runner(argv, **_):
        ran.append(argv)
        assert Path(argv[1]).read_text(encoding="utf-8") == INSTALLER
        return SimpleNamespace(returncode=exit_code)

    kw.setdefault("fetcher", fetcher())
    kw.setdefault("installed", "0.9.3")
    kw.setdefault("install", {"data_dir": tmp_path / "data", "bin_dir": tmp_path / "bin"})
    kw.setdefault("interactive", False)
    code = up.run_update(tmp_path, runner=runner, out=lines.append, **kw)
    return code, lines, ran


def test_parse_pins_reads_the_real_installer():
    pins = up.parse_pins((REPO / "scripts" / "install.sh").read_text(encoding="utf-8"))
    assert re.fullmatch(r"v\d+\.\d+\.\d+", pins["tag"])
    assert re.fullmatch(r"[0-9a-f]{40}", pins["commit"])


@pytest.mark.parametrize("text", ["", 'TS_PIN_TAG="main"\nTS_PIN_COMMIT="' + COMMIT + '"\nTS_PIN_TARBALL_SHA256=""',
                                  'TS_PIN_TAG="v1.0.0"\nTS_PIN_COMMIT="abc"\nTS_PIN_TARBALL_SHA256=""',
                                  '  TS_PIN_TAG="v1.0.0"; rm -rf /'])
def test_parse_pins_refuses_missing_or_malformed(text):
    with pytest.raises(up.UpdateError):
        up.parse_pins(text)


def test_version_compare_is_numeric():
    assert up.version_tuple("v0.10.0") > up.version_tuple("0.9.9")
    with pytest.raises(up.UpdateError):
        up.version_tuple("latest")


def test_changes_between_keeps_only_newer_released_sections():
    lines = up.changes_between(CHANGELOG, "0.9.3", "0.9.5")
    text = "\n".join(lines)
    assert lines[0].startswith("[0.9.5]") and "[0.9.4]" in text
    assert "New `update` command." in text and "A fix." in text
    assert "Not out yet" not in text and "Already installed" not in text
    assert max(len(line) for line in lines) <= up.CHANGE_WIDTH + 2


def test_changes_between_caps_the_list():
    log = "## [1.0.0] - x\n\n" + "\n".join(f"- item {i}" for i in range(200))
    lines = up.changes_between(log, "0.9.0", "1.0.0")
    assert len(lines) == up.MAX_CHANGE_LINES + 1 and "more lines" in lines[-1]


def test_fetch_refuses_plain_http():
    with pytest.raises(up.UpdateError):
        up.fetch("http://example.invalid/install.sh")


def test_up_to_date_does_nothing(tmp_path):
    calls: list[str] = []
    code, lines, ran = run(tmp_path, installed="0.9.5", fetcher=fetcher(calls=calls))
    assert code == 0 and ran == [] and "newest release" in lines[0]
    assert calls == [up.INSTALLER_URL]      # the changelog is not downloaded when there is nothing new
    assert json.loads(up.state_file(tmp_path).read_text())["newer"] is False


def test_newer_local_copy_is_not_downgraded(tmp_path):
    code, _, ran = run(tmp_path, installed="0.10.0", yes=True)
    assert code == 0 and ran == []


def test_check_only_reports_and_changes_nothing(tmp_path):
    code, lines, ran = run(tmp_path, check_only=True, yes=True)
    text = "\n".join(lines)
    assert code == up.EXIT_NEWER and ran == []
    assert "Installed: 0.9.3. Newest release: 0.9.5." in text and "What changed:" in text
    assert "releases/tag/v0.9.5" in text
    saved = json.loads(up.state_file(tmp_path).read_text())
    assert saved["newer"] is True and saved["latest"] == "0.9.5"


def test_non_interactive_never_installs_without_yes(tmp_path):
    code, lines, ran = run(tmp_path)
    assert code == up.EXIT_NEWER and ran == []
    assert any("tokenstretch update --yes" in line for line in lines)


@pytest.mark.parametrize("answer,installs", [("y", True), ("YES", True), ("", False), ("n", False), ("maybe", False)])
def test_interactive_asks_and_default_is_no(tmp_path, answer, installs):
    code, lines, ran = run(tmp_path, interactive=True, input_fn=lambda: answer)
    assert any("[y/N]" in line for line in lines)
    assert bool(ran) is installs
    assert code == (0 if installs else up.EXIT_NEWER)


def test_end_of_input_counts_as_no(tmp_path):
    def eof():
        raise EOFError
    code, _, ran = run(tmp_path, interactive=True, input_fn=eof)
    assert code == up.EXIT_NEWER and ran == []


def test_yes_runs_the_downloaded_installer_for_this_install(tmp_path):
    code, lines, ran = run(tmp_path, yes=True)
    assert code == 0
    argv = ran[0]
    assert argv[0] == "sh" and argv[2:] == ["--data-dir", str(tmp_path / "data"), "--bin-dir", str(tmp_path / "bin")]
    assert not Path(argv[1]).exists()       # the temporary copy is removed afterwards
    assert any("Updated to 0.9.5" in line for line in lines)
    saved = json.loads(up.state_file(tmp_path).read_text())
    assert saved == {**saved, "installed": "0.9.5", "newer": False}


def test_installer_failure_is_reported(tmp_path):
    code, lines, _ = run(tmp_path, yes=True, _exit=1)
    assert code == 1 and any("installer stopped" in line for line in lines)
    assert not any("Updated to" in line for line in lines)


def test_unmanaged_copy_is_never_changed(tmp_path):
    code, lines, ran = run(tmp_path, yes=True, install=None)
    assert code == up.EXIT_NEWER and ran == []
    assert any("not put here by the tokenstretch installer" in line for line in lines)


def test_offline_is_a_plain_error(tmp_path):
    def down(_url):
        raise up.UpdateError("curl exit 6. Are you online?")
    code, lines, ran = run(tmp_path, fetcher=down, yes=True)
    assert code == 1 and ran == [] and lines[0].startswith("Could not check for a newer release")
    assert not up.state_file(tmp_path).exists()


def test_bad_pins_stop_before_any_install(tmp_path):
    code, _, ran = run(tmp_path, yes=True, fetcher=fetcher({up.INSTALLER_URL: "echo hi\n"}))
    assert code == 1 and ran == []


def test_missing_changelog_still_offers_the_release(tmp_path):
    code, lines, ran = run(tmp_path, yes=True, fetcher=fetcher({up.INSTALLER_URL: INSTALLER}))
    assert code == 0 and len(ran) == 1 and not any("What changed" in line for line in lines)


def test_json_is_one_document_and_never_asks(tmp_path):
    def boom():
        raise AssertionError("json mode must not ask")
    code, lines, ran = run(tmp_path, as_json=True, interactive=True, input_fn=boom)
    data = json.loads("\n".join(lines))
    assert code == up.EXIT_NEWER and ran == []
    assert data["newer"] is True and data["updated"] is False and data["latest"] == "0.9.5"
    assert "_installer" not in data


def test_find_install_needs_the_installer_marker(tmp_path):
    data = tmp_path / "data"
    pkg = data / "src" / "tokenstretch"
    pkg.mkdir(parents=True)
    assert up.find_install(pkg, which=lambda _n: None) is None           # no manifest
    (data / "install-manifest.txt").write_text("something else\n")
    assert up.find_install(pkg, which=lambda _n: None) is None           # manifest without the marker
    (data / "install-manifest.txt").write_text(up.MARKER + "\ncommit:" + COMMIT + "\n")
    launcher = tmp_path / "bin" / "tokenstretch"
    launcher.parent.mkdir()
    launcher.write_text(f"#!/bin/sh\n{up.MARKER}\nexec '{data.resolve()}/venv/bin/python' -m tokenstretch \"$@\"\n")
    found = up.find_install(pkg, which=lambda _n: str(launcher))
    assert found == {"data_dir": data.resolve(), "bin_dir": launcher.parent}
    other = tmp_path / "other" / "tokenstretch"
    other.parent.mkdir()
    other.write_text("#!/bin/sh\necho not ours\n")
    assert up.find_install(pkg, which=lambda _n: str(other))["bin_dir"] is None   # a launcher for another copy


def test_a_source_checkout_is_not_a_managed_install():
    assert up.find_install(REPO / "tokenstretch", which=lambda _n: None) is None


def test_only_the_update_command_can_reach_the_downloader():
    """Nothing but cli.cmd_update may import the update module, so no hook or check can fetch by accident."""
    users = []
    for path in (REPO / "tokenstretch").rglob("*.py"):
        if path.name == "update.py":
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"from \.update import|from \. import .*\bupdate\b|import tokenstretch\.update", text):
            users.append(path.name)
    assert users == ["cli.py"]


def test_cli_wires_the_command(tmp_path, monkeypatch, capsys):
    from tokenstretch import cli

    seen = {}

    def fake(home, **kw):
        seen.update(kw, home=home)
        return 0
    monkeypatch.setattr(up, "run_update", fake)
    assert cli.main(["update", "--check", "--json", "--home", str(tmp_path)]) == 0
    assert seen == {"home": tmp_path, "yes": False, "check_only": True, "as_json": True}
