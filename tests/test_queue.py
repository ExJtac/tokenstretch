"""Task queue: atomic claims, file overlap, failures, pace gate."""
import json
import os
import threading
import time

from tokenstretch import cli, queue as q


def test_add_writes_ready_brief(tmp_path):
    t = q.add_task(tmp_path, "fix the retry", files=["src/a.py"], done="tests pass")
    assert t["id"] == "T0001" and "Work only in this file: `src/a.py`" in t["brief"]
    assert (tmp_path / ".tokenstretch" / "queue" / "T0001.json").is_file()
    assert q.add_task(tmp_path, "second")["id"] == "T0002"


def test_from_plan_splits_steps_with_files_and_check(tmp_path):
    plan = ("# Plan\n1. Add retry. Files: `src/up.py`. Check: pytest tests/test_up.py passes.\n"
            "2. (hard) Redo the key in `src/key.py` and `src/cache.py`. Check: `pytest -q` passes\n\n"
            "## Open questions\n3. not a step\n")
    tasks = q.add_from_plan(tmp_path, plan)
    assert [t["files"] for t in tasks] == [["src/up.py"], ["src/key.py", "src/cache.py"]]
    assert tasks[0]["done"].startswith("pytest tests/test_up.py")
    assert tasks[1]["hard"] and tasks[1]["tier"] == "top"
    assert "Done when: `pytest -q` passes" in tasks[1]["brief"]


def test_two_claimers_never_get_same_task(tmp_path):
    for i in range(20):
        q.add_task(tmp_path, f"task {i}")
    got, lock = [], threading.Lock()

    def worker(name):
        while True:
            t, _ = q.claim_next(tmp_path, name)
            if t is None:
                return
            with lock:
                got.append(t["id"])

    threads = [threading.Thread(target=worker, args=(f"a{i}",)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(got) == 20 and len(set(got)) == 20


def test_exclusive_claim_file(tmp_path):
    q.add_task(tmp_path, "only")
    first, _ = q.claim_next(tmp_path, "a")
    second, why = q.claim_next(tmp_path, "b")
    assert first and second is None and why == "empty"


def test_overlapping_files_skipped(tmp_path):
    q.add_task(tmp_path, "one", files=["src/a.py"])
    q.add_task(tmp_path, "two", files=["src/"])         # directory contains the claimed file
    q.add_task(tmp_path, "three", files=["docs/x.md"])
    a, _ = q.claim_next(tmp_path, "a")
    b, _ = q.claim_next(tmp_path, "b")
    c, why = q.claim_next(tmp_path, "c")
    assert (a["id"], b["id"]) == ("T0001", "T0003")
    assert c is None and why == "blocked"
    q.done_task(tmp_path, "T0001")
    q.release_stale(tmp_path, 0)
    d, _ = q.claim_next(tmp_path, "d")
    assert d["id"] == "T0002"


def test_fail_twice_then_needs_lead(tmp_path):
    q.add_task(tmp_path, "shaky")
    t, _ = q.claim_next(tmp_path, "a")
    t = q.fail_task(tmp_path, t["id"], "wrong import")
    assert t["status"] == "open" and "wrong import" in t["brief"]
    t, _ = q.claim_next(tmp_path, "b")
    assert "wrong import" in t["brief"]
    t = q.fail_task(tmp_path, t["id"], "still red")
    assert t["status"] == "needs-lead" and "still red" in t["brief"]
    assert q.claim_next(tmp_path, "c") == (None, "empty")
    assert q.list_view(tmp_path)[0]["status"] == "needs-lead"


def test_release_stale(tmp_path):
    q.add_task(tmp_path, "slow")
    t, _ = q.claim_next(tmp_path, "a")
    assert q.release_stale(tmp_path, 10) == []
    old = time.time() - 3600
    os.utime(q._claim_path(q.queue_dir(tmp_path), t["id"]), (old, old))
    assert q.release_stale(tmp_path, 30) == [t["id"]]
    assert q.claim_next(tmp_path, "b")[0]["id"] == t["id"]


def test_pace_gate_open_without_module():
    assert q.pace_gate(None)["open"] is True


def test_cli_gate_closed_exit_3_and_empty_exit_4(tmp_path, monkeypatch, capsys):
    q.add_task(tmp_path, "work")
    monkeypatch.setattr(q, "pace_gate", lambda home=None: {"open": False, "wait_seconds": 120, "note": ""})
    rc = cli.main(["queue", "next", "--root", str(tmp_path), "--home", str(tmp_path)])
    assert rc == 3 and "wait about 2m" in capsys.readouterr().out
    assert q.list_view(tmp_path)[0]["status"] == "open"          # nothing was claimed
    monkeypatch.setattr(q, "pace_gate", lambda home=None: {"open": True, "wait_seconds": 0, "note": ""})
    assert cli.main(["queue", "next", "--root", str(tmp_path)]) == 0
    assert cli.main(["queue", "next", "--root", str(tmp_path)]) == 4


def test_cli_roundtrip_and_json(tmp_path, capsys):
    root = str(tmp_path)
    assert cli.main(["queue", "add", "do it", "--root", root, "--files", "a.py", "--done", "ok"]) == 0
    assert cli.main(["queue", "list", "--root", root, "--json"]) == 0
    assert json.loads(capsys.readouterr().out.split("\n", 1)[1])[0]["id"] == "T0001"
    assert cli.main(["queue", "next", "--root", root]) == 0
    assert cli.main(["queue", "done", "T0001", "--root", root, "--note", "fine"]) == 0
    assert cli.main(["queue", "fail", "T0009", "--root", root, "--note", "x"]) == 2
    assert cli.main(["queue", "release", "--stale", "5", "--root", root]) == 0
    assert cli.main(["queue", "loop-prompt"]) == 0
    assert "queue next" in capsys.readouterr().out


def test_from_plan_keeps_paths_written_without_backticks(tmp_path):
    plan = ("1. Add retry in src/up.py and tests/test_up.py. Check: pytest passes.\n"
            "2. Read https://example.com/docs/page.html and version 1.2 then fix lib/core/key.py.\n"
            "3. Write the notes, no code.\n")
    tasks = q.add_from_plan(tmp_path, plan)
    assert [t["files"] for t in tasks] == [["src/up.py", "tests/test_up.py"], ["lib/core/key.py"], []]


def test_list_drops_agent_name_once_claim_is_released(tmp_path):
    q.add_task(tmp_path, "one")
    task, _ = q.claim_next(tmp_path, agent="w4")
    assert q.list_view(tmp_path)[0]["agent"] == "w4"
    assert q.release_stale(tmp_path, 0) == [task["id"]]
    row = q.list_view(tmp_path)[0]
    assert row["status"] == "open" and row["agent"] == ""
