"""`tokenstretch trim --to-skills`: move workflow-style sections out of always-loaded Claude Code
instruction files (CLAUDE.md) into Claude Code skills, leaving a one-line pointer.

Core only. Deterministic, no model calls, section text is moved unchanged. Token counts are
approximate (characters / 4) and always labeled approx.

Safety: a section that looks like a standing rule (guard words such as "never", "always",
"private", "security") stays where it is, because a skill only loads when the assistant picks it.
Files are backed up and registered by `installer.apply_changes` (same manifest as `trim`), so
`tokenstretch undo` restores the instruction files and removes the skill files this run created.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .context import Context
from .identity import CLI_NAME, PRODUCT_NAME, VERSION
from .trim import (APPROX, _FENCE, _HEADING, _display, _exists, _fence_mask, _isatty, _under, approx_tokens,
                   credential_like, discover, parse_units, unified_diff)

DEFAULT_MIN_TOKENS = 150
SLUG_MAX = 48
DESC_MAX = 199

GUARD_WORDS = (
    "never", "always", "must", "do not", "don't", "don’t", "should not", "shouldn't", "shouldn’t",
    "non-negotiable", "secret", "credential", "private", "privacy", "security", "safety", "permission",
    "approv", "authoriz", "forbidden", "prohibit", "password", "api key", "api_key", "oauth",
)
_GUARD = re.compile(r"(?<![\w])(" + "|".join(re.escape(w) for w in GUARD_WORDS) + r")", re.I)
_WORKFLOW_HEADING = re.compile(
    r"how to|workflow|checklist|steps|procedure|runbook|release|deploy|set ?up|testing|debugging", re.I)
_NUMBERED = re.compile(r"^\s{0,3}\d{1,3}[.)]\s+\S")
_SHELL_LANGS = {"bash", "sh", "shell", "zsh", "console", "shell-session", "shellsession", "cmd", "powershell", "ps1"}
_COMMANDS = {"git", "npm", "npx", "pnpm", "yarn", "pip", "pip3", "python", "python3", "make", "docker", "curl",
             "cd", "cargo", "go", "pytest", "kubectl", "brew", "uv", "ssh", "scp", "rsync", "bash", "sh", "gh",
             "terraform", "node", "deno", "bun", "composer", "bundle", "rake", "mvn", "gradle", "sudo"}

LIMITS = ("Limits: skills load only when the assistant picks them from their one-line description, so a moved "
          "section is no longer guaranteed to be read. Rules that must hold every turn should stay in the "
          "instruction file. Review each description before you rely on it.")


@dataclass
class Section:
    start: int          # heading line index
    end: int            # one past the last non-blank line
    level: int
    heading: str
    lines: list[str]


def _heading_text(line: str) -> str:
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", line)
    t = re.sub(r"\s+#+\s*$", "", t)
    return re.sub(r"\s+", " ", t).strip()


def find_sections(lines: list[str], rules_start: str, rules_end: str) -> tuple[list[Section], set[int]]:
    """Every heading outside fenced code and the rules block, with its section. Also the rules line indexes."""
    mask = _fence_mask(lines)
    rules: set[int] = set()
    for u in parse_units(lines, rules_start, rules_end):
        if u.kind == "rules":
            rules.update(range(u.start, u.end))
    heads = []
    for i, line in enumerate(lines):
        if mask[i] or i in rules or not _HEADING.match(line):
            continue
        level = len(re.match(r"^\s{0,3}(#+)", line).group(1))
        heads.append((i, level))
    out = []
    for n, (i, level) in enumerate(heads):
        end = len(lines)
        for j, lv in heads[n + 1:]:
            if lv <= level:
                end = j
                break
        while end > i + 1 and not lines[end - 1].strip():
            end -= 1
        out.append(Section(i, end, level, _heading_text(lines[i]), lines[i:end]))
    return out, rules


def slugify(heading: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")[:SLUG_MAX].strip("-")
    return s


def guard_hit(text: str) -> str | None:
    m = _GUARD.search(text)
    return m.group(1).lower() if m else None


def _fences(lines: list[str]) -> list[tuple[str, list[str]]]:
    """(info string, body lines) for each fenced block in the lines."""
    out, i, n = [], 0, len(lines)
    while i < n:
        m = _FENCE.match(lines[i])
        if not m:
            i += 1
            continue
        mark = m.group(1)
        info = lines[i].strip()[len(mark):].strip().lower()
        j = i + 1
        body = []
        while j < n:
            c = _FENCE.match(lines[j])
            if c and c.group(1)[0] == mark[0] and len(c.group(1)) >= len(mark):
                break
            body.append(lines[j])
            j += 1
        out.append((info, body))
        i = j + 1
    return out


def _has_commands(lines: list[str]) -> bool:
    for info, body in _fences(lines):
        if info.split(" ")[0].split("{")[0] in _SHELL_LANGS:
            return True
        for l in body:
            s = l.strip()
            if s.startswith("$ ") or (s.split(" ")[0] in _COMMANDS and len(s.split()) > 1):
                return True
    return False


def looks_like_procedure(sec: Section) -> bool:
    mask = _fence_mask(sec.lines)
    steps = sum(1 for i, l in enumerate(sec.lines) if not mask[i] and _NUMBERED.match(l))
    return (steps >= 3 or _has_commands(sec.lines[1:]) or bool(_WORKFLOW_HEADING.search(sec.heading)))


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[`*_\x00-\x1f]", "", text)).strip()


def make_description(heading: str, body: list[str], display: str) -> str:
    head = _clean(heading).rstrip(". ")
    tail = f"Use when the task involves {head.lower()}. Moved from {display} by {PRODUCT_NAME}."
    first = ""
    mask = _fence_mask(body)
    for i, l in enumerate(body):
        s = l.strip()
        if not s or mask[i] or _HEADING.match(l) or s.startswith(("|", "-", "*", "+", ">")) or _NUMBERED.match(l):
            continue
        first = re.split(r"(?<=[.!?])\s", _clean(s))[0].rstrip(". ")
        break
    for cand in ((f"{head}. {first}. {tail}" if 8 <= len(first) <= 90 else ""), f"{head}. {tail}"):
        if cand and len(cand) <= DESC_MAX:
            return cand
    room = DESC_MAX - len(f". {tail}")
    return f"{head[:max(room, 10)].rstrip()}. {tail}"[:DESC_MAX]


def skill_text(slug: str, description: str, section: list[str]) -> str:
    q = description.replace("\\", "\\\\").replace('"', '\\"')
    return f'---\nname: {slug}\ndescription: "{q}"\n---\n\n' + "\n".join(section) + "\n"


def _tilde(path: Path, ctx: Context) -> str:
    try:
        return "~/" + path.relative_to(ctx.home).as_posix()
    except ValueError:
        return str(path)


def _show_skill(path: Path, ctx: Context) -> str:
    try:
        return path.relative_to(ctx.root).as_posix()
    except ValueError:
        return _tilde(path, ctx)


@dataclass
class SkillPlan:
    moves: list[dict]
    kept: list[dict]
    skipped_files: list[dict]
    changes: list[dict]
    removed: int
    pointers: int
    listings: int
    note: str = ""

    @property
    def net(self) -> int:
        return self.removed - self.pointers - self.listings


def _eligible(f, ctx: Context) -> str | None:
    """None when the file may be scanned, else the reason it is skipped (empty string: skip silently)."""
    if "claude-code" not in f.tools or f.real.name == "MEMORY.md":
        return ""
    if f.text is None or not f.editable:
        return f.note or "not opened"
    if not (_under(f.real, ctx.root) or f.scope == "user"):
        return "outside the project and ~/.claude"
    return None


def plan_skills(ctx: Context, min_tokens: int = DEFAULT_MIN_TOKENS, adapters=None) -> SkillPlan:
    from .adapters.base import RULES_END, RULES_START
    moves: list[dict] = []
    kept: list[dict] = []
    skipped: list[dict] = []
    changes: list[dict] = []
    creates: list[dict] = []
    used_dirs: set[Path] = set()
    for f in discover(ctx, adapters):
        why = _eligible(f, ctx)
        if why is not None:
            if why:
                skipped.append({"file": f.display, "reason": why})
            continue
        in_project = f.scope != "user" and _under(f.real, ctx.root)
        base = (ctx.root if in_project else ctx.home) / ".claude" / "skills"
        lines = f.lines()
        secs, rules = find_sections(lines, RULES_START, RULES_END)
        first_ln = next((i for i, l in enumerate(lines) if l.strip()), 0)
        last_ln = max((i for i, l in enumerate(lines) if l.strip()), default=-1) + 1
        chosen: list[tuple[Section, dict]] = []
        taken_to = -1
        for sec in secs:
            if sec.start < taken_to or sec.level == 1:
                continue
            body = "\n".join(sec.lines[1:])
            body_tokens = approx_tokens(body)
            if body_tokens < min_tokens or not looks_like_procedure(sec):
                continue
            label = {"file": f.display, "heading": sec.heading, "tokens": approx_tokens("\n".join(sec.lines))}
            others = [n for t, n in zip(f.tools, f.tool_names) if t != "claude-code"]
            if any(sec.start <= r < sec.end for r in rules):
                kept.append({**label, "reason": f"contains the {PRODUCT_NAME} rules block"})
                continue
            hit = guard_hit("\n".join(sec.lines))
            if hit:
                kept.append({**label, "reason": f"looks like a standing rule: contains \"{hit}\""})
                continue
            if sec.start == first_ln and sec.end == last_ln:
                kept.append({**label, "reason": "it is the whole file"})
                continue
            slug = slugify(sec.heading)
            if not slug or credential_like(slug):
                kept.append({**label, "reason": "heading gives no usable skill name"})
                continue
            sdir = base / slug
            if sdir in used_dirs or _exists(str(sdir)):
                kept.append({**label, "reason": f"skill folder {_show_skill(sdir, ctx)} already exists; not overwritten"})
                continue
            desc = make_description(sec.heading, sec.lines[1:], f.display)
            pointer = f"- {sec.heading}: use the `{slug}` skill."
            mv = {**label, "slug": slug, "description": desc, "pointer": pointer,
                  "skill_path": str(sdir / "SKILL.md"), "skill_display": _show_skill(sdir / "SKILL.md", ctx),
                  "removed": approx_tokens("\n".join(sec.lines)), "pointer_tokens": approx_tokens(pointer),
                  "listing_tokens": approx_tokens(f"{slug} {desc}"), "also_loaded_by": others, "_sec": sec}
            if mv["removed"] - mv["pointer_tokens"] - mv["listing_tokens"] <= 0:
                kept.append({**label, "reason": "moving it adds more per turn than it removes"})
                continue
            used_dirs.add(sdir)
            taken_to = sec.end
            chosen.append((sec, mv))
            moves.append(mv)
            creates.append({"path": mv["skill_path"], "kind": "create", "display": mv["skill_display"],
                            "content": skill_text(slug, desc, sec.lines), "key": "skill",
                            "reason": f"{sec.heading} moved out of {f.display}"})
        if not chosen:
            continue
        new: list[str] = []
        i, byst = 0, {s.start: (s, mv) for s, mv in chosen}
        while i < len(lines):
            if i in byst:
                s, mv = byst[i]
                if new and new[-1].strip():
                    new.append("")
                new.append(mv["pointer"])
                if s.end < len(lines) and lines[s.end].strip():
                    new.append("")
                i = s.end
                continue
            new.append(lines[i])
            i += 1
        text = "\n".join(new)
        out = text.replace("\n", "\r\n") if f.nl == "\r\n" else text
        changes.append({"path": str(f.path), "kind": "modify", "content": out, "display": f.display,
                        "reason": f"trim --to-skills: moved {len(chosen)} section(s) to skills", "key": "instructions",
                        "before": f.text, "after": text,
                        "tokens_before": f.tokens, "tokens_after": approx_tokens(text)})
    plan = SkillPlan(moves, kept, skipped, [], sum(m["removed"] for m in moves),
                     sum(m["pointer_tokens"] for m in moves), sum(m["listing_tokens"] for m in moves))
    if moves and plan.net <= 0:
        plan.note = "The net change per turn is zero or negative, so nothing is proposed."
        plan.moves, plan.removed, plan.pointers, plan.listings = [], 0, 0, 0
        return plan
    if not moves and any("adds more per turn" in k["reason"] for k in kept):
        plan.note = "The net change per turn would be zero or negative, so nothing is proposed."
    plan.changes = creates + changes  # skills first: a pointer never exists without its skill
    return plan


def _public(mv: dict) -> dict:
    return {k: v for k, v in mv.items() if not k.startswith("_")}


def report_data(ctx: Context, plan: SkillPlan, min_tokens: int) -> dict:
    mods = [c for c in plan.changes if c["kind"] == "modify"]
    return {"product": PRODUCT_NAME, "version": VERSION, "mode": "dry-run", "approx": APPROX,
            "project": str(ctx.root), "min_tokens": min_tokens,
            "moves": [_public(m) for m in plan.moves], "kept": plan.kept, "skipped_files": plan.skipped_files,
            "net": {"removed": plan.removed, "pointers_added": plan.pointers, "skill_listings_added": plan.listings,
                    "net_per_turn": plan.net if plan.moves else 0},
            "note": plan.note, "limits": LIMITS, "diff": unified_diff(mods), "manifest": None}


def format_plan(data: dict, *, show_diff: bool = True) -> list[str]:
    out = [f"{PRODUCT_NAME} trim --to-skills: move workflow sections out of CLAUDE.md files into skills. "
           f"Token counts are approx. (characters / 4).", ""]
    if data["moves"]:
        out.append(f"Planned moves ({len(data['moves'])}):")
        for m in data["moves"]:
            out.append(f"  {m['file']}: \"{m['heading']}\" (~{m['tokens']:,} tokens approx.)")
            out.append(f"    -> {m['skill_display']}")
            out.append(f"    description: {m['description']}")
            if m.get("also_loaded_by"):
                out.append(f"    note: {', '.join(m['also_loaded_by'])} also reads this file and does not read Claude Code "
                           f"skills, so it will see only the pointer line.")
    else:
        out.append("Planned moves: none.")
    if data["kept"]:
        out += ["", "Kept in place:"]
        for k in data["kept"]:
            out.append(f"  {k['file']}: \"{k['heading']}\" (~{k['tokens']:,} tokens approx.): {k['reason']}")
    if data["skipped_files"]:
        out += ["", "Not scanned:"]
        for s in data["skipped_files"]:
            out.append(f"  {s['file']}: {s['reason']}")
    n = data["net"]
    out.append("")
    if data["moves"]:
        out.append(f"Per turn (approx.): ~{n['removed']:,} tokens removed, ~{n['pointers_added'] + n['skill_listings_added']:,} added "
                   f"(pointer lines ~{n['pointers_added']:,}, skill names and descriptions ~{n['skill_listings_added']:,}), "
                   f"net ~{n['net_per_turn']:,} fewer.")
    else:
        out.append(data["note"] or "Nothing to move: no section is long enough, procedure-like, and free of standing-rule wording.")
    out += ["", data["limits"]]
    if show_diff and data["diff"]:
        out += ["", data["diff"].rstrip("\n")]
    return out


def run_trim_skills(ctx: Context, min_tokens: int = DEFAULT_MIN_TOKENS, *, apply: bool = False, yes: bool = False,
                    as_json: bool = False, show_diff: bool = True, interactive: bool | None = None, input_fn=input,
                    out: Callable[[str], None] = print, adapters=None) -> int:
    from .installer import _now_stamp, apply_changes
    plan = plan_skills(ctx, min_tokens, adapters)
    data = report_data(ctx, plan, min_tokens)
    if interactive is None:
        interactive = _isatty()
    if not as_json:
        for line in format_plan(data, show_diff=show_diff):
            out(line)
    if not apply or not plan.changes:
        if not as_json:
            out("")
            out("Dry run: nothing was changed." + (
                f" Add --apply to make these edits (backed up; `{CLI_NAME} undo` restores them and removes the new skills)."
                if plan.changes else ""))
        else:
            data["mode"] = "dry-run" if plan.changes or not apply else "nothing"
            out(json.dumps(data, indent=2))
        return 0
    if not yes:
        if not interactive or as_json:
            msg = "No terminal to ask in, and no --yes: nothing was changed."
            if as_json:
                data["mode"] = "declined"
                data["message"] = msg
                out(json.dumps(data, indent=2))
            else:
                out("")
                out(msg)
            return 1
        out("")
        out(f"Move {len(plan.moves)} section(s) into skills? Edited files are backed up first. [y/N] ")
        try:
            answer = input_fn()
        except EOFError:
            answer = ""
        if answer.strip().lower() not in {"y", "yes"}:
            out("No changes made.")
            return 0
    run_id = _now_stamp()
    applied = apply_changes(ctx, [{k: ch[k] for k in ("path", "kind", "content", "reason", "key")} for ch in plan.changes],
                            stamp=f"{run_id}-trim", profile="trim",
                            extra_manifest={"setup_run": f"{run_id}-trim", "setup_item": "trim",
                                            "title": "Move instruction sections to skills"})
    data["mode"] = "applied"
    data["manifest"] = applied["manifest_path"]
    if as_json:
        out(json.dumps(data, indent=2))
        return 0
    out("")
    for rec in applied["manifest"]["files"]:
        verb = "backed up and trimmed" if rec.get("backup") else "created (undo deletes it)"
        out(f"✔ {verb} {_display(Path(rec['path']), ctx)}")
    out(data["limits"])
    out(f"Done. To undo: `{CLI_NAME} undo` (or `{CLI_NAME} undo {applied['manifest_path']}`).")
    return 0
