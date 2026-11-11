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
    return {"tier": tier, "score": score, "reasons": reasons, "files": len(files), "words": words}


_PLAIN = {
    "light": "a small, mechanical change; the light model is usually enough",
    "mid": "everyday coding work; the mid model handles most of it",
    "top": "hard reasoning across many parts; worth the top model for this task only",
}


def tool_picks(ctx: Context, tier: str, adapters=None) -> list[dict]:
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
                    "switch": ad.model_switch_hint(model) if model else ad.model_switch_hint(""),
                    "supported": bool(tiers) or bool(ad.model_switch_hint(""))})
    return out


def format_suggestion(res: dict, picks: list[dict]) -> list[str]:
    lines = [f"Suggested tier: {res['tier'].upper()}: {_PLAIN[res['tier']]}.",
             "Why: " + "; ".join(res["reasons"]) + f" (score {res['score']:+d}).", ""]
    for p in picks:
        label = p["tool"] + ("" if p["detected"] else " (not detected; example)")
        if p["model"]:
            lines.append(f"  {label}: {p['model']}  (switch now: `{p['switch']}`)")
        elif p["switch"]:
            lines.append(f"  {label}: no tier names known yet; pick a {res['tier']} model with `{p['switch']}`")
        else:
            lines.append(f"  {label}: model picker not supported yet")
    lines += ["", "This is a keyword-and-size rule of thumb, not a measured result. "
              "Move up a tier if the answer comes back wrong."]
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
            out(f"{ad.display_name}: already on {ad.model_tiers()[tier]} (or its settings file is unreadable); nothing to change.")
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
