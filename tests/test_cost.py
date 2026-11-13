"""model cost: price-weighted units per model and effort, the price-scaled watch limits, and the plan preset."""
from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

from conftest import credential_guard
from test_watch_model import _ctx, _effort_world, _row, _watch_once, _world

from tokenstretch.brief import build, from_preset, lint, render
from tokenstretch.cli import main
from tokenstretch.cost import cost_view, format_cost_view, grid, rate, reread_weight, resaved, startup_price, units
from tokenstretch.statusline import build_line
from tokenstretch.watch import WatchConfig, price_scaled

TOP, MID = "claude-fable-5-2", "claude-sonnet-5-5"


def _args(w):
    return ["--home", str(w / "home"), "--root", str(w / "root"), "--sys-snapshot", str(w / "sys")]


def test_rates_match_by_version_and_unknown_models_are_not_priced():
    assert rate(TOP) == (4.0, 0.025) and rate("claude-fable-5-1") == (5.0, 0.025)
    assert rate("claude-opus-4-8") == (2.5, 0.1) and rate(MID) == (1.0, 0.05)
    assert rate("gpt-6-astra") is None and rate("") is None and rate("claude-opus-4-1") is None
    assert reread_weight(MID) == 1.0 and reread_weight(TOP) == 2.0 and reread_weight("claude-opus-4-8") == 5.0
    assert reread_weight("some-other-model") is None


def test_units_weigh_each_kind_of_token():
    u = units(TOP, new=100, reread=1000, write=100, resave=200, output=10)
    assert u == {"reread": 100.0, "resave": 1000.0, "new": 900.0, "output": 200.0}
    assert units("gpt-6-astra", new=5) is None
    assert startup_price(40_000, TOP)["units"] == 40_000 * 50 * 4.0 * 0.025
    assert startup_price(40_000, "gpt-6-astra") is None and startup_price(0, TOP) is None


def test_resaved_counts_only_later_steps_that_lost_their_cache():
    turns = [{"cache_read": 0, "cache_write": 50_000},        # first step: a first save, not a re-save
             {"cache_read": 50_000, "cache_write": 2_000},    # normal step
             {"cache_read": 0, "cache_write": 52_000},        # cache lapsed
             {"cache_read": 30_000, "cache_write": 25_000}]   # mostly re-read
    assert resaved(turns) == 52_000


def test_cost_view_groups_by_model_effort_and_kind_and_names_the_lever():
    sessions = [{
        "turns": [{"model": TOP, "effort": "max", "history_tokens": 100_000, "cache_read": 99_000, "cache_write": 1_000,
                   "output_tokens": 4_000} for _ in range(3)],
        "helpers": [{"model": MID, "effort": "low", "steps": 10, "sent": 500_000, "cache_read": 480_000,
                     "cache_write": 20_000, "output": 500}],
    }, {"turns": [{"model": "gpt-6-astra", "history_tokens": 1_000, "output_tokens": 10}]}]
    view = cost_view(sessions)
    top, helper, other = view["rows"]
    assert (top["model"], top["effort"], top["kind"], top["steps"]) == (TOP, "max", "main", 3)
    assert top["biggest"] == "output" and "ran at max" in top["lever"]
    assert round(top["total"]) == round(3 * (99_000 * 0.1 + 1_000 * 5.0 + 4_000 * 20.0))
    assert top["total"] / top["on_mid"] > 3
    assert helper["kind"] == "helper" and helper["biggest"] == "new" and helper["steps"] == 10
    assert other["priced"] is False and other["total"] == 0.0
    assert abs(sum(r["share"] for r in view["rows"]) - 1.0) < 1e-9
    text = "\n".join(format_cost_view(view))
    assert "not a bill" in text and "no price ratio for this model" in text and "the same tokens on the mid model" in text
    g = grid(view)
    assert [x["model"] for x in g] == [TOP, "gpt-6-astra"] and g[0]["times_cheapest"] == 1.0 and g[1]["units_per_step"] is None
    assert format_cost_view(cost_view([])) == ["No token counts found in the 0 newest conversations."]


def test_model_cost_reads_local_logs(tmp_path):
    w = _effort_world(tmp_path, "medium", "high")
    buf = io.StringIO()
    with redirect_stdout(buf), credential_guard():
        assert main(["model", "cost", *_args(w), "--json"]) == 0
    view = json.loads(buf.getvalue())
    kinds = {(r["kind"], r["effort"]): r for r in view["rows"]}
    assert kinds[("main", "medium")]["steps"] == 12 and kinds[("helper", "high")]["steps"] == 12
    assert kinds[("helper", "high")]["tokens"]["reread"] == 2 * 6 * 60_000
    for flags in ([], ["--grid"]):
        buf = io.StringIO()
        with redirect_stdout(buf), credential_guard():
            assert main(["model", "cost", *_args(w), *flags]) == 0
        assert "claude-sonnet-4-5" in buf.getvalue() and "not a bill" in buf.getvalue()


def test_price_scaled_lowers_a_limit_only_for_dearer_models():
    assert price_scaled(150_000, 2.0, 60_000) == 75_000
    assert price_scaled(150_000, 5.0, 60_000) == 60_000        # never below the floor
    assert price_scaled(150_000, 1.0, 60_000) == 150_000
    assert price_scaled(150_000, 0.1, 60_000) == 150_000       # a cheap model does not raise it
    assert price_scaled(150_000, None, 60_000) == 150_000
    assert price_scaled(50_000, 5.0, 60_000) == 50_000         # a limit under the floor stays as it is
    assert WatchConfig().limits(TOP)["context"] == 75_000
    assert WatchConfig(max_context=200_000).limits(TOP)["context"] == 200_000     # a limit you set stays as set
    assert WatchConfig(price_scale=False).limits(TOP)["context"] == 150_000


def test_watch_and_status_line_use_the_lower_limit_on_the_top_model(tmp_path):
    rows = lambda model: [_row(i, model, 100_000) for i in range(2)]    # noqa: E731
    top = _watch_once(_world(tmp_path / "top", rows(TOP)))
    assert "history is ~100K tokens (over 75K)" in top and "a re-read costs about 2x" in top
    assert "history is" not in _watch_once(_world(tmp_path / "mid", rows(MID)))
    w = _world(tmp_path / "line", rows(TOP))
    with credential_guard():
        assert "100K/75K history" in build_line(_ctx(w), {})


def test_watch_warns_when_the_conversation_itself_runs_long_at_a_top_effort(tmp_path):
    def world(name, effort, out):
        return _world(tmp_path / name, [{**_row(i, MID, 30_000, out=out), "effort": effort} for i in range(15)])
    hot = _watch_once(world("hot", "max", 5_000))
    assert "this conversation ran 15 steps at max effort" in hot and "~75K output tokens" in hot
    assert "steps at max effort" not in _watch_once(world("short", "max", 500))
    assert "effort and they wrote" not in _watch_once(world("medium", "medium", 5_000))


def test_plan_preset_writes_one_file_on_the_top_model_and_changes_nothing_else():
    b = from_preset("plan", task="plan the move of the quote form to its own module", files=["docs/plan.md"],
                    done="the plan lists numbered steps, each with files and a check")
    assert lint(b) == [] and b.no_build and b.max_reads == 12
    text = render(b)
    assert "- Write the plan to the file named above. Change nothing else" in text
    assert "small enough for a smaller model at a lower effort" in text
    assert "Reply in under 80 words: the plan file path" in text
    data = build(b)
    assert data["tier"] == "top" and data["effort"] == "high" and data["preset"] == "plan"
