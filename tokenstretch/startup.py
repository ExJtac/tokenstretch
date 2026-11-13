"""`tokenstretch startup`: what a session carries before you type anything.

Every session starts with instruction files, skill and helper listings, and tool and connector
definitions. That block is re-sent on every step, and a fresh start does not remove it. This
command lists the parts that can be sized from local files, largest first, next to the measured
size of a recent session's first step, and says what to do about each part.

Core only. Adapters supply the parts (`startup_parts`) and the sessions (`watch_sessions`). Sizes
are bytes / 4, so they are estimates. No network, no model calls.

Connector tool lists can't be sized from local files, so they are sized by difference. Each run
remembers the unsized remainder of the measured start for the current set of connectors, in one
small file under the state directory (names and token counts only). When an earlier run saw the
same set with one connector more or one fewer, the change in the remainder is that connector's
size. `--no-save` keeps the run read-only.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .context import Context
from .identity import CLI_NAME, state_dir

HISTORY_KEEP = 30       # connector sets remembered per tool
DIFF_MIN = 2_000        # [reasoning] starts of like sessions differ by a few K; a smaller change is noise


def _k(n: int) -> str:
    return f"{n / 1000:.1f}K" if n < 10_000 else f"{round(n / 1000)}K"


def measured_starts(ctx: Context, ad, limit: int = 5) -> list[int]:
    """First-step sizes of recent sessions that recorded one, newest first."""
    try:
        sessions = ad.watch_sessions(ctx, limit=12, max_age_s=None) or []
    except Exception:  # noqa: BLE001 - a missing or odd log must not break the report
        return []
    return [int(s["start_tokens"]) for s in sessions if s.get("start_tokens")][:limit]


def newest_model(ctx: Context, ad) -> str:
    """The model the newest session with a measured start ran on, or "" when the log does not say."""
    try:
        sessions = ad.watch_sessions(ctx, limit=12, max_age_s=None) or []
    except Exception:  # noqa: BLE001 - a missing or odd log must not break the report
        return ""
    for s in sessions:
        if s.get("start_tokens") and s.get("turns"):
            return str(s["turns"][0].get("model") or "")
    return ""


def _history_file(home: Path) -> Path:
    return state_dir(home) / "startup-history.json"


def load_history(home: Path) -> dict:
    try:
        data = json.loads(_history_file(home).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_history(home: Path, history: dict) -> None:
    try:
        path = _history_file(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history, indent=1, sort_keys=True), encoding="utf-8")
    except OSError:
        pass  # the report still prints; only the next run's comparison is lost


def size_by_difference(seen: dict, connectors: list[str], rest: int) -> tuple[dict[str, int], dict[str, int]]:
    """(sizes of connectors that are on, sizes of connectors turned off since an earlier run).

    `seen` maps a connector set to the remainder measured with it. A set that differs from the
    current one by exactly one connector gives that connector's size."""
    now = set(connectors)
    on: dict[str, int] = {}
    off: dict[str, int] = {}
    for snap in sorted(seen.values(), key=lambda v: v.get("ts") or 0):   # newer runs win
        try:
            then, then_rest = set(snap["connectors"]), int(snap["rest"])
        except (KeyError, TypeError, ValueError):
            continue
        if len(now - then) == 1 and then < now and rest - then_rest >= DIFF_MIN:
            on[next(iter(now - then))] = rest - then_rest
        elif len(then - now) == 1 and now < then and then_rest - rest >= DIFF_MIN:
            off[next(iter(then - now))] = then_rest - rest
    return on, off


