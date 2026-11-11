"""trim: instruction-file findings, safe mechanical proposal, dry run, apply + undo, privacy."""
from __future__ import annotations

import builtins
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from conftest import CANARIES, credential_guard

import tokenstretch.trim as trim
from tokenstretch.adapters.base import rules_block
from tokenstretch.cli import main
from tokenstretch.context import build_context
from tokenstretch.trim import Options, analyze, approx_tokens, path_mentions

SHARED = "- Always run the full test suite before you say a task is done."
PNPM = "- Use pnpm, not npm, for every install in this repository."
LOG = "\n".join(f"2026-01-05 10:00:0{i} {'ERROR' if i == 3 else 'INFO'} step {i}" for i in range(9))
CODE = "\n".join(f"print({i})" for i in range(40))
BLOB = "A" * 260
TABLE = "| a | b |\n|---|---|\n" + "\n".join(f"| {i} | {i * 2} |" for i in range(25))
RULES = rules_block("sonnet", "/clear")

PROJECT_CLAUDE = f"""# Project

Read src/app.py first. The old entry point was src/legacy/main.py.   

{SHARED}
{PNPM}



## Build log

```
{LOG}
```

## Big example

```python
{CODE}
```

{TABLE}

Blob: {BLOB}

{PNPM}
- Never commit secrets/credentials.json, ../outside/thing.txt, or .env.local/x.txt.
- Docs live in docs/guide.md and https://example.com/docs/missing.md.

{RULES}
{RULES}"""


def _world(tmp_path: Path) -> Path:
    home, root, sys_ = tmp_path / "home", tmp_path / "root", tmp_path / "sys"
    for d in (home / ".claude", home / ".codex", home / ".copilot", root / "src", root / "docs", sys_):
        d.mkdir(parents=True, exist_ok=True)
    (home / ".claude" / ".credentials.json").write_text("CANARY_CREDENTIAL_DO_NOT_READ_CLAUDE")
    (home / ".codex" / "auth.json").write_text("CANARY_CREDENTIAL_DO_NOT_READ_CODEX")
    (home / ".claude" / "CLAUDE.md").write_text(f"# Mine\n\n{SHARED}\n- Keep replies short.\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text(PROJECT_CLAUDE, encoding="utf-8")
    (root / "AGENTS.md").write_text(
        "# Agents\n\nUse pnpm, not npm, for every single install in this repository, always please.\n\n"
        "Use pnpm and never npm, for every single install in this repository, always please.\n\n"
        "- Keep diffs small and focused on the task at hand.\n", encoding="utf-8")
    (root / "src" / "app.py").write_text("x = 1\n")
    (root / "docs" / "guide.md").write_text("guide\n")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "thing.txt").write_text("outside\n")
    return tmp_path


def _ctx(w: Path):
    return build_context(w / "home", w / "root", w / "sys")


def _cli(w: Path, *args: str, code: int = 0) -> str:
    buf = io.StringIO()
    with credential_guard(), redirect_stdout(buf):
        assert main(["trim", "--home", str(w / "home"), "--project", str(w / "root"),
                     "--sys-snapshot", str(w / "sys"), *args]) == code
    out = buf.getvalue()
    for c in CANARIES:
        assert c not in out
    return out


def _tree(w: Path) -> dict:
    return {str(p.relative_to(w)): (p.read_bytes() if p.is_file() else None, p.stat().st_mtime_ns)
            for p in sorted(w.rglob("*"))}


def _by_cat(an, cat):
    return [f for f in an.findings if f.category == cat]


