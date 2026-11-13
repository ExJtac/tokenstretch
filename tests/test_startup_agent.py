"""`startup --agent`: lean worker profile, from parts already sized."""
import json

from tokenstretch import cli
from tokenstretch.cost import startup_price
from tokenstretch.startup import agent_profile, format_agent

MODEL = "claude-fable-5-2"


def _entry(**over):
    d = {"adapter": "claude-code", "name": "Claude Code", "measured_start": 76_000, "known": 9_000, "rest": 67_000,
         "price": startup_price(76_000, MODEL),
         "parts": [
             {"kind": "instruction file", "name": "BIG.md", "tokens": 4_000},
             {"kind": "instruction file", "name": "small.md", "tokens": 200},
             {"kind": "skill listing (user)", "name": "deploy", "tokens": 600},
             {"kind": "helper listing (user)", "name": "reviewer", "tokens": 400},
             {"kind": "connector (measured by difference)", "name": "mail (user)", "tokens": 4_000},
         ],
         "unsized": [{"kind": "connector", "name": "crm (user)", "tokens": None, "fix": ""}]}
    d.update(over)
    return d


def test_profile_lists_switch_offs_and_saving():
    a = agent_profile(_entry())
    names = {i["name"] for i in a["items"]}
    assert names == {"BIG.md", "deploy", "reviewer", "mail (user)", "crm (user)"}   # small file stays
    assert a["saved_tokens"] == 9_000 and a["before"] == 76_000 and a["after"] == 67_000
    assert abs(a["units_saved"] - (startup_price(76_000, MODEL)["units"] - startup_price(67_000, MODEL)["units"])) < 1e-6
    assert a["steps"] == 50 and a["model"] == MODEL
    assert any("--strict-mcp-config" in s for s in a["settings"])


def test_profile_without_price_or_start():
    a = agent_profile(_entry(measured_start=None, price=None, rest=None))
    assert a["before"] is None and a["units_saved"] is None
    text = "\n".join(format_agent([a]))
    assert "no before and after" in text


def test_report_text_says_estimate_and_suggestion():
    text = "\n".join(format_agent([agent_profile(_entry())]))
    assert "not a bill" in text and "nothing is written" in text and "units saved" in text


def test_cli_agent_json_writes_nothing(tmp_path, capsys):
    home, root = tmp_path / "h", tmp_path / "r"
    home.mkdir(), root.mkdir()
    assert cli.main(["startup", "--agent", "--json", "--no-save", "--home", str(home), "--root", str(root)]) == 0
    assert isinstance(json.loads(capsys.readouterr().out), list)
    assert list(home.iterdir()) == []
