"""Read-only snapshot of the machine that adapters inspect.

Everything here is local: files, environment, PATH, process list, listening sockets,
crontab. Nothing opens a network connection. Tests (and `--sys-snapshot DIR`) replace
the live probes with fixture files so results are deterministic.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

RC_FILES = (".zshrc", ".bashrc", ".bash_profile", ".profile", ".zprofile", ".config/fish/config.fish")


@dataclass
class Listener:
    address: str
    port: int
    process: str = ""

    @property
    def exposed(self) -> bool:
        return self.address in {"0.0.0.0", "*", "::", "[::]"}


@dataclass
class FileHold:
    """A process holding a path, from an lsof snapshot or a live lsof probe. The file is never opened."""
    process: str
    path: str


@dataclass
class Context:
    home: Path
    root: Path
    env: dict[str, str] = field(default_factory=dict)
    rc_text: str = ""
    path_dirs: list[Path] = field(default_factory=list)
    processes: list[str] = field(default_factory=list)
    listeners: list[Listener] = field(default_factory=list)
    crontab: str = ""
    timers: list[str] = field(default_factory=list)
    plan_override: str | None = None  # "subscription" | "api" | None
    snapshot: bool = False
    file_holds: list[FileHold] = field(default_factory=list)
    trust_certs: list[str] = field(default_factory=list)
    platform: str = ""  # sys.platform or snapshot platform.txt (darwin/linux/win32)
    os_version: str = ""  # macOS product version from platform.mac_ver() or snapshot os-version.txt / sw_vers.txt

    # ---- helpers adapters use --------------------------------------------------
    def which(self, name: str) -> Path | None:
        for d in self.path_dirs:
            p = d / name
            if p.is_file() and os.access(p, os.X_OK):
                return p
        return None

    def env_or_rc(self, name: str) -> tuple[str | None, str]:
        """Return (value, where) for a variable set in the process env or exported in an rc file."""
        if name in self.env:
            return self.env[name], "env"
        m = re.search(rf"^\s*(?:export\s+|set\s+-gx\s+)?{re.escape(name)}[= ]\s*['\"]?([^'\"\n]*)", self.rc_text, re.M)
        if m:
            return m.group(1).strip(), "rc"
        return None, ""

    def process_matches(self, pattern: str) -> list[str]:
        rx = re.compile(pattern, re.I)
        return [p for p in self.processes if rx.search(p)]

    def listener_on(self, port: int) -> list[Listener]:
        return [l for l in self.listeners if l.port == port]

    def exists(self, path: Path) -> bool:
        """Stat-only presence check. Used for credential files: never opened."""
        return self.file_stat(path) is not None

    def file_stat(self, path: Path):
        """Return os.stat_result or None. Never opens the file. Safe for credentials."""
        try:
            return path.stat()
        except OSError:
            return None

    def os_name(self) -> str:
        return (self.platform or sys.platform).lower()

    def holders_of(self, path: Path) -> list[str]:
        """Process names holding `path`. Uses the snapshot when present; otherwise a local lsof.
        Never opens the file."""
        target = path
        found = [h.process for h in self.file_holds if _hold_matches(h.path, target, self.home)]
        if found or self.snapshot:
            return found
        text = _run_local(["lsof", "-Fn", "--", str(path)])
        procs: list[str] = []
        current = ""
        for line in text.splitlines():
            if line.startswith("c"):
                current = line[1:]
            elif line.startswith("n") and current:
                procs.append(current)
                current = ""
        return procs


# ---- live probes (local only) ------------------------------------------------------


def _hold_matches(raw: str, target: Path, home: Path) -> bool:
    expanded = raw.replace("$HOME", str(home))
    if expanded.startswith("~"):
        expanded = str(home) + expanded[1:]
    try:
        if Path(expanded) == target or Path(expanded).resolve() == target.resolve():
            return True
    except OSError:
        pass
    try:
        rel = target.relative_to(home).as_posix()
    except ValueError:
        rel = target.name
    return raw.endswith(str(target)) or raw.endswith(rel) or raw.endswith("/" + rel)


def _parse_lsof(text: str, home: Path) -> list[FileHold]:
    out: list[FileHold] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            out.append(FileHold(parts[0], parts[-1]))
    return out


def _run_local(argv: list[str]) -> str:
    if not shutil.which(argv[0]):
        return ""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _parse_listeners(text: str) -> list[Listener]:
    out: list[Listener] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("State", "Netid", "#")):
            continue
        # Accept `ss -ltnp` rows and the simple fixture form "ADDR:PORT process".
        m = re.search(r"(\[?[0-9a-fA-F:.*]+\]?):(\d+)\s", line + " ")
        if not m:
            continue
        proc = ""
        pm = re.search(r'users:\(\("([^"]+)"', line)
        if pm:
            proc = pm.group(1)
        else:
            tail = line[m.end():].strip().split()
            proc = tail[-1] if tail else ""
        out.append(Listener(m.group(1).strip("[]") or "*", int(m.group(2)), proc))
    return out


def _read_rc(home: Path) -> str:
    chunks = []
    for name in RC_FILES:
        p = home / name
        try:
            chunks.append(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    return "\n".join(chunks)



def _os_version_from_snapshot(rd) -> str:
    ver = (rd("os-version.txt") or "").strip()
    if ver:
        return ver.splitlines()[0].strip()
    sw = rd("sw_vers.txt")
    if sw:
        for line in sw.splitlines():
            if "ProductVersion" in line:
                _, _, rest = line.partition(":")
                rest = rest.strip() or line.split()[-1]
                if rest:
                    return rest
        # allow a bare version as the only content
        first = sw.strip().splitlines()[0].strip() if sw.strip() else ""
        if first and first[0].isdigit():
            return first
    return ""


def _live_os_version() -> str:
    if sys.platform != "darwin":
        return ""
    try:
        import platform as _platform
        return (_platform.mac_ver()[0] or "").strip()
    except Exception:
        return ""


def build_context(home: Path | None = None, root: Path | None = None, sys_snapshot: Path | None = None,
                  plan: str | None = None) -> Context:
    home = Path(home or Path.home()).expanduser().resolve()
    root = Path(root or Path.cwd()).expanduser().resolve()
    if sys_snapshot:
        snap = Path(sys_snapshot)

        def rd(name: str) -> str:
            try:
                return (snap / name).read_text(encoding="utf-8")
            except OSError:
                return ""

        env = json.loads(rd("env.json") or "{}")
        path_dirs = [Path(p) if Path(p).is_absolute() else (snap / p) for p in env.get("PATH", "").split(os.pathsep) if p]
        return Context(
            home=home, root=root, env=env, rc_text=_read_rc(home), path_dirs=path_dirs,
            processes=[l for l in rd("ps.txt").splitlines() if l.strip()],
            listeners=_parse_listeners(rd("listeners.txt")),
            crontab=rd("crontab.txt"),
            timers=[l for l in rd("timers.txt").splitlines() if l.strip()],
            plan_override=plan,
            snapshot=True,
            file_holds=_parse_lsof(rd("lsof.txt"), home),
            trust_certs=[l.strip() for l in rd("ca-certs.txt").splitlines() if l.strip() and not l.startswith("#")],
            platform=(rd("platform.txt") or "").strip() or sys.platform,
            os_version=_os_version_from_snapshot(rd),
        )
    env = dict(os.environ)
    cert_dir = Path("/usr/local/share/ca-certificates")
    certs = []
    if cert_dir.is_dir():
        try:
            certs = [p.name for p in cert_dir.iterdir() if p.is_file()]
        except OSError:
            certs = []
    return Context(
        home=home, root=root, env=env, rc_text=_read_rc(home),
        path_dirs=[Path(p) for p in env.get("PATH", "").split(os.pathsep) if p],
        processes=[l for l in _run_local(["ps", "-eo", "args"]).splitlines()[1:] if l.strip()],
        listeners=_parse_listeners(_run_local(["ss", "-ltnp"])),
        crontab=_run_local(["crontab", "-l"]),
        timers=[l for l in _run_local(["systemctl", "--user", "list-timers", "--all", "--no-pager"]).splitlines() if l.strip()],
        plan_override=plan,
        snapshot=False,
        file_holds=[],
        trust_certs=certs,
        platform=sys.platform,
        os_version=_live_os_version(),
    )