def test_findings_for_each_category(tmp_path):
    w = _world(tmp_path)
    with credential_guard():
        an = analyze(_ctx(w), Options(max_tokens=500))
    files = {f.display: f for f in an.files}
    assert set(files) >= {"~/.claude/CLAUDE.md", "CLAUDE.md", "AGENTS.md"}
    assert files["~/.claude/CLAUDE.md"].scope == "user"
    # (a) exact duplicates: within one file, and user + project loaded together by Claude Code
    dups = _by_cat(an, "duplicate")
    assert any(d.file == "CLAUDE.md" and "repeats CLAUDE.md:" in d.message and d.fixable for d in dups)
    # ...but Copilot CLI also loads the project CLAUDE.md and not the user one, so that copy stays
    cross = [d for d in dups if d.file == "CLAUDE.md" and "repeats ~/.claude/CLAUDE.md:" in d.message]
    assert cross and not cross[0].fixable and "another tool loads only this file" in cross[0].fix
    # near-duplicate: two wordings in AGENTS.md (report only)
    near = _by_cat(an, "near-duplicate")
    assert any(n.file == "AGENTS.md" and not n.fixable for n in near)
    # (b) very long file
    assert any(x.file == "CLAUDE.md" for x in _by_cat(an, "long-file"))
    # (c) big blocks: log, code, table, blob
    msgs = " ".join(x.message for x in _by_cat(an, "big-block"))
    for label in ("pasted log", "code block", "long table", "base64 or minified blob"):
        assert label in msgs
    # (d) stale paths: only the missing one; URLs, credentials, and ../ never reported
    stale = _by_cat(an, "stale-path")
    assert len(stale) == 1 and "src/legacy/main.py" in stale[0].message
    for bad in ("src/app.py", "docs/guide.md", "credentials", "thing.txt", "example.com", ".env"):
        assert bad not in stale[0].message
    # (e) whitespace
    ws = [x for x in _by_cat(an, "whitespace") if x.file == "CLAUDE.md"]
    assert ws and "extra blank line" in ws[0].message and "trailing spaces" in ws[0].message
    # (f) rules block duplicated
    rd = _by_cat(an, "rules-duplicate")
    assert len(rd) == 1 and rd[0].fixable
    # every finding explains itself
    assert all(x.why and x.fix for x in an.findings)


def test_user_and_project_duplicate_removed_when_only_one_tool_loads_both(tmp_path):
    w = _world(tmp_path)
    (w / "home" / ".copilot").rmdir()
    with credential_guard():
        an = analyze(_ctx(w))
    cross = [d for d in _by_cat(an, "duplicate") if "repeats ~/.claude/CLAUDE.md:" in d.message]
    assert cross and cross[0].fixable and "Claude Code" in cross[0].why


def test_cross_file_duplicate_kept_when_another_tool_loads_only_that_file(tmp_path):
    w = _world(tmp_path)
    # Copilot CLI loads CLAUDE.md and AGENTS.md together; Codex loads only AGENTS.md.
    (w / "root" / "AGENTS.md").write_text("# Agents\n\n" + PNPM + "\n", encoding="utf-8")
    with credential_guard():
        an = analyze(_ctx(w))
    tools = {f.display: set(f.tools) for f in an.files}
    assert {"codex", "copilot-cli"} <= tools["AGENTS.md"] or {"codex", "copilot-cli"} <= tools["CLAUDE.md"] | tools["AGENTS.md"]
    cross = [d for d in _by_cat(an, "duplicate") if "AGENTS.md" in d.message and "CLAUDE.md" in d.message]
    assert cross and not any(d.fixable for d in cross)


def test_dry_run_writes_nothing(tmp_path):
    w = _world(tmp_path)
    before = _tree(w)
    out = _cli(w, "--move-blocks")
    assert "Dry run: nothing was changed" in out and "--- a/CLAUDE.md" in out and "approx." in out
    assert "per 100 turns" in out
    assert _tree(w) == before
    assert not (w / "home" / ".tokenstretch").exists()
    # --apply without a terminal and without --yes also changes nothing
    out = _cli(w, "--apply", code=1)
    assert "nothing was changed" in out and _tree(w) == before


