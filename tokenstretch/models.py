"""`tokenstretch model`: a transparent, local heuristic model picker, and a backed-up default switch.

`suggest` scores a task description with keyword and size rules you can read below. It makes no
model calls and is a rule of thumb, not a measured claim. `default` plans a change through the
adapter (`plan_default_model`) and applies it with the same backup + manifest path as setup.
"""
from __future__ import annotations

import re
from typing import Callable

from .context import Context
from .identity import CLI_NAME

TIERS = ("light", "mid", "top")
# Reasoning effort, cheapest first. `max` is never suggested: it is a ceiling, not a default.
EFFORTS = ("low", "medium", "high", "xhigh")

# (pattern, label). Each hit moves the score by +2 (top) or -2 (light).
TOP_RULES = [
    (r"\barchitect(ure|ural)?\b|\bsystem design\b|\bdesign (a|the|an) (new )?(system|service|api|schema)", "architecture/design"),
    (r"\brefactor\w*\b.*\b(across|whole|entire|codebase|all|module|modules|layer)\b|\blarge refactor", "broad refactor"),
    (r"\bmigrat\w+\b|\brewrite\b|\bport (it |this |the )?(to|from)\b", "migration/rewrite"),
    (r"\brace condition|\bdeadlock|\bconcurren\w+|\bthread[- ]safe|\bmemory leak|\bheisenbug", "concurrency/leak bug"),
    (r"\bsecurity\b|\bvulnerab\w+|\bauth(entication|orization)? flow|\bcrypto\w*|\bxss\b|\binjection\b|\bcve\b", "security"),
    (r"\broot cause\b|\bintermittent\w*|\bflaky\b|\bnon-?deterministic|\bwhy does\b|\bonly in prod", "hard debugging"),
    (r"\bperformance\b|\boptimi[sz]\w+|\bprofil\w+|\bbottleneck", "performance"),
    (r"\bdistributed\b|\bconsensus\b|\bscal(e|ing|ability)\b", "distributed/scale"),
    (r"\bredesign\w*\b|\b(visual|ui|ux|page|web|site) design\b|\bdesign system\b|\blook and feel\b", "visual design"),
]
LIGHT_RULES = [
    (r"\brenam\w*\b", "rename"),
    (r"\btypo\w*|\bspelling|\bwording\b", "typo/wording"),
    (r"\bformat\w*\b|\blint\w*\b|\bprettier|\bblack\b|\bwhitespace|\bindent\w*", "formatting/lint"),
    (r"\bdocstring\w*|\bcomments?\b|\breadme\b|\bdocs?\b|\bdocumentation\b|\bchangelog\b", "docs/comments"),
    (r"\bbump\b|\bversion number\b|\bupdate (the )?(version|dependency|dep)\b", "version bump"),
    (r"\bsummar\w+|\bexplain what\b|\blist (the |all )?files\b|\bfind where\b", "lookup/summary"),
]
# Each hit moves the score by -1: usually mid-tier work.
MID_RULES = [
    (r"\b(unit |integration )?tests?\b", "tests"),
    (r"\bsmall (fix|change|bug)|\bquick fix|\bone[- ]line|\bsimple\b", "small fix"),
]
FILE_RE = re.compile(r"(?<![\w/.-])(?:[\w.-]+/)*[\w-]+\.(?:py|js|ts|tsx|jsx|go|rs|rb|java|kt|swift|c|h|cpp|cs|php|"
                     r"md|json|toml|ya?ml|sql|sh|html|css|scss|vue|svelte)\b")


def suggest_tier(text: str) -> dict:
    """Return {tier, score, reasons, files}. Score >= 2 → top, <= -2 → light, else mid."""
    t = (text or "").lower()
    score, reasons = 0, []
    for pat, label in TOP_RULES:
        if re.search(pat, t):
            score += 2
            reasons.append(f"+2 mentions {label}")
    for pat, label in LIGHT_RULES:
        if re.search(pat, t):
            score -= 2
            reasons.append(f"-2 mentions {label}")
    for pat, label in MID_RULES:
        if re.search(pat, t):
            score -= 1
            reasons.append(f"-1 mentions {label}")
    files = sorted(set(FILE_RE.findall(text or "")))
    if len(files) >= 5:
        score += 1
        reasons.append(f"+1 names {len(files)} files")
    words = len(t.split())
    if words > 150:
        score += 1
        reasons.append(f"+1 long description ({words} words)")
    elif words <= 12 and score <= 0 and len(files) <= 1:
        score -= 1
        reasons.append(f"-1 short, single-target ask ({words} words)")
    tier = "top" if score >= 2 else "light" if score <= -2 else "mid"
    if not reasons:
        reasons.append("no strong signals, so the everyday mid tier")
    effort = suggest_effort(score)
    return {"tier": tier, "score": score, "reasons": reasons, "files": len(files), "words": words,
            "effort": effort, "effort_why": _EFFORT_PLAIN[effort]}


