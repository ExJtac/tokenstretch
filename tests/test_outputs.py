"""`outputs`: tool output text per tool, and the largest single outputs, from the session logs."""
import json
import os
import time

from tokenstretch import cli
from tokenstretch import outputs as op

NOW = 1_800_000_000.0
DAY = time.strftime("%Y-%m-%d", time.gmtime(NOW - 86400))


def _line(kind, blocks, day=DAY):
    return json.dumps({"type": kind, "timestamp": day + "T00:00:00Z", "message": {"content": blocks}}) + "\n"


def _use(i, name, **inp):
    return {"type": "tool_use", "id": i, "name": name, "input": inp}


def _res(i, content):
    return {"type": "tool_result", "tool_use_id": i, "content": content}


def _home(tmp_path, when=NOW - 3600):
    d = tmp_path / ".claude" / "projects" / "p"
    d.mkdir(parents=True)
    f = d / "s.jsonl"
    f.write_text(
        _line("assistant", [_use("a", "Read", file_path=str(tmp_path / "work" / "big.py")),
                            _use("b", "Bash", command="TOKEN=canary-value curl https://x")])
        + _line("user", [_res("a", "x" * 8000)])
        + _line("user", [_res("b", [{"type": "text", "text": "y" * 400}, {"type": "image", "source": {"data": "z" * 9000}}])])
        + _line("assistant", [_use("c", "Bash", command="/usr/bin/git log --all")])
        + _line("user", [_res("c", "canary-output " * 100)])
        + _line("assistant", [_use("old", "Read", file_path="/etc/old")], day="2020-01-01")
        + _line("user", [_res("old", "o" * 90000)], day="2020-01-01")
        + _line("user", [_res("orphan", "q" * 5000)])
        + "not json\n")
    os.utime(f, (when, when))
    return tmp_path


def test_totals_per_tool_and_largest_outputs(tmp_path):
    d = op.scan(_home(tmp_path), NOW)
    tools = {t["name"]: t for t in d["tools"]}
    assert tools["Read"] == {"name": "Read", "calls": 1, "chars": 8000, "largest": 8000, "tokens": 2000}
    assert tools["Bash"]["calls"] == 2 and tools["Bash"]["chars"] == 400 + 1400   # the image is not counted
    assert d["chars"] == 9800 and d["sessions"] == 1 and d["complete"]            # old step and orphan left out
    assert [(r["tool"], r["target"], r["chars"]) for r in d["largest"]] == [
        ("Read", "~/work/big.py", 8000), ("Bash", "git", 1400), ("Bash", "(command)", 400)]
    text = "\n".join(op.format_report(d))
    assert "2.0k tokens" in text and "Read ~/work/big.py" in text and "Read: ask for the part you need" in text
    assert "canary" not in text and "canary" not in json.dumps(d) and "curl" not in text


def test_nothing_recent_and_incomplete_logs_are_said_plainly(tmp_path):
    home = _home(tmp_path, when=NOW - 30 * 86400)
    d = op.scan(home, NOW)
    assert d["tools"] == [] and "No tool output found" in "\n".join(op.format_report(d))
    part = op.scan(home, NOW, days=60, max_bytes=1)
    assert part["complete"] is False and part["sessions"] == 0


def test_cli_prints_the_report_and_json(tmp_path, capsys):
    home = _home(tmp_path, when=time.time())
    f = home / ".claude" / "projects" / "p" / "s.jsonl"
    f.write_text(f.read_text().replace(DAY, time.strftime("%Y-%m-%d", time.gmtime())))
    assert cli.main(["outputs", "--home", str(home), "--top", "1"]) == 0
    out = capsys.readouterr().out
    assert "Largest single outputs:" in out and "big.py" in out and "git" not in out.split("Largest single outputs:")[1].split("\n")[2]
    assert cli.main(["outputs", "--home", str(home), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["tools"][0]["name"] == "Read"
