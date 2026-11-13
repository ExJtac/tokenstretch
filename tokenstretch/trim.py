"""`tokenstretch trim`: find waste in the instruction files agents resend on every turn, and
propose a trimmed version that only makes safe mechanical changes.

Core only: which files each tool loads comes from the adapters (`list_instruction_files`).
Deterministic and transparent: no model calls, no rewriting or summarizing of prose.
Token counts are approximate (characters / 4) and always labeled "approx.".

Safe changes the proposal may make:
  * remove an exact duplicate block (the first copy is kept; across files only when every tool
    that loads the later file also loads the file with the kept copy);
  * collapse repeated blank lines and strip trailing whitespace (outside fenced code);
  * with --move-blocks, move big fenced code blocks, pasted logs, and blobs out of project files
    into docs/agent-notes/<name>.md, leaving a one-line pointer.

Privacy: reads only the instruction files the adapters list that sit under the home or project
folder. The stale-path check only stats relative paths inside the project, never opens them,
and skips credential-looking names without touching them.
"""
from __future__ import annotations

import difflib
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .context import Context
from .identity import CLI_NAME, PRODUCT_NAME, VERSION

DEFAULT_MAX_TOKENS = 2000
DEFAULT_MAX_BLOCK_LINES = 30
DEFAULT_MAX_TABLE_ROWS = 20
LOG_MIN_LINES = 8
MIN_DUP_CHARS = 20
NEAR_MIN_CHARS = 60
NEAR_RATIO = 0.9
NEAR_MAX_UNITS = 1500
NEAR_BUDGET = 200_000     # cheap pair checks per run; near-duplicate search stops after this
APPROX = "approx. tokens = characters / 4"
NOTES_DIR = "docs/agent-notes"

SEVERITY_ORDER = {"high": 0, "med": 1, "low": 2, "info": 3}


def approx_tokens(text: str) -> int:
    return math.ceil(len(text) / 4) if text else 0


def _fmt(n: int) -> str:
    return f"{n:,}"


# ---- credential-looking names (generic; no tool-specific paths) --------------------------

_CRED_SUBSTR = ("credential", "secret", "passw", "oauth", "apikey", "api_key", "api-key", "id_rsa",
                "id_ed25519", "id_ecdsa", "keychain", "kubeconfig", "private_key", "private-key")
_CRED_NAMES = {"auth.json", "token", "token.json", "tokens.json", ".netrc", ".npmrc", ".pypirc", ".pgpass"}
_CRED_SUFFIX = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")


def credential_like(name_or_path: str) -> bool:
    for seg in re.split(r"[\\/]", str(name_or_path)):
        s = seg.lower()
        if not s:
            continue
        if s in _CRED_NAMES or s.startswith(".env") or s.endswith(_CRED_SUFFIX):
            return True
        if any(x in s for x in _CRED_SUBSTR):
            return True
    return False


def _exists(path: str) -> bool:
    """Stat-only presence check (never opens; does not follow a final symlink)."""
    return os.path.lexists(path)


# ---- parsing into blocks ------------------------------------------------------------------

_HEADING = re.compile(r"^\s{0,3}#{1,6}(\s|$)")
_BULLET = re.compile(r"^\s{0,3}(?:[-*+]|\d{1,3}[.)])\s+\S")
_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_LOG = re.compile(
    r"^\s*(?:\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}|\[?\d{2}:\d{2}:\d{2}|\[?(?:ERROR|WARN(?:ING)?|INFO|DEBUG|TRACE|FATAL)\b"
    r"|Traceback \(most recent call last\)|at [\w$.<>]+\(|File \".*\", line \d+|[\w.]+(?:Error|Exception)\b[:(]"
    r"|npm (?:ERR|WARN)!|E\s{2,}|\s*\^+\s*$)")
_B64 = re.compile(r"[A-Za-z0-9+/=_-]{200,}")


