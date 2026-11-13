"""pace auto: the status line stores the plan limits a tool reports, only when switched on."""
import io
import json

from tokenstretch import pace as pc
from tokenstretch import paceauto
from tokenstretch.cli import main
from tokenstretch.statusline import run_statusline

NOW = 1_800_000_000.0
PAYLOAD = {"rate_limits": {"five_hour": {"used_percentage": 23.5, "resets_at": NOW + 3600},
                           "seven_day": {"used_percentage": 41.2, "resets_at": NOW + 3 * 86400}}}


def _on(home):
    assert main(["pace", "auto", "--home", str(home)]) == 0


def test_off_by_default_writes_nothing(tmp_path):
    assert paceauto.record(tmp_path, PAYLOAD, NOW) == []
    assert not pc.state_path(tmp_path).exists()
    assert run_statusline(tmp_path, tmp_path, "subscription", stdin=io.StringIO(json.dumps(PAYLOAD)),
                          out=lambda s: None) == 0
    assert not pc.state_path(tmp_path).exists()


def test_on_stores_both_windows_and_only_percent_and_reset(tmp_path):
    _on(tmp_path)
    assert paceauto.record(tmp_path, PAYLOAD, NOW) == ["auto-5h", "auto-week"]
    st = pc.load_state(tmp_path)
    w5, w7 = st["windows"]["auto-5h"], st["windows"]["auto-week"]
    assert (w5["length_s"], w7["length_s"]) == (5 * 3600, 7 * 86400)
    assert w5["readings"] == [{"used": 23.5, "at": pc.iso(NOW)}] and w7["resets"] == pc.iso(NOW + 3 * 86400)
    assert w5["models"] is None and set(w5) == {"models", "length_s", "resets", "readings"}


def test_throttle_new_reading_only_on_a_point_or_half_an_hour(tmp_path):
    _on(tmp_path)
    paceauto.record(tmp_path, PAYLOAD, NOW)
    assert paceauto.record(tmp_path, PAYLOAD, NOW + 60) == []
    up = {"rate_limits": {"seven_day": {"used_percentage": 42.3, "resets_at": NOW + 3 * 86400}}}
    assert paceauto.record(tmp_path, up, NOW + 120) == ["auto-week"]
    assert paceauto.record(tmp_path, up, NOW + 120 + paceauto.MIN_GAP_S) == ["auto-week"]
    assert len(pc.load_state(tmp_path)["windows"]["auto-week"]["readings"]) == 3


def test_new_period_restarts_and_bad_or_past_values_are_ignored(tmp_path):
    _on(tmp_path)
    paceauto.record(tmp_path, PAYLOAD, NOW)
    nxt = {"rate_limits": {"five_hour": {"used_percentage": 2, "resets_at": NOW + 6 * 3600}}}
    assert paceauto.record(tmp_path, nxt, NOW + 4000) == ["auto-5h"]
    assert pc.load_state(tmp_path)["windows"]["auto-5h"]["readings"] == [{"used": 2.0, "at": pc.iso(NOW + 4000)}]
    for bad in ({}, {"rate_limits": 3}, {"rate_limits": {"five_hour": {"used_percentage": "9", "resets_at": NOW + 9}}},
                {"rate_limits": {"five_hour": {"used_percentage": 140, "resets_at": NOW + 9000}}},
                {"rate_limits": {"seven_day": {"used_percentage": True, "resets_at": NOW + 9000}}},
                {"rate_limits": {"seven_day": {"used_percentage": 50, "resets_at": NOW - 5}}}):
        assert paceauto.record(tmp_path, bad, NOW + 5000) == []


def test_status_line_stores_when_on_and_off_switch_stops_it(tmp_path, capsys):
    import time
    _on(tmp_path)
    live = {"rate_limits": {"seven_day": {"used_percentage": 12, "resets_at": time.time() + 86400}}}
    assert run_statusline(tmp_path, tmp_path, "subscription", stdin=io.StringIO(json.dumps(live)),
                          out=lambda s: None) == 0
    assert pc.load_state(tmp_path)["windows"]["auto-week"]["readings"][0]["used"] == 12.0
    assert main(["pace", "auto", "--off", "--home", str(tmp_path)]) == 0
    assert "stores nothing" in capsys.readouterr().out
    live["rate_limits"]["seven_day"]["used_percentage"] = 30
    run_statusline(tmp_path, tmp_path, "subscription", stdin=io.StringIO(json.dumps(live)), out=lambda s: None)
    assert len(pc.load_state(tmp_path)["windows"]["auto-week"]["readings"]) == 1
    assert main(["pace", "--gate", "--home", str(tmp_path), "--root", str(tmp_path)]) in (0, 3)
