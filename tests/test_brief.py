"""brief: a local prompt generator and lint (no model calls, no log reads)."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

from tokenstretch.brief import Brief, build, lint, render
from tokenstretch.cli import main


def _codes(b: Brief) -> list[str]:
    return [x.code for x in lint(b)]


def test_bare_task_is_flagged_for_everything_it_leaves_open():
    assert _codes(Brief("fix the bug")) == ["no-files", "no-done", "vague", "no-sources"]
    assert "several-tasks" in _codes(Brief("add the retry and then also rewrite the docs"))
    assert "unbounded-research" in _codes(Brief("research which queue library to use"))
    assert "unbounded-research" not in _codes(Brief("research which queue library to use", max_searches=5))


def test_a_complete_brief_has_nothing_missing():
    b = Brief("add a retry with backoff to the upload client", files=["src/upload.py"], sources=["docs/upload-notes.md"],
              done="tests pass", no_build=False, helper=True)
    assert lint(b) == []
    text = render(b)
    assert text.startswith("Task: add a retry with backoff to the upload client\n")
    assert "Work only in this file: `src/upload.py`. Do not change any other file." in text
    assert "Work from: `docs/upload-notes.md`. Read each once." in text
    assert "No web search and no new research." in text and "Done when: tests pass" in text
    assert "Reply in under 80 words: what you wrote, file paths" in text


def test_paths_and_stop_conditions_in_the_task_text_count():
    assert _codes(Brief("rename get_user to fetch_user in src/api/users.py until the unit tests pass")) == []
    assert "no-files" not in _codes(Brief("update README.md wording", done="reads clearly"))
    assert "no-files" in _codes(Brief("update the wording in the readme", done="reads clearly"))


def test_research_brief_gets_a_cap_and_a_place_to_write():
    text = render(Brief("research queue libraries for the worker", files=["notes/queues.md"], max_searches=6))
    assert "At most 6 searches. Write findings to the file as you go, with source and date." in text
    assert "No web search" not in text
    forced = render(Brief("summarize the options", research=False))
    assert "No web search and no new research." in forced
    assert "Do not run builds" in render(Brief("write the page", no_build=True))


def test_build_reports_size_and_a_tier():
    data = build(Brief("fix a typo in README.md", done="spelling is right"))
    assert data["tier"] == "light" and data["approx_tokens"] == max(1, round(len(data["brief"]) / 4))
    assert data["lint"] == []


def _cli(*args: str) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(["brief", *args])
    return code, buf.getvalue()


def test_cli_brief_check_and_json(capsys):
    code, out = _cli("fix the bug")
    assert code == 0 and out.startswith("Task: fix the bug\n") and "Missing from this brief (4):" in out
    assert "Why it costs credits:" in out
    code, out = _cli("--check", "fix the bug")
    assert code == 1 and out.count("\n") == 4 and "Fix:" in out
    code, out = _cli("--check", "add a retry to src/upload.py", "--done", "tests pass")
    assert code == 0 and out == "Nothing missing.\n"
    code, out = _cli("--json", "write the cost page", "--files", "site/cost.py", "--from", "research/notes.md",
                     "--done", "the audit prints NO ISSUES", "--helper", "--no-build", "--reply-words", "60")
    data = json.loads(out)
    assert code == 0 and data["lint"] == [] and "under 60 words" in data["brief"]
    assert main(["brief"]) == 2