def test_apply_then_undo_restores_bytes_exactly(tmp_path):
    w = _world(tmp_path)
    (w / "home" / ".copilot").rmdir()
    crlf = w / "root" / ".claude" / "CLAUDE.md"
    crlf.parent.mkdir()
    crlf.write_bytes(b"# Copilot\r\n\r\n\r\n\r\n- Be brief in every single reply.   \r\n- Be brief in every single reply.   \r\n")
    originals = {p: p.read_bytes() for p in (w / "root").rglob("*") if p.is_file()}
    originals[w / "home" / ".claude" / "CLAUDE.md"] = (w / "home" / ".claude" / "CLAUDE.md").read_bytes()
    out = _cli(w, "--apply", "--yes", "--move-blocks")
    assert "backed up and trimmed" in out and "tokenstretch undo" in out
    new = (w / "root" / "CLAUDE.md").read_text(encoding="utf-8")
    assert new.count(PNPM) == 1 and SHARED not in new and new.count("credit-rules start") == 1
    assert "\n\n\n" not in new and "main.py.   " not in new
    assert crlf.read_bytes() == b"# Copilot\r\n\r\n- Be brief in every single reply.\r\n"
    assert (w / "home" / ".claude" / "CLAUDE.md").read_bytes() == originals[w / "home" / ".claude" / "CLAUDE.md"]
    assert (w / "root" / "docs" / "agent-notes").is_dir()
    manifests = list((w / "home" / ".tokenstretch" / "manifests").glob("*.json"))
    assert len(manifests) == 1 and json.loads(manifests[0].read_text())["profile"] == "trim"

    buf = io.StringIO()
    with redirect_stdout(buf):
        assert main(["undo", "--home", str(w / "home"), "--root", str(w / "root"), "--sys-snapshot", str(w / "sys")]) == 0
    now = {p: p.read_bytes() for p in (w / "root").rglob("*") if p.is_file()}
    now[w / "home" / ".claude" / "CLAUDE.md"] = (w / "home" / ".claude" / "CLAUDE.md").read_bytes()
    assert now == originals
    assert not (w / "root" / "docs" / "agent-notes").exists()


def test_moved_block_pointer_is_correct(tmp_path):
    w = _world(tmp_path)
    (w / "root" / "docs" / "agent-notes").mkdir()
    (w / "root" / "docs" / "agent-notes" / "claude-1.md").write_text("taken\n")
    _cli(w, "--apply", "--yes", "--move-blocks")
    text = (w / "root" / "CLAUDE.md").read_text(encoding="utf-8")
    pointers = [l for l in text.splitlines() if l.startswith("See docs/agent-notes/")]
    assert len(pointers) == 3  # log, code block, blob paragraph (tables are report-only)
    assert pointers[0] == "See docs/agent-notes/claude-2.md when needed (pasted log, 11 lines, moved by `tokenstretch trim`)."
    note = (w / "root" / "docs" / "agent-notes" / "claude-2.md").read_text(encoding="utf-8")
    assert note == f"# Moved from CLAUDE.md by tokenstretch trim\n\n```\n{LOG}\n```\n"
    code_note = (w / "root" / "docs" / "agent-notes" / "claude-3.md").read_text(encoding="utf-8")
    assert f"```python\n{CODE}\n```" in code_note
    assert (w / "root" / "docs" / "agent-notes" / "claude-1.md").read_text() == "taken\n"
    for p in pointers:
        rel = p.split()[1]
        assert (w / "root" / rel).is_file()
    # the pointer sits where the block was: right under its heading
    lines = text.splitlines()
    assert lines[lines.index("## Build log") + 2] == pointers[0]


def test_user_files_never_get_blocks_moved(tmp_path):
    w = _world(tmp_path)
    (w / "home" / ".claude" / "CLAUDE.md").write_text(f"# Mine\n\n```\n{CODE}\n```\n", encoding="utf-8")
    with credential_guard():
        an = analyze(_ctx(w), Options(move_blocks=True))
    user = [x for x in _by_cat(an, "big-block") if x.file.startswith("~/")]
    assert user and not any(x.fixable for x in user)


def test_stale_check_never_touches_outside_project_or_credentials(tmp_path, monkeypatch):
    w = _world(tmp_path)
    root = os.path.normpath(str(w / "root"))
    checked: list[str] = []
    real = trim._exists
    monkeypatch.setattr(trim, "_exists", lambda p: (checked.append(p), real(p))[1])
    opened: list[str] = []
    real_open, real_rb, real_rt = builtins.open, Path.read_bytes, Path.read_text

    def rec_open(f, *a, **k):
        opened.append(str(f))
        return real_open(f, *a, **k)

    def rec_rb(self):
        opened.append(str(self))
        return real_rb(self)

    def rec_rt(self, *a, **k):
        opened.append(str(self))
        return real_rt(self, *a, **k)

    monkeypatch.setattr(builtins, "open", rec_open)
    monkeypatch.setattr(Path, "read_bytes", rec_rb)
    monkeypatch.setattr(Path, "read_text", rec_rt)
    with credential_guard():
        an = analyze(_ctx(w))
    monkeypatch.undo()
    assert checked and all(os.path.commonpath([c, root]) == root for c in checked)
    assert not any(trim.credential_like(os.path.relpath(c, root)) for c in checked)
    assert not any("outside" in c or "legacy" in c and not c.startswith(root) for c in checked)
    assert not any("thing.txt" in o or "app.py" in o or "guide.md" in o for o in opened)
    assert not any(trim.credential_like(Path(o).name) for o in opened)
    assert _by_cat(an, "stale-path")