def build(ctx: Context, adapters=None, remember: bool = False, now: float | None = None) -> list[dict]:
    from .setup import detected_adapters
    out = []
    history = load_history(ctx.home)
    changed = False
    for ad in adapters if adapters is not None else detected_adapters(ctx):
        parts = ad.startup_parts(ctx)
        if parts is None:
            continue
        starts = measured_starts(ctx, ad)
        start = starts[0] if starts else None
        turned_off: dict[str, int] = {}
        since_last = 0
        if start:
            connectors = sorted(p["name"] for p in parts if p["kind"] == "connector")
            raw_rest = max(0, start - sum(p["tokens"] for p in parts if p.get("tokens")))
            seen = history.get(ad.id) if isinstance(history.get(ad.id), dict) else {}
            on, turned_off = size_by_difference(seen, connectors, raw_rest)
            try:    # same local set as last time: a plugin or an app connector may have changed
                delta = raw_rest - int(seen["|".join(connectors)]["rest"])
            except (KeyError, TypeError, ValueError):
                delta = 0
            since_last = delta if abs(delta) >= DIFF_MIN else 0
            for p in parts:
                if p["kind"] == "connector" and p["name"] in on:
                    p.update(tokens=on[p["name"]], kind="connector (measured by difference)")
            if remember:
                seen["|".join(connectors)] = {"connectors": connectors, "rest": raw_rest,
                                              "ts": time.time() if now is None else now}
                keep = sorted(seen.items(), key=lambda kv: -(kv[1].get("ts") or 0))[:HISTORY_KEEP]
                history[ad.id] = dict(keep)
                changed = True
        from .cost import startup_price
        price = startup_price(start, newest_model(ctx, ad)) if start else None
        sized = sorted((p for p in parts if p.get("tokens")), key=lambda p: -p["tokens"])
        unsized = [p for p in parts if not p.get("tokens")]
        known = sum(p["tokens"] for p in sized)
        out.append({
            "adapter": ad.id, "name": ad.display_name, "measured_start": start, "recent_starts": starts, "known": known,
            "rest": max(0, start - known) if start else None, "parts": sized, "unsized": unsized,
            "since_last": since_last, "price": price,
            "turned_off": [{"name": n, "tokens": t} for n, t in sorted(turned_off.items(), key=lambda kv: -kv[1])],
        })
    if changed:
        save_history(ctx.home, history)
    return out


def format_report(data: list[dict], top: int = 12) -> list[str]:
    if not data:
        return ["No supported tool found, so there is nothing to size."]
    out: list[str] = []
    for d in data:
        out.append(f"{d['name']}: what every session carries before you type")
        if d["measured_start"]:
            out.append(f"  Measured: the newest session's first step was ~{_k(d['measured_start'])} tokens."
                       + (f" Recent sessions: {', '.join(_k(n) for n in d['recent_starts'])}."
                          if len(d["recent_starts"]) > 1 else ""))
            pr = d.get("price")
            if pr:
                dearer = (f", {pr['weight']:g}x what the mid model pays for the same block"
                          if pr["weight"] and pr["weight"] > 1 else "")
                out.append(f"  Price: on {pr['model']}, {pr['steps']} steps re-read this block {pr['steps']} times. "
                           f"That costs about as much as ~{_k(round(pr['units']))} new input tokens on the mid "
                           f"model{dearer}. An estimate from published price ratios, not a bill.")
        else:
            out.append("  Measured: no recent session with a clean first step was found.")
        out.append(f"  Sized from local files: ~{_k(d['known'])} tokens (approx., bytes / 4).")
        for p in d["parts"][:top]:
            out.append(f"    ~{_k(p['tokens']):>6}  {p['kind']}: {p['name']}")
            if p.get("fix"):
                out.append(f"             {p['fix']}")
        if len(d["parts"]) > top:
            more = d["parts"][top:]
            out.append(f"    ~{_k(sum(p['tokens'] for p in more)):>6}  {len(more)} smaller parts (--all lists them)")
        if d["rest"] is not None:
            out.append(f"  The rest, ~{_k(d['rest'])} tokens, can't be sized from local files: the tool's own "
                       "system prompt and built-in tools, connector tool lists, and plugin listings.")
            out.append("  To size a connector: turn it off, start a new session, and run this again. "
                       "This command remembers the last run and shows the difference as that connector's size.")
        for t in d.get("turned_off") or []:
            out.append(f"  Since an earlier run, {t['name']} is off: sessions now start ~{_k(t['tokens'])} tokens lighter.")
        if d.get("since_last"):
            way = "lighter" if d["since_last"] < 0 else "heavier"
            out.append(f"  Since the last run, sessions start ~{_k(abs(d['since_last']))} tokens {way}, with the same "
                       "local files and connectors. A plugin or an app connector changed.")
        for p in d["unsized"]:
            out.append(f"    size unknown  {p['kind']}: {p['name']}" + (f". {p['fix']}" if p.get("fix") else ""))
        out.append("")
    out.append(f"This block is re-sent on every step. `{CLI_NAME} trim` shortens instruction files; "
               "the other fixes are listed with each part.")
    return out


# --- worker agent profile ---------------------------------------------------------------------
# A worker session does one narrow coding task from a brief. It needs the tool's built-ins and the
# brief, not the lead's connectors, skill and helper listings, plugins or long instruction files.
AGENT_BIG_INSTRUCTION = 1_000      # an instruction file this big (tokens) is worth leaving out of a worker
AGENT_STEPS = 50


