"""`tokenstretch brief`: turn a one-line task into a brief that keeps an agent or helper cheap.

Core only, local rules, no model calls, no network. It reads only the text you pass in; it never
reads your session logs or past prompts.

Why a brief matters: a helper is its own conversation. It re-sends its whole history on every
step, so every extra search and every re-read is paid for again on each later step. The brief
names the files to work in, the notes to work from, what not to do again, when to stop, and how
short the reply must be. `lint` says which of those the task text is missing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .identity import CLI_NAME
from .models import suggest_tier

# A path or file name: has a slash, or ends in a short extension.
PATH_RE = re.compile(r"(?<![\w/.-])(?:[\w.-]+/)+[\w.-]+|(?<![\w/.-])[\w-]+\.[A-Za-z]{1,5}(?![\w/])")
DONE_RE = re.compile(r"\b(done (when|means)|until|so that|should (print|pass|return|show)|must (print|pass|return)|"
                     r"passes|acceptance|expected (output|result))\b", re.I)
VAGUE_RE = re.compile(r"\b(fix (it|this|that|the bug|the issue|things)|make it (work|better|nice)|clean ?up|improve|"
                      r"look into|check (it|this|everything)|take a look|sort (it|this) out|optimi[sz]e)\b", re.I)
JOIN_RE = re.compile(r"\b(and then|then also|and also|after that|as well as|plus also)\b|;\s*\S", re.I)
RESEARCH_RE = re.compile(r"\b(research|find out|look up|search|investigate|survey|compare options|who ranks)\b", re.I)


@dataclass
class Lint:
    code: str
    what: str
    why: str
    fix: str


@dataclass
class Brief:
    task: str
    files: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    done: str = ""
    reply_words: int = 80
    research: bool | None = None      # None = decide from the task text
    max_searches: int = 0             # 0 = no cap line
    max_fetches: int = 0              # 0 = no cap line
    no_build: bool = False
    helper: bool = False
    rules: list[str] = field(default_factory=list)   # your own rules, added after the built-in ones


def lint(b: Brief) -> list[Lint]:
    """What the task text leaves open, most costly first. Each item says why it costs credits."""
    out: list[Lint] = []
    text = b.task
    has_files = bool(b.files) or bool(PATH_RE.search(text))
    if not has_files:
        out.append(Lint("no-files", "no file or folder is named.",
                        "the agent has to explore to find where to work, and everything it opens stays in its "
                        "history and is re-sent on every later step.",
                        "name the files to change with --files (or in the task text)."))
    if not b.done and not DONE_RE.search(text):
        out.append(Lint("no-done", "nothing says when the task is finished.",
                        "without a stop condition the agent keeps checking and polishing, and each extra step "
                        "re-sends the whole history.",
                        'add --done "tests pass" or "the audit prints NO ISSUES".'))
    if VAGUE_RE.search(text):
        out.append(Lint("vague", f'the wording is open-ended ("{VAGUE_RE.search(text).group(0)}").',
                        "open wording invites broad reading and a wrong first attempt, which you pay for twice.",
                        "say what is wrong now and what it should do instead."))
    if JOIN_RE.search(text):
        out.append(Lint("several-tasks", "this reads like more than one task.",
                        "one conversation then carries the history of every task to the end.",
                        "give each task its own brief and a fresh session or helper."))
    wants_research = b.research if b.research is not None else bool(RESEARCH_RE.search(text))
    if wants_research and not b.max_searches:
        out.append(Lint("unbounded-research", "it asks for research with no limit.",
                        "search results are large and stay in history; an open-ended researcher can run dozens "
                        "of steps.",
                        "cap it with --max-searches N and say where findings should be written."))
    if not wants_research and not b.sources and not has_files and len(text.split()) < 12:
        out.append(Lint("no-sources", "no notes or reference file to work from.",
                        "without one the agent re-derives facts by reading or searching.",
                        "point to notes with --from FILE if you have them."))
    return out


def render(b: Brief) -> str:
    """The brief as plain text, ready to paste or to hand to a helper."""
    wants_research = b.research if b.research is not None else bool(RESEARCH_RE.search(b.task))
    lines = [f"Task: {b.task.strip()}", ""]
    if b.files:
        only = "this file" if len(b.files) == 1 else "these files"
        lines += [f"Work only in {only}: " + ", ".join(f"`{f}`" for f in b.files) + ". Do not change any other file.", ""]
    if b.sources:
        lines += ["Work from: " + ", ".join(f"`{s}`" for s in b.sources) +
                  ". Read each once. Open another file only when you need its exact lines.", ""]
    rules = []
    if wants_research:
        caps = [f"{n} {what}" for n, what in ((b.max_searches, "searches"), (b.max_fetches, "page fetches")) if n]
        if caps:
            rules.append("At most " + " and ".join(caps) + ".")
        rules.append("Write findings to the file as you go. Every fact gets its full URL and the date you read it.")
        rules.append("Mark each fact as read (you opened the page) or summary (search result only).")
        rules.append("Nothing from memory. If the allowed sources have nothing on a topic, list it as a gap; "
                     "do not skip it silently.")
    else:
        rules.append("No web search and no new research. If a fact is missing, say so in your reply; do not go looking.")
    rules.append("Search before you read, and read line ranges, not whole large files.")
    rules.append("Do not re-read a file you already read unless it changed.")
    if b.no_build:
        rules.append("Do not run builds, tests, or installs. The caller does that.")
    rules += [r.strip() for r in b.rules if r and r.strip()]
    lines += ["Rules:"] + [f"- {r}" for r in rules] + [""]
    if b.done:
        lines += [f"Done when: {b.done.strip()}", ""]
    who = "what you wrote" if b.helper else "what changed"
    lines.append(f"Reply in under {b.reply_words} words: {who}, file paths, and anything you needed and could not find. "
                 "No recap of the task.")
    return "\n".join(lines) + "\n"


def approx_tokens(text: str) -> int:
    return max(1, round(len(text) / 4))


def build(b: Brief) -> dict:
    text = render(b)
    tier = suggest_tier(b.task)
    return {
        "brief": text,
        "approx_tokens": approx_tokens(text),
        "lint": [{"code": x.code, "what": x.what, "why": x.why, "fix": x.fix} for x in lint(b)],
        "tier": tier.get("tier"),
        "tier_why": "; ".join(tier.get("reasons") or []),
    }


def format_report(data: dict) -> list[str]:
    out = [data["brief"].rstrip("\n"), "", f"(about {data['approx_tokens']} tokens, approx.; "
           f"suggested model tier: {data['tier']}, a rule of thumb from `{CLI_NAME} model suggest`)"]
    if data["lint"]:
        out += ["", f"Missing from this brief ({len(data['lint'])}):"]
        for x in data["lint"]:
            out += [f"  - {x['what']}", f"    Why it costs credits: {x['why']}", f"    Fix: {x['fix']}"]
    else:
        out += ["", "Nothing missing: files, stop condition, and limits are all stated."]
    return out