@dataclass
class Unit:
    kind: str          # heading | para | bullet | fence | table | rules
    start: int         # first line index
    end: int           # one past the last line
    lines: list[str]

    @property
    def key(self) -> str:
        return "\n".join(l.rstrip() for l in self.lines).strip()

    @property
    def near(self) -> str:
        text = "\n".join(re.sub(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+", "", l) for l in self.lines)
        return re.sub(r"[\W_]+", " ", text.lower()).strip()


def parse_units(lines: list[str], rules_start: str = "", rules_end: str = "") -> list[Unit]:
    units: list[Unit] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if rules_start and line.strip().startswith(rules_start):
            j = i + 1
            while j < n and not lines[j].strip().startswith(rules_end):
                j += 1
            j = min(j + 1, n)
            units.append(Unit("rules", i, j, lines[i:j]))
            i = j
            continue
        m = _FENCE.match(line)
        if m:
            mark = m.group(1)
            j = i + 1
            while j < n:
                c = _FENCE.match(lines[j])
                if c and c.group(1)[0] == mark[0] and len(c.group(1)) >= len(mark) and not lines[j].strip()[len(c.group(1)):].strip():
                    j += 1
                    break
                j += 1
            units.append(Unit("fence", i, j, lines[i:j]))
            i = j
            continue
        if _HEADING.match(line):
            units.append(Unit("heading", i, i + 1, [line]))
            i += 1
            continue
        if line.lstrip().startswith("|"):
            j = i
            while j < n and lines[j].lstrip().startswith("|"):
                j += 1
            units.append(Unit("table", i, j, lines[i:j]))
            i = j
            continue
        if _BULLET.match(line):
            j = i + 1
            while j < n and lines[j].strip() and lines[j][:1].isspace() and not _FENCE.match(lines[j]):
                j += 1
            units.append(Unit("bullet", i, j, lines[i:j]))
            i = j
            continue
        j = i + 1
        while j < n and lines[j].strip() and not (_HEADING.match(lines[j]) or _FENCE.match(lines[j])
                                                   or _BULLET.match(lines[j]) or lines[j].lstrip().startswith("|")
                                                   or (rules_start and lines[j].strip().startswith(rules_start))):
            j += 1
        units.append(Unit("para", i, j, lines[i:j]))
        i = j
    return units


def _fence_mask(lines: list[str]) -> list[bool]:
    """True for lines inside (or delimiting) a fenced code block."""
    out = [False] * len(lines)
    mark = None
    for i, line in enumerate(lines):
        m = _FENCE.match(line)
        if mark is None and m:
            mark = m.group(1)
            out[i] = True
        elif mark is not None:
            out[i] = True
            if m and m.group(1)[0] == mark[0] and len(m.group(1)) >= len(mark):
                mark = None
    return out


def _is_log(lines: list[str]) -> bool:
    body = [l for l in lines if l.strip() and not _FENCE.match(l)]
    if len(body) < LOG_MIN_LINES:
        return False
    return sum(1 for l in body if _LOG.match(l)) >= 0.6 * len(body)


def _blob_line(line: str) -> bool:
    if _B64.search(line):
        return True
    s = line.strip()
    return len(s) > 500 and s.count(" ") < len(s) * 0.02


# ---- stale path references -------------------------------------------------------------------

_TOKEN = re.compile(r"[\w./-]+")
_EXT = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,7}$")
_PLACEHOLDER = ("path/to/", "your/", "example/", "foo/", "some/")


def path_mentions(text: str) -> list[tuple[int, str]]:
    """(line number, relative path) for `dir/name.ext` mentions. Never URLs, absolute, or ~ paths."""
    out = []
    for ln, line in enumerate(text.split("\n"), 1):
        for m in _TOKEN.finditer(line):
            tok = m.group(0).rstrip(".")
            s = m.start()
            prev = line[s - 1] if s > 0 else ""
            if prev in "@~:\\$%{" or line[max(0, s - 3):s].endswith("://"):
                continue
            if "/" not in tok or tok.startswith("/") or "//" in tok or "..." in tok:
                continue
            if not _EXT.search(tok.rsplit("/", 1)[-1]):
                continue
            segs = [x for x in tok.split("/") if x not in ("", ".")]
            if not segs or any(x == ".." for x in segs):
                continue  # never look above the file's folder
            first = segs[0]
            if "." in first.lstrip("."):
                continue  # looks like a domain (example.com/x.md) or a file, not a folder
            if any(tok.lower().startswith(p) or f"/{p}" in tok.lower() for p in _PLACEHOLDER):
                continue
            out.append((ln, "/".join(segs)))
    return out


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def stale_paths(text: str, file_dir: Path, project: Path) -> list[tuple[int, str]]:
    root = os.path.normpath(str(project))
    out, seen = [], set()
    for ln, rel in path_mentions(text):
        if rel in seen:
            continue
        seen.add(rel)
        if credential_like(rel):
            continue  # skipped without being touched
        cands = [os.path.normpath(os.path.join(root, rel)), os.path.normpath(os.path.join(str(file_dir), rel))]
        cands = [c for c in dict.fromkeys(cands) if _inside(c, root)]
        if not cands:
            continue
        if not any(_exists(c) for c in cands):
            out.append((ln, rel))
    return out


# ---- discovery ---------------------------------------------------------------------------------

@dataclass
class IFile:
    path: Path                 # as listed
    real: Path                 # resolved
    scope: str
    tools: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    display: str = ""
    text: str | None = None    # None when not opened
    raw: bytes | None = None
    nl: str = "\n"
    editable: bool = False
    note: str = ""
    size: int = 0

    @property
    def tokens(self) -> int:
        return approx_tokens(self.text) if self.text is not None else math.ceil(self.size / 4)

    def lines(self) -> list[str]:
        return (self.text or "").split("\n")


def _display(path: Path, ctx: Context) -> str:
    for base, prefix in ((ctx.root, ""), (ctx.home, "~/")):
        try:
            rel = path.relative_to(base)
            return prefix + rel.as_posix() if prefix or str(rel) != "." else str(path)
        except ValueError:
            pass
        try:
            rel = path.resolve().relative_to(base.resolve())
            return prefix + rel.as_posix()
        except (ValueError, OSError):
            pass
    return str(path)


def _under(path: Path, base: Path) -> bool:
    try:
        b = base.resolve()
    except OSError:
        return False
    return path == b or b in path.parents


def discover(ctx: Context, adapters=None) -> list[IFile]:
    from .adapters import REGISTRY
    if adapters is None:
        adapters = [a for a in REGISTRY if a.id != "host" and a.detect(ctx)]
    files: dict[Path, IFile] = {}
    for ad in adapters:
        try:
            listed = ad.list_instruction_files(ctx)
        except Exception:  # one adapter's bad config must not stop the rest
            continue
        for f in listed or []:
            if not f.always_loaded:
                continue
            p = Path(f.path)
            try:
                real = p.resolve()
            except OSError:
                real = p
            item = files.get(real)
            if item is None:
                item = files[real] = IFile(path=p, real=real, scope=f.scope, size=f.bytes)
            if f.scope == "user":
                item.scope = "user"
            if ad.id not in item.tools:
                item.tools.append(ad.id)
                item.tool_names.append(ad.display_name)
    out = list(files.values())
    for item in out:
        item.display = _display(item.path, ctx)
        in_tree = _under(item.real, ctx.root) or _under(item.real, ctx.home)
        if credential_like(item.real.name):
            item.note = "credential-looking name: not opened"
            continue
        if not in_tree:
            item.note = "outside the home and project folders: counted from its size, not opened"
            continue
        try:
            raw = item.real.read_bytes()
        except OSError:
            item.note = "could not be read"
            continue
        item.raw = raw
        item.size = len(raw)
        try:
            text = raw.decode("utf-8")
            item.editable = True
        except UnicodeDecodeError:
            text = raw.decode("utf-8", errors="replace")
            item.note = "not UTF-8: analyzed, never edited"
        item.nl = "\r\n" if "\r\n" in text else "\n"
        item.text = text.replace("\r\n", "\n")
    # user files first (they load for every project), then project files, each in discovery order
    out.sort(key=lambda f: 0 if f.scope == "user" else 1)
    return out


# ---- analysis ----------------------------------------------------------------------------------

@dataclass
class TrimFinding:
    category: str      # duplicate | near-duplicate | long-file | big-block | stale-path | whitespace | rules-duplicate
    severity: str
    file: str
    line: int | None
    message: str
    why: str
    fix: str
    fixable: bool = False

    def to_json(self) -> dict:
        return {"category": self.category, "severity": self.severity, "file": self.file, "line": self.line,
                "message": self.message, "why": self.why, "fix": self.fix, "fixable": self.fixable}


@dataclass
class Options:
    max_tokens: int = DEFAULT_MAX_TOKENS
    max_block_lines: int = DEFAULT_MAX_BLOCK_LINES
    max_table_rows: int = DEFAULT_MAX_TABLE_ROWS
    move_blocks: bool = False


@dataclass
class Analysis:
    files: list[IFile]
    findings: list[TrimFinding]
    remove: dict[Path, set[int]]          # file -> unit indexes to remove
    move: dict[Path, dict[int, str]]      # file -> unit index -> label
    units: dict[Path, list[Unit]]
    tools: list[dict]
    opts: Options


def _snip(text: str, n: int = 60) -> str:
    s = re.sub(r"\s+", " ", text).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _bigrams(text: str) -> frozenset:
    words = text.split()
    return frozenset(zip(words, words[1:])) if len(words) > 1 else frozenset(words)


def _together(a: IFile, b: IFile) -> list[str]:
    return [t for t in a.tools if t in b.tools]


def analyze(ctx: Context, opts: Options | None = None, adapters=None) -> Analysis:
    from .adapters.base import RULES_END, RULES_START
    opts = opts or Options()
    files = discover(ctx, adapters)
    findings: list[TrimFinding] = []
    remove: dict[Path, set[int]] = {}
    move: dict[Path, dict[int, str]] = {}
    units: dict[Path, list[Unit]] = {}
    names = {t: n for f in files for t, n in zip(f.tools, f.tool_names)}
    seen: dict[str, list[tuple[IFile, Unit]]] = {}
    near_seen: dict[str, tuple[IFile, Unit]] = {}
    rules_seen: list[tuple[IFile, Unit]] = []
    near_pool: list[tuple[IFile, Unit, frozenset]] = []
    budget = NEAR_BUDGET

    for f in files:
        if f.text is None:
            continue
        lines = f.lines()
        us = units[f.real] = parse_units(lines, RULES_START, RULES_END)
        # (b) very long file
        if f.tokens > opts.max_tokens:
            findings.append(TrimFinding(
                "long-file", "high" if f.tokens > 2 * opts.max_tokens else "med", f.display, None,
                f"{f.display} is ~{_fmt(f.tokens)} tokens (approx.), over the {_fmt(opts.max_tokens)} limit.",
                "The whole file is resent on every turn, so every extra line costs credits all day.",
                "Keep only rules that apply to most tasks; move how-to detail into files the agent reads when needed."))
        for idx, u in enumerate(us):
            key = u.key
            # (a)/(f) duplicates
            if u.kind == "rules":
                for g, gu in rules_seen:
                    tog = [names[t] for t in _together(f, g)] if g is not f else f.tool_names
                    if not tog:
                        continue
                    same = gu.key == key
                    safe = same and (g is f or set(f.tools) <= set(g.tools))
                    findings.append(TrimFinding(
                        "rules-duplicate", "med", f.display, u.start + 1,
                        f"The {PRODUCT_NAME} rules block appears again in {f.display}:{u.start + 1} "
                        f"(first in {g.display}:{gu.start + 1}).",
                        f"Both copies load on every turn in {', '.join(tog)}, so you pay for the same rules twice.",
                        "Keep one copy." + (" trim removes the later copy." if safe else
                                           " The copies differ or another tool needs this one, so trim leaves it for you."),
                        fixable=safe))
                    if safe:
                        remove.setdefault(f.real, set()).add(idx)
                    break
                rules_seen.append((f, u))
                continue
            if u.kind != "heading" and len(key) >= MIN_DUP_CHARS:
                hit = None
                for g, gu in seen.get(key, []):
                    if g is f or _together(f, g):
                        hit = (g, gu)
                        if g is f or set(f.tools) <= set(g.tools):
                            break
                if hit:
                    g, gu = hit
                    safe = g is f or set(f.tools) <= set(g.tools)
                    where = "in the same file" if g is f else f"in {g.display}, which loads in the same turn"
                    tog = f.tool_names if g is f else [names[t] for t in _together(f, g)]
                    findings.append(TrimFinding(
                        "duplicate", "med", f.display, u.start + 1,
                        f"{f.display}:{u.start + 1} repeats {g.display}:{gu.start + 1} exactly (\"{_snip(key)}\").",
                        f"The same text is already {where}, so you pay for it twice on every turn in {', '.join(tog)}.",
                        "Keep the first copy. trim removes this one." if safe else
                        "Keep one copy. trim leaves this one because another tool loads only this file.",
                        fixable=safe))
                    if safe:
                        remove.setdefault(f.real, set()).add(idx)
                else:
                    nk = u.near
                    near = near_seen.get(nk) if len(nk) >= MIN_DUP_CHARS else None
                    if near and not (near[0] is f or _together(f, near[0])):
                        near = None
                    if near is None and len(key) >= NEAR_MIN_CHARS and u.kind in ("para", "bullet") \
                            and len(near_pool) < NEAR_MAX_UNITS:
                        sm = difflib.SequenceMatcher(None, "", nk, autojunk=False)  # seq2 cached once
                        grams = _bigrams(nk)
                        for g, gu, ggrams in near_pool:
                            if budget <= 0:
                                break
                            budget -= 1
                            if not (g is f or _together(f, g)):
                                continue
                            la, lb = len(gu.near), len(nk)
                            if 2 * min(la, lb) / (la + lb) < NEAR_RATIO:
                                continue
                            union = len(grams | ggrams) or 1
                            if len(grams & ggrams) / union < 0.6:
                                continue
                            sm.set_seq1(gu.near)
                            if sm.quick_ratio() >= NEAR_RATIO and sm.ratio() >= NEAR_RATIO:
                                near = (g, gu)
                                break
                    if near:
                        g, gu = near
                        findings.append(TrimFinding(
                            "near-duplicate", "low", f.display, u.start + 1,
                            f"{f.display}:{u.start + 1} is nearly the same as {g.display}:{gu.start + 1} (\"{_snip(key)}\").",
                            "Two wordings of one rule both load every turn, and the agent has to reconcile them.",
                            "Merge them into one wording yourself; trim never rewrites prose."))
                    if len(nk) >= MIN_DUP_CHARS:
                        near_seen.setdefault(nk, (f, u))
                    if u.kind in ("para", "bullet") and len(key) >= NEAR_MIN_CHARS:
                        near_pool.append((f, u, _bigrams(u.near)))
                seen.setdefault(key, []).append((f, u))
            if idx in remove.get(f.real, set()):
                continue
            # (c) large pasted blocks
            label = None
            if u.kind == "fence" and _is_log(u.lines):
                label = "pasted log"
            elif u.kind == "fence" and len(u.lines) - 2 > opts.max_block_lines:
                label = "code block"
            elif u.kind == "para" and _is_log(u.lines):
                label = "pasted log"
            elif any(_blob_line(l) for l in u.lines):
                label = "base64 or minified blob"
            elif u.kind == "table" and len(u.lines) - 2 > opts.max_table_rows:
                label = "long table"
            if label:
                movable = u.kind in ("fence", "para") and f.scope != "user" and f.editable \
                    and _under(f.real, ctx.root)
                fix = (f"With --move-blocks, trim moves it to {NOTES_DIR}/ and leaves a one-line pointer."
                       if movable else "Move it to a separate file and point to it; the agent can open it when needed.")
                findings.append(TrimFinding(
                    "big-block", "med", f.display, u.start + 1,
                    f"{f.display}:{u.start + 1} has a {label} ({len(u.lines)} lines, ~{_fmt(approx_tokens(chr(10).join(u.lines)))} tokens approx.).",
                    "Pasted code, logs, and data are resent on every turn even when the task has nothing to do with them.",
                    fix, fixable=movable and opts.move_blocks))
                if movable and opts.move_blocks:
                    move.setdefault(f.real, {})[idx] = label
        # (d) stale references (project files only; never outside the project)
        if f.scope != "user" and _under(f.real, ctx.root):
            stale = stale_paths(f.text, f.real.parent, ctx.root)
            if stale:
                shown = ", ".join(f"{p} (line {ln})" for ln, p in stale[:10])
                more = f" and {len(stale) - 10} more" if len(stale) > 10 else ""
                findings.append(TrimFinding(
                    "stale-path", "low", f.display, stale[0][0],
                    f"{f.display} mentions {len(stale)} path(s) that don't exist in this project: {shown}{more}.",
                    "Outdated pointers cost tokens every turn and can send the agent looking for files that are gone.",
                    "Update or delete those lines (trim doesn't edit them)."))
        # (e) whitespace (minor)
        mask = _fence_mask(lines)
        trailing = sum(1 for i, l in enumerate(lines) if not mask[i] and l != l.rstrip())
        extra_blank, run = 0, 0
        for i, l in enumerate(lines):
            if not l.strip() and not mask[i]:
                run += 1
                if run >= 2:
                    extra_blank += 1
            else:
                run = 0
        if trailing or extra_blank:
            bits = []
            if extra_blank:
                bits.append(f"{extra_blank} extra blank line(s)")
            if trailing:
                bits.append(f"{trailing} line(s) with trailing spaces")
            findings.append(TrimFinding(
                "whitespace", "info", f.display, None, f"{f.display} has {' and '.join(bits)}.",
                "Minor: whitespace is resent too, but it's a small cost.",
                "trim collapses repeated blank lines and strips trailing spaces (outside code blocks).",
                fixable=f.editable))
    findings.sort(key=lambda x: (SEVERITY_ORDER.get(x.severity, 9), x.file, x.line or 0))
    tools = []
    for t in dict.fromkeys(t for f in files for t in f.tools):
        mine = [f for f in files if t in f.tools]
        per = sum(f.tokens for f in mine)
        tools.append({"id": t, "name": names[t], "files": [f.display for f in mine],
                      "tokens_per_turn": per, "per_100_turns": per * 100})
    return Analysis(files, findings, remove, move, units, tools, opts)


# ---- proposal ------------------------------------------------------------------------------------

def _note_name(f: IFile, used: set[str], ctx: Context) -> str:
    stem = re.sub(r"[^a-z0-9]+", "-", f.real.stem.lower()).strip("-") or "notes"
    n = 1
    while True:
        name = f"{stem}-{n}.md"
        rel = f"{NOTES_DIR}/{name}"
        if rel not in used and not _exists(str(ctx.root / rel)):
            used.add(rel)
            return rel
        n += 1


def _collapse(lines: list[str]) -> list[str]:
    mask = _fence_mask(lines)
    out: list[str] = []
    out_mask: list[bool] = []
    for i, l in enumerate(lines):
        if not mask[i]:
            l = l.rstrip()
            if not l and out and not out[-1] and not out_mask[-1]:
                continue
        out.append(l)
        out_mask.append(mask[i])
    return out


def propose(ctx: Context, an: Analysis) -> list[dict]:
    """Planned changes [{path, kind, content, reason, key, display, tokens_before, tokens_after}]."""
    changes: list[dict] = []
    used: set[str] = set()
    for f in an.files:
        if not f.editable or f.text is None:
            continue
        lines = f.lines()
        had_final_nl = f.text.endswith("\n")
        if had_final_nl:
            lines = lines[:-1]
        us = an.units.get(f.real, [])
        rm = an.remove.get(f.real, set())
        mv = an.move.get(f.real, {})
        by_start = {us[i].start: (i, us[i]) for i in rm | set(mv)}
        new: list[str] = []
        notes: list[dict] = []
        i = 0
        while i < len(lines):
            if i in by_start:
                idx, u = by_start[i]
                if idx in mv:
                    rel = _note_name(f, used, ctx)
                    new.append(f"See {rel} when needed ({mv[idx]}, {len(u.lines)} lines, moved by `{CLI_NAME} trim`).")
                    body = "\n".join(u.lines)
                    notes.append({"path": str(ctx.root / rel), "kind": "create", "display": rel,
                                  "content": f"# Moved from {f.display} by {PRODUCT_NAME} trim\n\n{body}\n",
                                  "reason": f"{mv[idx]} moved out of {f.display}", "key": "moved-block"})
                i = u.end
                continue
            new.append(lines[i])
            i += 1
        new = _collapse(new)
        while new and new[-1] == "" and not _fence_mask(new)[-1]:
            new.pop()  # blank lines at the end of the file are pure waste
        text = "\n".join(new) + ("\n" if had_final_nl and new else "")
        if text == f.text:
            continue
        out_text = text.replace("\n", "\r\n") if f.nl == "\r\n" else text
        reasons = []
        if rm:
            reasons.append(f"removed {len(rm)} exact duplicate block(s)")
        if mv:
            reasons.append(f"moved {len(mv)} block(s) to {NOTES_DIR}/")
        reasons.append("whitespace tidy")
        changes.append({"path": str(f.path), "kind": "modify", "content": out_text, "display": f.display,
                        "reason": "trim: " + ", ".join(reasons), "key": "instructions",
                        "before": f.text, "after": text,
                        "tokens_before": f.tokens, "tokens_after": approx_tokens(text)})
        for nt in notes:
            nt["tokens_before"] = 0
            nt["tokens_after"] = 0  # not loaded each turn
            nt["before"], nt["after"] = "", nt["content"]
            changes.append(nt)
    return changes


def unified_diff(changes: list[dict]) -> str:
    parts = []
    for ch in changes:
        a = ch["before"].splitlines(keepends=True)
        b = ch["after"].splitlines(keepends=True)
        name = ch["display"]
        frm = "/dev/null" if ch["kind"] == "create" else f"a/{name}"
        parts.append("".join(difflib.unified_diff(a, b, frm, f"b/{name}", n=2)))
    return "".join(p if p.endswith("\n") else p + "\n" for p in parts if p)


# ---- report -------------------------------------------------------------------------------------

def build_report(ctx: Context, opts: Options | None = None, adapters=None) -> tuple[Analysis, list[dict], dict]:
    an = analyze(ctx, opts, adapters)
    changes = propose(ctx, an)
    after = {f.real: f.tokens for f in an.files}
    for ch in changes:
        if ch["kind"] == "modify":
            for f in an.files:
                if str(f.path) == ch["path"]:
                    after[f.real] = ch["tokens_after"]
    tools = []
    for t in an.tools:
        mine = [f for f in an.files if t["id"] in f.tools]
        per_after = sum(after[f.real] for f in mine)
        tools.append({**t, "tokens_per_turn_after": per_after, "per_100_turns_after": per_after * 100})
    before_total = sum(ch["tokens_before"] for ch in changes if ch["kind"] == "modify")
    after_total = sum(ch["tokens_after"] for ch in changes if ch["kind"] == "modify")
    data = {
        "product": PRODUCT_NAME, "version": VERSION, "mode": "dry-run", "approx": APPROX,
        "project": str(ctx.root), "max_tokens": an.opts.max_tokens, "move_blocks": an.opts.move_blocks,
        "files": [{"path": str(f.path), "display": f.display, "scope": f.scope, "tools": f.tool_names,
                   "tokens": f.tokens, "lines": (f.text.count("\n") + (0 if f.text.endswith("\n") or not f.text else 1))
                   if f.text is not None else None, "bytes": f.size, "opened": f.text is not None,
                   "editable": f.editable, "note": f.note} for f in an.files],
        "tools": tools,
        "findings": [x.to_json() for x in an.findings],
        "proposal": {
            "changes": [{"path": ch["path"], "display": ch["display"], "kind": ch["kind"], "reason": ch["reason"],
                         "tokens_before": ch["tokens_before"], "tokens_after": ch["tokens_after"]} for ch in changes],
            "tokens_before": before_total, "tokens_after": after_total,
            "tokens_saved_per_turn": before_total - after_total,
            "diff": unified_diff(changes),
        },
        "manifest": None,
    }
    return an, changes, data


def summary(ctx: Context, opts: Options | None = None, adapters=None) -> dict:
    """Compact read-only analysis for the dashboard card."""
    _, _, data = build_report(ctx, opts, adapters)
    counts: dict[str, int] = {}
    for x in data["findings"]:
        counts[x["category"]] = counts.get(x["category"], 0) + 1
    return {"files": [{k: f[k] for k in ("display", "scope", "tools", "tokens", "note")} for f in data["files"]],
            "tools": [{k: t[k] for k in ("name", "tokens_per_turn", "per_100_turns", "tokens_per_turn_after")}
                      for t in data["tools"]],
            "findings": len(data["findings"]), "by_category": counts,
            "top": [x["message"] for x in data["findings"][:3]],
            "saved_per_turn": data["proposal"]["tokens_saved_per_turn"],
            "command": f"{CLI_NAME} trim", "approx": APPROX}


def format_report(data: dict, *, show_diff: bool = True) -> list[str]:
    out = [f"{PRODUCT_NAME} trim: instruction files resent on every turn. Token counts are approx. (characters / 4).", ""]
    if not data["files"]:
        out.append("No instruction files found for the detected tools in this project or your home folder.")
        return out
    out.append("Files:")
    w = max(len(f["display"]) for f in data["files"])
    for f in data["files"]:
        note = f"  ({f['note']})" if f["note"] else ""
        out.append(f"  {f['display']:<{w}}  {f['scope']:<7}  {'~' + _fmt(f['tokens']):>8} tokens  {', '.join(f['tools'])}{note}")
    out += ["", "Resent per turn:"]
    for t in data["tools"]:
        out.append(f"  {t['name']}: ~{_fmt(t['tokens_per_turn'])} tokens per turn "
                   f"(~{_fmt(t['per_100_turns'])} per 100 turns, approx.)")
    out.append("")
    fs = data["findings"]
    if not fs:
        out.append("Findings: none. These files look lean.")
    else:
        out.append(f"Findings ({len(fs)}):")
        for x in fs:
            out.append(f"  {x['severity'].upper():<4} {x['category']}: {x['message']}")
            out.append(f"       Why: {x['why']}")
            out.append(f"       Fix: {x['fix']}")
    p = data["proposal"]
    out.append("")
    if not p["changes"]:
        out.append("Proposed edit: nothing safe to change automatically.")
        return out
    out.append("Proposed edit (safe mechanical changes only: exact duplicates, blank lines, trailing spaces"
               + (", moved blocks" if data["move_blocks"] else "") + "; prose is never rewritten):")
    for ch in p["changes"]:
        if ch["kind"] == "create":
            out.append(f"  create {ch['display']} (not loaded each turn; the agent opens it when needed)")
        else:
            out.append(f"  {ch['display']}: ~{_fmt(ch['tokens_before'])} -> ~{_fmt(ch['tokens_after'])} tokens "
                       f"({_fmt(ch['tokens_after'] - ch['tokens_before'])} approx.)")
    out.append(f"  Per turn: ~{_fmt(p['tokens_saved_per_turn'])} fewer tokens (approx.) in every tool that loads these files.")
    for t in data["tools"]:
        if t["tokens_per_turn_after"] != t["tokens_per_turn"]:
            out.append(f"    {t['name']}: ~{_fmt(t['tokens_per_turn'])} -> ~{_fmt(t['tokens_per_turn_after'])} per turn")
    if show_diff and p["diff"]:
        out += ["", p["diff"].rstrip("\n")]
    return out


def _isatty() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def run_trim(ctx: Context, opts: Options, *, apply: bool = False, yes: bool = False, as_json: bool = False,
             show_diff: bool = True, interactive: bool | None = None, input_fn=input,
             out: Callable[[str], None] = print, adapters=None) -> int:
    from .installer import _now_stamp, apply_changes
    an, changes, data = build_report(ctx, opts, adapters)
    if interactive is None:
        interactive = _isatty()
    lines = [] if as_json else format_report(data, show_diff=show_diff)
    for line in lines:
        out(line)
    if not apply or not changes:
        if not as_json:
            out("")
            out("Dry run: nothing was changed." + (
                f" Add --apply to make these edits (backed up; `{CLI_NAME} undo` restores them)." if changes else ""))
        else:
            data["mode"] = "dry-run" if changes or not apply else "nothing"
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
        out(f"Apply these edits to {sum(1 for c in changes if c['kind'] == 'modify')} file(s)? Every file is backed up first. [y/N] ")
        try:
            answer = input_fn()
        except EOFError:
            answer = ""
        if answer.strip().lower() not in {"y", "yes"}:
            out("No changes made.")
            return 0
    run_id = _now_stamp()
    applied = apply_changes(ctx, [{k: ch[k] for k in ("path", "kind", "content", "reason", "key")} for ch in changes],
                            stamp=f"{run_id}-trim", profile="trim",
                            extra_manifest={"setup_run": f"{run_id}-trim", "setup_item": "trim",
                                            "title": "Trim instruction files"})
    data["mode"] = "applied"
    data["manifest"] = applied["manifest_path"]
    if as_json:
        out(json.dumps(data, indent=2))
        return 0
    out("")
    for rec in applied["manifest"]["files"]:
        verb = "backed up and trimmed" if rec.get("backup") else "created (undo deletes it)"
        out(f"✔ {verb} {_display(Path(rec['path']), ctx)}")
    out(f"Done. To undo: `{CLI_NAME} undo` (or `{CLI_NAME} undo {applied['manifest_path']}`).")
    return 0