def agent_profile(d: dict, steps: int = AGENT_STEPS) -> dict:
    """What a worker session on this tool can switch off, from one `build()` entry. Nothing is written."""
    from .cost import startup_price
    items = []
    for p in d["parts"]:
        kind = p["kind"]
        if kind.startswith("connector"):
            action = "connector: leave it off for workers"
        elif kind.startswith("skill listing"):
            action = "skill: hide it from workers"
        elif kind.startswith("helper listing"):
            action = "helper: a worker does not delegate"
        elif kind == "instruction file" and p["tokens"] >= AGENT_BIG_INSTRUCTION:
            action = "large instruction file: the brief carries what a narrow task needs"
        else:
            continue
        items.append({"kind": kind, "name": p["name"], "tokens": p["tokens"], "action": action})
    for p in d["unsized"]:
        if p["kind"].startswith("connector"):
            items.append({"kind": p["kind"], "name": p["name"], "tokens": None,
                          "action": "connector: leave it off for workers (size unknown; see the note below)"})
    saved = sum(i["tokens"] for i in items if i["tokens"])
    before = d.get("measured_start")
    after = max(0, before - saved) if before else None
    model = (d.get("price") or {}).get("model") or ""
    pb = startup_price(before, model, steps) if before and model else None
    pa = startup_price(after, model, steps) if after and model else None
    return {
        "adapter": d["adapter"], "name": d["name"], "items": items, "saved_tokens": saved, "before": before,
        "after": after, "steps": steps, "model": model or None,
        "units_before": pb["units"] if pb else None, "units_after": pa["units"] if pa else None,
        "units_saved": (pb["units"] - pa["units"]) if pb and pa else None,
        "unsized_rest": d.get("rest"), "settings": agent_settings(d["adapter"], items),
    }


def agent_settings(adapter: str, items: list[dict]) -> list[str]:
    """Suggested settings for a worker. Only the Claude Code ones are exact; check them against your version."""
    if adapter != "claude-code":
        return ["Turn off connectors, plugins and unused skills for worker sessions in this tool's own settings."]
    out = ["Start workers with no connectors (flags as in Claude Code's CLI help; check them against your version):",
           "    claude --strict-mcp-config --mcp-config '{\"mcpServers\":{}}'",
           "Switch plugins off for worker projects, in that project's .claude/settings.json:",
           '    {"enabledPlugins": {"<plugin>@<marketplace>": false}}']
    if any(i["kind"].startswith("skill listing") for i in items):
        out += ["Hide skills a worker never runs, in each SKILL.md frontmatter:", "    disable-model-invocation: true"]
    if any(i["kind"] == "instruction file" for i in items):
        out.append("Keep large instruction files out of worker projects (a worker's brief carries the task), "
                   f"or shorten them with `{CLI_NAME} trim`.")
    return out


def format_agent(profiles: list[dict]) -> list[str]:
    if not profiles:
        return ["No supported tool found, so there is nothing to size."]
    out: list[str] = []
    for a in profiles:
        out.append(f"{a['name']}: lean profile for a worker agent session (a suggestion; nothing is written)")
        if a["items"]:
            out.append("  Switch off for a worker:")
            for i in a["items"]:
                size = f"~{_k(i['tokens']):>6}" if i["tokens"] else "  size ?"
                out.append(f"    {size}  {i['kind']}: {i['name']}  ({i['action']})")
        else:
            out.append("  No sized part is worth switching off.")
        if a["saved_tokens"]:
            out.append(f"  Saves ~{_k(a['saved_tokens'])} tokens per step from the parts sized above.")
        if a["before"]:
            out.append(f"  Start-up size: ~{_k(a['before'])} tokens now, ~{_k(a['after'])} after.")
        else:
            out.append("  Start-up size: no recent session with a clean first step, so no before and after.")
        if a["units_saved"]:
            out.append(f"  Over {a['steps']} steps on {a['model']}: ~{_k(round(a['units_before']))} units now, "
                       f"~{_k(round(a['units_after']))} after, so ~{_k(round(a['units_saved']))} units saved. "
                       "Units = new input tokens on the mid model; an estimate, not a bill.")
        elif a["before"] and not a["model"]:
            out.append("  No price ratio for the newest session's model, so no unit saving is shown.")
        if a["unsized_rest"]:
            out.append(f"  ~{_k(a['unsized_rest'])} tokens can't be sized from local files (system prompt, built-ins, "
                       "plugin listings, connector tool lists). The saving above is a floor: after you apply the "
                       "settings, start a worker and run `startup` again to see the measured size.")
        out += ["  Settings to use:"] + [f"    {line}" if not line.startswith("    ") else f"  {line}" for line in a["settings"]]
        out.append("")
    return out