def suggest_effort(score: int) -> str:
    """Effort for a task score. <= -2 low, -1..1 medium, 2..3 high, >= 4 xhigh. Same score as the tier."""
    if score <= -2:
        return "low"
    if score <= 1:
        return "medium"
    if score <= 3:
        return "high"
    return "xhigh"


_EFFORT_PLAIN = {
    "low": "a mechanical change; more thinking adds cost here, not quality",
    "medium": "everyday work; enough thinking for most tasks",
    "high": "one hard signal; worth more thinking for this task",
    "xhigh": "several hard signals at once; for this task only, then step back down",
}


LOGGED_EFFORTS = EFFORTS + ("max",)


def effort_mix(sessions: list[dict]) -> dict:
    """Steps and output tokens per logged effort, main conversations and helpers apart.

    Reads only what a tool's own log records. A helper counts at the effort most of its steps used.
    """
    main: dict[str, dict] = {}
    helpers: dict[str, dict] = {}
    used = 0
    for s in sessions:
        seen = False
        for t in s.get("turns") or []:
            if t.get("effort") in LOGGED_EFFORTS:
                row = main.setdefault(t["effort"], {"steps": 0, "output": 0})
                row["steps"] += 1
                row["output"] += int(t.get("output_tokens") or 0)
                seen = True
        for h in s.get("helpers") or []:
            if h.get("effort") in LOGGED_EFFORTS:
                row = helpers.setdefault(h["effort"], {"steps": 0, "output": 0, "helpers": 0})
                row["steps"] += int(h.get("steps") or 0)
                row["output"] += int(h.get("output") or 0)
                row["helpers"] += 1
                seen = True
        used += 1 if seen else 0
    return {"sessions": used, "read": len(sessions), "main": main, "helpers": helpers,
            "advice": _effort_advice(main, helpers)}


def _effort_advice(main: dict, helpers: dict) -> list[str]:
    out = []
    steps = sum(r["steps"] for r in helpers.values())
    hot = sum(r["steps"] for e, r in helpers.items() if e in ("high", "xhigh", "max"))
    if steps and hot * 2 >= steps:
        out.append(f"{hot} of {steps} helper steps ran at high effort or above. Helpers that search, read or "
                   "write routine text rarely need it: give them a lower effort and keep the high one for the "
                   "hard step.")
    if "max" in main or "max" in helpers:
        out.append("Some steps ran at max. It is the dearest setting: use it for one hard task, then step back down.")
    both = set(main) | set(helpers)
    total = steps + sum(r["steps"] for r in main.values())
    if len(both) == 1 and total >= 20:
        only = next(iter(both))
        out.append(f"Every step ran at {only}, whatever the task. `{CLI_NAME} model suggest \"<task>\"` suggests "
                   "an effort per task: lower for mechanical work, higher for the hard part.")
    return out


def format_effort_mix(mix: dict) -> list[str]:
    if not mix["main"] and not mix["helpers"]:
        return [f"No reasoning effort is recorded in the {mix['read']} newest conversations.",
                "Claude Code writes it to its logs; older logs and other tools may not."]

    def line(label: str, rows: dict) -> str:
        total = sum(r["steps"] for r in rows.values()) or 1
        parts = [f"{e.upper()} {rows[e]['steps']} steps ({round(100 * rows[e]['steps'] / total)}%), "
                 f"{rows[e]['output']:,} output tokens" for e in LOGGED_EFFORTS if e in rows]
        return f"  {label}: " + ("; ".join(parts) if parts else "none recorded")

    n = mix["sessions"]
    lines = [f"Reasoning effort recorded in {n} recent conversation{'s' if n != 1 else ''} (local logs, read-only):",
             line("main conversations", mix["main"]), line("helpers", mix["helpers"])]
    if mix["advice"]:
        lines += [""] + mix["advice"]
    lines += ["", "Output tokens include thinking. The advice is a rule of thumb, not a measured saving."]
    return lines


