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
