"""Rework meter: signals from raw Claude Code log lines, pure and offline."""
import json

from tokenstretch import rework


def _asst(mid, blocks, model="claude-fable-5-2"):
    return {"type": "assistant", "message": {"id": mid, "model": model, "content": blocks, "usage": {}}}


def _edit(i, path):
    return {"type": "tool_use", "id": f"e{i}", "name": "Edit", "input": {"file_path": path}}


def _bash(i, cmd):
    return {"type": "tool_use", "id": f"b{i}", "name": "Bash", "input": {"command": cmd}}


def _result(i, err):
    return {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": f"b{i}", "content": "x", "is_error": err}]}}


def _prompt(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _write(tmp_path, objs):
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(o) for o in objs) + "\n")
    return p


def _session(path, n, effort="max", model="claude-fable-5-2"):
    turns = [{"model": model, "effort": effort, "history_tokens": 1000, "cache_read": 800, "cache_write": 100,
              "output_tokens": 100} for _ in range(n)]
    return {"adapter": "claude-code", "path": str(path), "turns": turns}


def test_edit_after_failing_check_and_fail_fix(tmp_path):
    p = _write(tmp_path, [
        _asst("1", [_edit(1, "a.py")]),
        _asst("2", [_bash(2, "python -m pytest -q")]), _result(2, True),
        _asst("3", [_edit(3, "a.py")]),
        _asst("4", [_bash(4, "pytest")]), _result(4, False),
    ])
    scan = rework.scan_log(p)
    assert scan["steps"] == 4
    marks = rework.redo_steps(4, scan["events"])
    assert "after-fail" in marks[2]
    assert marks[2] >= {"fail-fix"} and marks[3] == {"fail-fix"}
    assert 0 not in marks and 1 not in marks


def test_same_file_three_times(tmp_path):
    p = _write(tmp_path, [_asst(str(i), [_edit(i, "b.py")]) for i in range(4)])
    scan = rework.scan_log(p)
    marks = rework.redo_steps(scan["steps"], scan["events"])
    assert set(marks) == {2, 3} and marks[2] == {"3x-edit"}


def test_correction_prompt_marks_following_steps(tmp_path):
    p = _write(tmp_path, [
        _prompt("add a retry"), _asst("1", [_edit(1, "c.py")]),
        _prompt("that is wrong, redo it"), _asst("2", [_edit(2, "d.py")]), _asst("3", [_edit(3, "e.py")]),
        _prompt("thanks, now write the docs"), _asst("4", [_edit(4, "f.py")]),
    ])
    scan = rework.scan_log(p)
    marks = rework.redo_steps(scan["steps"], scan["events"])
    assert set(marks) == {1, 2} and marks[1] == {"correction"}


def test_long_prompt_is_not_correction():
    assert not rework.is_correction("still " * 100)
    assert rework.is_correction("This is not what I asked")
    assert not rework.is_correction("stillness is fine")


def test_unfinished_failure_not_counted(tmp_path):
    p = _write(tmp_path, [_asst("1", [_bash(1, "make test")]), _result(1, True), _asst("2", [_edit(2, "z.py")])])
    scan = rework.scan_log(p)
    assert rework.redo_steps(2, scan["events"]) == {}


def test_view_rows_and_small_sample_sentence(tmp_path):
    p = _write(tmp_path, [_asst(str(i), [_edit(i, "b.py")]) for i in range(4)])
    view = rework.rework_view([_session(p, 4), {"adapter": "codex", "path": str(p), "turns": []}])
    assert view["unreadable_sessions"] == 1
    row = view["rows"][0]
    assert row["steps"] == 4 and row["redo_steps"] == 2 and row["sessions"] == 1
    assert 0 < row["redo_share"] < 1
    assert row["units_per_session_no_redo"] < row["units_per_session"]
    assert "too small" in view["comparison"]
    text = "\n".join(rework.format_rework(view))
    assert "heuristic" in text and "not a bill" in text and "after-fail" in text


def test_comparison_needs_three_sessions_each(tmp_path):
    p = _write(tmp_path, [_asst("1", [_edit(1, "q.py")])])
    sess = [_session(p, 1, "max") for _ in range(3)] + [_session(p, 1, "medium") for _ in range(3)]
    assert "max sessions put" in rework_cmp(sess)
    assert "too small" in rework_cmp(sess[:4])


def rework_cmp(sess):
    return rework.rework_view(sess)["comparison"]


def test_unpriced_model_not_guessed(tmp_path):
    p = _write(tmp_path, [_asst("1", [_edit(1, "q.py")], model="mystery-1")])
    row = rework.rework_view([_session(p, 1, model="mystery-1")])["rows"][0]
    assert row["units_per_session"] is None and row["redo_share"] is None


def test_mismatched_steps_skipped(tmp_path):
    p = _write(tmp_path, [_asst("1", [_edit(1, "q.py")])])
    assert rework.rework_view([_session(p, 5)])["skipped_sessions"] == 1