def test_path_mentions_rules():
    text = ("see src/a.py, ./lib/b.ts and docs/c.md. Not https://x.com/d/e.md, not /etc/f.conf, "
            "not ~/g/h.md, not example.com/i.md, not ../j/k.md, not path/to/file.ext, not @scope/pkg.js, not src/")
    assert [p for _, p in path_mentions(text)] == ["src/a.py", "lib/b.ts", "docs/c.md"]
    assert trim.credential_like("config/.env.production") and trim.credential_like("keys/server.pem")
    assert trim.credential_like("x/client_secret.json") and not trim.credential_like("tokenstretch/trim.py")
    assert approx_tokens("abcd" * 10) == 10 and approx_tokens("abcde") == 2


def test_json_shape(tmp_path):
    w = _world(tmp_path)
    data = json.loads(_cli(w, "--json"))
    assert data["mode"] == "dry-run" and data["manifest"] is None and "characters / 4" in data["approx"]
    assert {"files", "tools", "findings", "proposal", "max_tokens", "move_blocks", "project"} <= set(data)
    f0 = data["files"][0]
    assert {"path", "display", "scope", "tools", "tokens", "lines", "bytes", "opened", "editable", "note"} <= set(f0)
    t0 = data["tools"][0]
    assert {"id", "name", "files", "tokens_per_turn", "per_100_turns", "tokens_per_turn_after"} <= set(t0)
    assert t0["per_100_turns"] == 100 * t0["tokens_per_turn"]
    x0 = data["findings"][0]
    assert set(x0) == {"category", "severity", "file", "line", "message", "why", "fix", "fixable"}
    p = data["proposal"]
    assert {"changes", "tokens_before", "tokens_after", "tokens_saved_per_turn", "diff"} <= set(p)
    assert p["tokens_saved_per_turn"] == p["tokens_before"] - p["tokens_after"] > 0
    assert p["diff"].startswith("--- a/")
    applied = json.loads(_cli(w, "--json", "--apply", "--yes"))
    assert applied["mode"] == "applied" and applied["manifest"].endswith("-trim.json")


def test_lean_files_have_nothing_to_change(tmp_path):
    w = tmp_path
    for d in ("home/.claude", "root", "sys"):
        (w / d).mkdir(parents=True)
    (w / "root" / "CLAUDE.md").write_text("# Project\n\n- Use pnpm.\n", encoding="utf-8")
    out = _cli(w)
    assert "Findings: none" in out and "nothing safe to change" in out


def test_dashboard_has_instruction_files_card(tmp_path):
    from tokenstretch.dashboard import DashboardState, render_page
    w = _world(tmp_path)
    before = _tree(w)
    with credential_guard():
        st = DashboardState(_ctx(w), tz=None).snapshot()
    ins = st["instructions"]
    assert ins and ins["command"] == "tokenstretch trim" and ins["findings"] > 0
    assert {f["display"] for f in ins["files"]} >= {"CLAUDE.md", "AGENTS.md"}
    assert ins["tools"] and ins["saved_per_turn"] > 0 and "characters / 4" in ins["approx"]
    assert 'id="instructions"' in render_page(st) and "Instruction files" in render_page(st)
    assert _tree(w) == before  # read-only


def test_doctor_points_at_trim(tmp_path):
    from conftest import run_doctor
    _, findings, _, _ = run_doctor("bloated", tmp_path)
    w01 = [f for f in findings if f["id"] in ("W01", "W02", "W17")]
    assert w01 and any("tokenstretch trim" in f["fix"] for f in w01)
