"""Compact mode: off by default, on/off/undo round trips for each adapter, one block only,
merged into the credit-rules block when there is one, and small enough to pay for on every step."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stderr, redirect_stdout

import pytest
from conftest import credential_guard
from test_gemini_copilot import _copilot_home, _gemini_home
from test_setup import _ctx, _snapshot, _world

from tokenstretch import compact as cm
from tokenstretch.adapters import REGISTRY
from tokenstretch.adapters.base import (
    COMPACT_END,
    COMPACT_START,
    RULES_END,
    RULES_START,
    add_compact_block,
    add_rules_block,
    compact_block,
    remove_compact_block,
    rules_block,
)
from tokenstretch.cli import main
from tokenstretch.installer import apply_changes
from tokenstretch.trim import approx_tokens

BY_ID = {a.id: a for a in REGISTRY}
RULES = rules_block("Sonnet", "/clear")
# adapter id -> its project instruction file, relative to the project root
FILES = {"claude-code": "CLAUDE.md", "codex": "AGENTS.md", "gemini-cli": "GEMINI.md",
         "copilot-cli": ".github/copilot-instructions.md"}


def _world_for(tmp_path, adapter_id: str, text: str):
    w = _world(tmp_path)
    (w / "root" / "CLAUDE.md").unlink()
    _gemini_home(w)
    _copilot_home(w)
    path = w / "root" / FILES[adapter_id]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return w, path


def _cli(w, *words):
    out, err = io.StringIO(), io.StringIO()
    with credential_guard(), redirect_stdout(out), redirect_stderr(err):
        code = main([*words, "--home", str(w / "home"), "--root", str(w / "root"), "--sys-snapshot", str(w / "sys")])
    return code, out.getvalue(), err.getvalue()


# ---- the block itself ---------------------------------------------------------------------------

def test_block_is_under_the_token_budget_and_says_all_seven_rules():
    full, merged = compact_block(), compact_block(merged=True)
    assert approx_tokens(full) < 200 and approx_tokens(merged) < 200
    assert full.count("\n- ") == 7 and merged.count("\n- ") == 6
    for word in ("targeted edits", "auth.py:40-60", "restate", "never fewer facts", "one line",
                 "goal / done / files / next / open questions"):
        assert word in full and word in merged
    assert "Search first" in full and "Search first" not in merged   # the rules block already says it
    assert "compact off" in full


@pytest.mark.parametrize("existing", ["", "# Project\n", "# Project", "# Project\n\n", f"# P\n\n{RULES}",
                                      f"# P\n\n{RULES}\n## More\ntext\n"])
def test_add_then_remove_and_never_twice(existing):
    on = add_compact_block(existing)
    assert on is not None and on.count(COMPACT_START) == 1 and on.count(COMPACT_END) == 1
    assert add_compact_block(on) is None
    off = remove_compact_block(on)
    assert COMPACT_START not in off and off.strip() == existing.strip()
    if existing.endswith("\n") and not existing.endswith("\n\n"):
        assert off == existing                                       # byte for byte
    assert remove_compact_block(off) is None


def test_merges_into_the_rules_block_instead_of_a_second_block():
    text = f"# P\n\n{RULES}"
    on = add_compact_block(text)
    assert on.count(RULES_START) == 1 and on.count("## ") == 1      # one block, one heading
    assert on.index(RULES_START) < on.index(COMPACT_START) < on.index(COMPACT_END) < on.index(RULES_END)
    assert on.count("Read narrowly") == 1 and "Search first" not in on
    assert remove_compact_block(on) == text
    # rules added after compact mode: still one compact block
    later = add_rules_block(add_compact_block("# P\n"), RULES)
    assert later.count(COMPACT_START) == 1 and add_compact_block(later) is None


# ---- each adapter: on / off / undo ---------------------------------------------------------------

@pytest.mark.parametrize("adapter_id", sorted(FILES))
@pytest.mark.parametrize("text", ["# Project\n", f"# Project\n\n{RULES}"])
def test_adapter_round_trip(tmp_path, adapter_id, text):
    w, path = _world_for(tmp_path, adapter_id, text)
    ad, ctx = BY_ID[adapter_id], _ctx(w)
    before = _snapshot(w)
    assert ad.compact_item(ctx, False) is None                       # off by default: nothing to take out
    item = ad.compact_item(ctx, True)
    assert item.id == f"{adapter_id}-compact-block" and item.changes[0]["path"] == str(path)
    with credential_guard():
        applied = apply_changes(ctx, item.changes, stamp="c-on")
    now = path.read_text()
    assert now.count(COMPACT_START) == 1 and now.count(RULES_START) == text.count(RULES_START)
    assert ad.compact_item(ctx, True) is None                        # a second `on` plans nothing
    off = ad.compact_item(ctx, False)
    with credential_guard():
        apply_changes(ctx, off.changes, stamp="c-off")
    assert _snapshot(w) == before
    # on again, then undo of that manifest
    with credential_guard():
        applied = apply_changes(ctx, ad.compact_item(ctx, True).changes, stamp="c-on2")
        ad.undo(ctx, applied["manifest_path"])
    assert _snapshot(w) == before


def test_crlf_file_keeps_its_line_endings_and_comes_back_byte_for_byte(tmp_path):
    w, path = _world_for(tmp_path, "claude-code", "# Project\n")
    path.write_bytes(b"# Project\r\n\r\nRule one.\r\n")
    ad, ctx = BY_ID["claude-code"], _ctx(w)
    before = _snapshot(w)
    with credential_guard():
        apply_changes(ctx, ad.compact_item(ctx, True).changes, stamp="c-on")
    on = path.read_bytes()
    assert COMPACT_START.encode() in on and b"\n" not in on.replace(b"\r\n", b"")   # every line still ends CRLF
    assert ad.compact_item(ctx, True) is None
    with credential_guard():
        apply_changes(ctx, ad.compact_item(ctx, False).changes, stamp="c-off")
    assert _snapshot(w) == before


def test_files_that_are_never_created_and_the_home_folder(tmp_path):
    w = _world(tmp_path)
    _gemini_home(w)
    _copilot_home(w)
    ctx = _ctx(w)
    assert BY_ID["gemini-cli"].compact_item(ctx, True) is None       # no GEMINI.md: not created
    assert BY_ID["copilot-cli"].compact_item(ctx, True) is None      # no copilot-instructions.md: not created
    (w / "root" / "CLAUDE.md").unlink()
    assert BY_ID["claude-code"].compact_item(ctx, True).changes[0]["kind"] == "create"
    assert BY_ID["codex"].compact_item(ctx, True).changes[0]["kind"] == "create"
    from tokenstretch.context import build_context
    home_ctx = build_context(w / "home", w / "home", w / "sys")
    assert all(a.compact_item(home_ctx, True) is None for a in REGISTRY)
    for a in REGISTRY:                                               # tools with no instruction file here
        if a.id not in FILES:
            assert a.compact_file(ctx) is None and a.compact_item(ctx, True) is None


def test_one_block_for_tools_that_load_the_same_files(tmp_path):
    w = _world(tmp_path)                                             # has CLAUDE.md
    _copilot_home(w)
    gh = w / "root" / ".github"
    gh.mkdir()
    (gh / "copilot-instructions.md").write_text("# Rules\n")
    ctx = _ctx(w)
    items = cm.plan(ctx, True)
    assert "claude-code-compact-block" in [i.id for i in items]
    assert "copilot-cli-compact-block" not in [i.id for i in items]  # Copilot CLI reads CLAUDE.md too
    cm.apply(ctx, items)
    assert COMPACT_START not in (gh / "copilot-instructions.md").read_text()
    assert BY_ID["copilot-cli"].compact_item(ctx, True) is None
    assert cm.plan(ctx, True) == []


# ---- the command ---------------------------------------------------------------------------------

def test_cli_on_off_status_dry_run_and_undo(tmp_path):
    w = _world(tmp_path)
    md = w / "root" / "CLAUDE.md"
    before = _snapshot(w)
    code, out, err = _cli(w, "compact")
    assert code == 0 and "Compact mode is off" in out and "compact on" in out
    code, out, _ = _cli(w, "compact", "on", "--dry-run")
    assert code == 0 and "nothing was changed" in out and _snapshot(w) == before
    code, out, _ = _cli(w, "compact", "on")
    assert code == 0 and "backed up" in out and md.read_text().count(COMPACT_START) == 1
    backups = list((w / "home" / ".tokenstretch" / "backups").rglob("CLAUDE.md"))
    assert backups and backups[0].read_text() == "# Project\n"
    code, out, _ = _cli(w, "compact", "on")                          # twice: no second block
    assert code == 0 and "already on" in out and md.read_text().count(COMPACT_START) == 1
    assert json.loads(_cli(w, "compact", "--json")[1]) == {"on": True, "files": [str(md)]}
    assert _cli(w, "undo")[0] == 0                                   # undo takes it out
    assert _snapshot(w) == before
    assert _cli(w, "compact", "on")[0] == 0
    code, out, _ = _cli(w, "compact", "off")
    assert code == 0 and _snapshot(w) == before
    assert "already off" in _cli(w, "compact", "off")[1]


def test_setup_then_compact_is_one_block_and_each_undo_step_works(tmp_path):
    w = _world(tmp_path)
    md = w / "root" / "CLAUDE.md"
    assert _cli(w, "setup", "--yes", "--no-installs")[0] == 0
    after_setup = _snapshot(w)
    assert RULES_START in md.read_text() and COMPACT_START not in md.read_text()   # setup alone leaves it off
    assert _cli(w, "compact", "on")[0] == 0
    text = md.read_text()
    assert text.count(RULES_START) == 1 and text.index(COMPACT_START) < text.index(RULES_END)
    assert approx_tokens(text[text.index(COMPACT_START):text.index(COMPACT_END)]) < 200
    assert _cli(w, "undo")[0] == 0
    assert _snapshot(w) == after_setup
    # trim leaves the merged block alone
    assert _cli(w, "compact", "on")[0] == 0
    with_compact = md.read_text()
    assert _cli(w, "trim", "--apply", "--yes")[0] == 0
    assert md.read_text() == with_compact
    assert _cli(w, "compact", "off")[0] == 0
    assert _snapshot(w) == after_setup


# ---- offered in `start` --------------------------------------------------------------------------

def test_start_offers_it_and_start_compact_applies_it_with_one_undo(tmp_path):
    w = _world(tmp_path)
    before = _snapshot(w)
    code, out, _ = _cli(w, "start")
    assert code == 0 and "compact on" in out and "Turn on compact mode" not in out and _snapshot(w) == before
    code, out, _ = _cli(w, "start", "--compact")
    assert code == 0 and "Turn on compact mode" in out and "start --yes --compact" in out and _snapshot(w) == before
    data = json.loads(_cli(w, "start", "--compact", "--json")[1])
    assert data["planned_changes"][-1]["id"] == "compact-mode" and data["applied"] is False
    code, out, _ = _cli(w, "start", "--yes", "--compact")
    assert code == 0
    text = (w / "root" / "CLAUDE.md").read_text()
    assert text.count(RULES_START) == 1 and text.count(COMPACT_START) == 1
    assert text.index(COMPACT_START) < text.index(RULES_END)         # merged, not a second block
    code, out, _ = _cli(w, "start", "--compact")
    assert "Already set up" in out and "compact on" not in out       # on already: no offer, nothing to do
    assert _cli(w, "undo")[0] == 0
    assert _snapshot(w) == before


# ---- restore: off, undo, line endings, one block ---------------------------------------------------

def test_undo_after_compact_off_does_not_put_the_block_back(tmp_path):
    w = _world(tmp_path)
    md = w / "root" / "CLAUDE.md"
    before = _snapshot(w)
    assert _cli(w, "compact", "on")[0] == 0 and _cli(w, "compact", "off")[0] == 0
    code, out, _ = _cli(w, "undo")
    assert code == 0 and "Nothing to undo" in out and _snapshot(w) == before
    assert COMPACT_START not in md.read_text()
    assert json.loads(_cli(w, "undo", "--json")[1])["restored"] == []
    # a new `on` is the latest change again, and undo reverses that one
    assert _cli(w, "compact", "on")[0] == 0 and COMPACT_START in md.read_text()
    assert _cli(w, "undo")[0] == 0 and _snapshot(w) == before


def test_undo_after_compact_off_goes_to_the_setup_run_before_it(tmp_path):
    w = _world(tmp_path)
    md = w / "root" / "CLAUDE.md"
    before = _snapshot(w)
    assert _cli(w, "setup", "--yes", "--no-installs")[0] == 0
    after_setup = _snapshot(w)
    assert _cli(w, "compact", "on")[0] == 0 and _cli(w, "compact", "off")[0] == 0
    assert _snapshot(w) == after_setup
    assert _cli(w, "undo")[0] == 0                                   # reverses setup, not the compact switch
    assert COMPACT_START not in md.read_text() and RULES_START not in md.read_text()
    assert _snapshot(w) == before


def test_compact_off_deletes_a_file_that_compact_created_and_undo_is_clean(tmp_path):
    w = _world(tmp_path)
    md = w / "root" / "CLAUDE.md"
    md.unlink()
    before = _snapshot(w)
    assert _cli(w, "compact", "on")[0] == 0 and md.is_file()
    assert _cli(w, "compact", "off")[0] == 0
    assert not md.exists() and _snapshot(w) == before                # not left behind empty
    assert _cli(w, "undo")[0] == 0 and not md.exists()               # and undo does not bring it back
    # the deleted file was backed up first
    assert any(COMPACT_START in p.read_text() for p in (w / "home" / ".tokenstretch" / "backups").rglob("CLAUDE.md"))


def test_compact_off_keeps_a_created_file_the_user_wrote_in(tmp_path):
    w = _world(tmp_path)
    md = w / "root" / "CLAUDE.md"
    md.unlink()
    assert _cli(w, "compact", "on")[0] == 0
    md.write_bytes(b"# Mine\n\n" + md.read_bytes())
    assert _cli(w, "compact", "off")[0] == 0
    assert md.read_bytes().startswith(b"# Mine\n") and COMPACT_START.encode() not in md.read_bytes()


@pytest.mark.parametrize("raw", [
    b"# P\r\n\r\nline one\nline two\r\nline three\n",                # mixed, LF last
    b"# P\n\nline one\r\nline two\r\n",                              # mixed, CRLF last
    b"# P\r\nno final line ending",
    b"# P\nno final line ending",
    b"# P\n\n\n",
    b"# P\r\n\r\n",
    b"caf\xe9 is not UTF-8\n",                                       # bytes that are not UTF-8 come back as they were
    b"",
])
def test_off_and_undo_restore_any_line_endings_byte_for_byte(tmp_path, raw):
    w = _world(tmp_path)
    md = w / "root" / "CLAUDE.md"
    md.write_bytes(raw)
    before = _snapshot(w)
    assert _cli(w, "compact", "on")[0] == 0
    on = md.read_bytes()
    assert on.startswith(raw) and COMPACT_START.encode() in on       # nothing already there is changed
    assert _cli(w, "compact", "off")[0] == 0 and _snapshot(w) == before
    assert _cli(w, "compact", "on")[0] == 0 and _cli(w, "undo")[0] == 0 and _snapshot(w) == before


def test_mixed_line_endings_keep_every_existing_byte_when_the_file_was_edited_since(tmp_path):
    w = _world(tmp_path)
    md = w / "root" / "CLAUDE.md"
    raw = b"# P\r\n\r\nline one\nline two\r\n"
    md.write_bytes(raw)
    assert _cli(w, "compact", "on")[0] == 0
    md.write_bytes(md.read_bytes() + b"added later\n")               # no longer what `on` wrote: text removal
    assert _cli(w, "compact", "off")[0] == 0
    assert md.read_bytes() == raw + b"\r\nadded later\n" and COMPACT_START.encode() not in md.read_bytes()


def test_block_text_helpers_never_change_an_existing_character():
    for text in ["a\r\nb\nc\r\n", "a\nb\r\nc\n", "a\r\nb", "a\r\n\r\n", f"a\r\nb\n\r\n{RULES}".replace("\n", "\r\n")]:
        on = add_compact_block(text)
        assert on.startswith(text[:text.find(RULES_END)] if RULES_END in text else text)
        nl = "\r\n" if text.count("\r\n") > text.count("\n") - text.count("\r\n") else "\n"
        block = on[on.index(COMPACT_START):on.index(COMPACT_END)]
        assert ("\r\n" in block) == (nl == "\r\n") and block.replace("\r\n", "").count("\n") == (0 if nl == "\r\n" else block.count("\n"))
        # Text removal is exact when the file ended with one line ending. Other endings are restored
        # from the backup instead (see the byte-for-byte test above), because the text alone cannot say
        # whether a blank line in front of the block was there before.
        if text.endswith("\n") and not text.endswith(("\n\n", "\n\r\n")):
            assert remove_compact_block(on) == text


def test_setup_after_a_standalone_compact_block_leaves_one_block(tmp_path):
    w = _world(tmp_path)
    md = w / "root" / "CLAUDE.md"
    before = _snapshot(w)
    assert _cli(w, "compact", "on")[0] == 0
    standalone = md.read_text()
    assert standalone.count("## ") == 1 and RULES_START not in standalone
    assert _cli(w, "setup", "--yes", "--no-installs")[0] == 0
    both = md.read_text()
    assert both.count(COMPACT_START) == 1 and both.count(RULES_START) == 1
    assert both.count("## ") == 1                                    # one block, one heading
    assert both.index(RULES_START) < both.index(COMPACT_START) < both.index(COMPACT_END) < both.index(RULES_END)
    assert both.count("Read narrowly") == 1 and "Search first" not in both
    assert _cli(w, "compact", "off")[0] == 0
    assert COMPACT_START not in md.read_text() and md.read_text().count(RULES_START) == 1
    # CLAUDE.md is no longer what setup wrote, so undo refuses that file, as for any file edited
    # since setup, and leaves it as it is. It does not put the compact block back.
    left = md.read_bytes()
    assert _cli(w, "undo")[0] == 2 and md.read_bytes() == left


def test_a_deleted_file_is_put_back_by_undo_and_never_over_a_new_one(tmp_path):
    from tokenstretch.installer import DriftError, undo_manifest
    w = _world(tmp_path)
    md, ctx = w / "root" / "CLAUDE.md", _ctx(w)
    raw = md.read_bytes()
    with credential_guard():
        done = apply_changes(ctx, [{"path": str(md), "kind": "delete", "reason": "test"}], stamp="del-1")
    assert not md.exists() and done["manifest"]["files"][0]["action"] == "deleted"
    md.write_text("something new\n")
    with pytest.raises(DriftError):
        undo_manifest(done["manifest_path"])
    assert md.read_text() == "something new\n"
    md.unlink()
    assert undo_manifest(done["manifest_path"])["restored"] == [str(md)] and md.read_bytes() == raw
    assert undo_manifest(done["manifest_path"])["skipped"] == [str(md)]


def test_undo_reads_the_switch_from_manifests_written_before_the_key_existed():
    from tokenstretch.installer import compact_switch
    assert compact_switch({"profile": "compact", "title": "Take the compact-mode rules out of CLAUDE.md"}) == "off"
    assert compact_switch({"profile": "compact", "title": "Add the compact-mode rules to CLAUDE.md"}) == "on"
    assert compact_switch({"profile": "clean", "title": "Take"}) is None
    assert compact_switch({"profile": "compact", "compact": "on", "title": "Take"}) == "on"