_PLAIN = {
    "light": "a small, mechanical change; the light model is usually enough",
    "mid": "everyday coding work; the mid model handles most of it",
    "top": "hard reasoning across many parts; worth the top model for this task only",
}


def tool_picks(ctx: Context, tier: str, adapters=None, effort: str = "") -> list[dict]:
    """Concrete model per tool for this tier. Detected tools first; else tools with a tier map as examples."""
    from .adapters import REGISTRY
    from .setup import detected_adapters
    detected = [a for a in (adapters if adapters is not None else detected_adapters(ctx)) if a.id != "host"]
    pool = detected or [a for a in REGISTRY if a.model_tiers()]
    out = []
    for ad in pool:
        tiers = ad.model_tiers()
        model = tiers.get(tier)
        out.append({"adapter": ad.id, "tool": ad.display_name, "detected": bool(detected), "model": model,
                    "alternatives": list(ad.model_tier_alternatives().get(tier) or []) if model else [],
                    "note": ad.model_tier_note() if model else "",
                    "switch": ad.model_switch_hint(model) if model else ad.model_switch_hint(""),
                    "effort_hint": ad.effort_hint(effort) if effort else "",
                    "supported": bool(tiers) or bool(ad.model_switch_hint(""))})
    return out


def format_suggestion(res: dict, picks: list[dict]) -> list[str]:
    lines = [f"Suggested tier: {res['tier'].upper()}: {_PLAIN[res['tier']]}.",
             "Why: " + "; ".join(res["reasons"]) + f" (score {res['score']:+d}).",
             f"Suggested effort: {res['effort'].upper()}: {res['effort_why']}.", ""]
    for p in picks:
        label = p["tool"] + ("" if p["detected"] else " (not detected; example)")
        if p["model"]:
            alt = f" or {', '.join(p['alternatives'])}" if p.get("alternatives") else ""
            lines.append(f"  {label}: {p['model']}{alt}  (switch now: `{p['switch']}`)")
            if p.get("note"):
                lines.append(f"    ({p['note']})")
            if p.get("effort_hint"):
                lines.append(f"    effort: {p['effort_hint']}")
        elif p["switch"]:
            lines.append(f"  {label}: no tier names known yet; pick a {res['tier']} model with `{p['switch']}`")
        else:
            lines.append(f"  {label}: model picker not supported yet")
    lines += ["", "Pick the effort when a session starts: a change in the middle of a session re-sends "
              "the history at full price.",
              "This is a keyword-and-size rule of thumb, not a measured result. "
              "Move up a tier or an effort level if the answer comes back wrong."]
    return lines


def run_default(ctx: Context, tier: str, *, tool: str | None = None, apply: bool = False,
                out: Callable[[str], None] = print, adapters=None) -> int:
    from .installer import _now_stamp
    from .setup import apply_plan, detected_adapters, format_plan
    ads = [a for a in (adapters if adapters is not None else detected_adapters(ctx)) if a.id != "host"]
    if tool:
        ads = [a for a in ads if tool in (a.id, a.id.split("-")[0])]
        if not ads:
            out(f"{tool}: not detected on this machine.")
            return 2
    items = []
    for ad in ads:
        if not ad.model_tiers():
            out(f"{ad.display_name}: setting a default by tier is not supported yet.")
            continue
        item = ad.plan_default_model(ctx, tier)
        if item is None:
            out(f"{ad.display_name}: already on the {tier} tier ({ad.model_tiers()[tier]}), or its settings file "
                "can't be edited safely; nothing to change.")
            continue
        items.append(item)
    if not items:
        return 0
    for line in format_plan(ctx, items):
        out(line)
    if not apply:
        out(f"Dry run: nothing was changed. Add --apply to make this change (backed up; `{CLI_NAME} undo` restores it).")
        return 0
    run_id = _now_stamp()
    apply_plan(ctx, items, run_id, out)
    out(f"Done. To undo: `{CLI_NAME} undo`")
    return 0
