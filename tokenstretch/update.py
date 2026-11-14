"""`tokenstretch update`: see whether a newer release exists, show what changed, ask, install it.

This is the one command that uses the network, and only when you run it. It downloads two public
files from the project's GitHub page with `curl` (or `wget`): the installer script from the main
branch, which names the newest pinned release, and that release's changelog. Nothing about you or
this computer is sent beyond what any download sends (your address, to GitHub). The install itself
is the same pinned installer as a first install: it checks the release tag against the pinned
commit, or the tarball against its pinned SHA-256, before it copies anything.

No network library is imported: the download is a separate `curl`/`wget` process, so the rest of
the tool still cannot reach the network.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

from .identity import CLI_NAME, VERSION, state_dir

REPO_URL = "https://github.com/ExJtac/tokenstretch"
RAW_URL = "https://raw.githubusercontent.com/ExJtac/tokenstretch"
INSTALLER_URL = f"{RAW_URL}/main/scripts/install.sh"
MARKER = "# managed by the tokenstretch installer (scripts/install.sh)"
MAX_BYTES = 2_000_000       # the installer is ~15K and the changelog ~40K; anything far larger is not ours
FETCH_SECONDS = 20
MAX_CHANGE_LINES = 40
CHANGE_WIDTH = 240
EXIT_NEWER = 10             # `update --check`: a newer release exists

_PIN_RE = {
    "tag": re.compile(r'^TS_PIN_TAG="(v\d+\.\d+\.\d+)"', re.M),
    "commit": re.compile(r'^TS_PIN_COMMIT="([0-9a-f]{40})"', re.M),
    "sha256": re.compile(r'^TS_PIN_TARBALL_SHA256="([0-9a-f]{64})?"', re.M),
}
_SECTION_RE = re.compile(r"^## \[(\d+\.\d+\.\d+)\]")


class UpdateError(RuntimeError):
    pass


def fetch(url: str) -> str:
    """Download one https URL as text with curl or wget. No network library is used."""
    if not url.startswith("https://"):
        raise UpdateError(f"refusing a non-https address: {url}")
    if shutil.which("curl"):
        argv = ["curl", "--proto", "=https", "--tlsv1.2", "-fsSL", "--max-time", str(FETCH_SECONDS),
                "--max-filesize", str(MAX_BYTES), url]
    elif shutil.which("wget"):
        argv = ["wget", "--https-only", "-q", "-O", "-", f"--timeout={FETCH_SECONDS}", url]
    else:
        raise UpdateError("neither curl nor wget was found, so the release list cannot be downloaded")
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=FETCH_SECONDS + 10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"could not run {argv[0]}: {exc}") from exc
    if proc.returncode != 0:
        raise UpdateError(f"could not download {url} ({argv[0]} exit {proc.returncode}). Are you online?")
    if len(proc.stdout) > MAX_BYTES:
        raise UpdateError(f"{url} is larger than expected; stopping")
    return proc.stdout.decode("utf-8", errors="replace")


def parse_pins(installer_text: str) -> dict[str, str]:
    """The release the installer script is pinned to. Raises when the pins are missing or malformed."""
    pins = {}
    for key, rx in _PIN_RE.items():
        m = rx.search(installer_text)
        if not m:
            raise UpdateError("the installer on the project page has no valid pinned release; not updating")
        pins[key] = m.group(1) or ""
    return pins


def version_tuple(text: str) -> tuple[int, ...]:
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", text.strip())
    if not m:
        raise UpdateError(f"not a version number: {text!r}")
    return tuple(int(g) for g in m.groups())


def changes_between(changelog: str, installed: str, latest: str) -> list[str]:
    """Changelog lines for every release after `installed`, up to and including `latest`."""
    low, high = version_tuple(installed), version_tuple(latest)
    out: list[str] = []
    keep = False
    for line in changelog.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            keep = low < version_tuple(m.group(1)) <= high
            if keep:
                out.append(line[3:].strip())
            continue
        if line.startswith("## "):      # [Unreleased] or any other section
            keep = False
            continue
        if keep and line.startswith("- "):
            out.append("  " + (line if len(line) <= CHANGE_WIDTH else line[:CHANGE_WIDTH - 1].rstrip() + "…"))
    if len(out) > MAX_CHANGE_LINES:
        more = len(out) - MAX_CHANGE_LINES
        out = out[:MAX_CHANGE_LINES] + [f"  … and {more} more lines: {REPO_URL}/blob/main/CHANGELOG.md"]
    return out


def find_install(package_dir: Path | None = None, which: Callable[[str], str | None] = shutil.which) -> dict | None:
    """The folders of a copy put there by scripts/install.sh, or None for any other kind of copy."""
    pkg = (package_dir or Path(__file__).parent).resolve()
    src, data = pkg.parent, pkg.parent.parent
    manifest = data / "install-manifest.txt"
    try:
        if src.name != "src" or MARKER not in manifest.read_text(encoding="utf-8"):
            return None
    except OSError:
        return None
    bin_dir = None
    found = which(CLI_NAME)
    for cand in ([Path(found)] if found else []) + [Path.home() / ".local" / "bin" / CLI_NAME]:
        try:
            text = cand.read_text(encoding="utf-8")
        except OSError:
            continue
        if MARKER in text and str(data) in text:
            bin_dir = cand.parent
            break
    return {"data_dir": data, "bin_dir": bin_dir}


def check(fetcher: Callable[[str], str] = fetch, installed: str = VERSION) -> dict:
    """Compare the installed version with the newest pinned release. Uses the network."""
    installer = fetcher(INSTALLER_URL)
    pins = parse_pins(installer)
    latest = pins["tag"].lstrip("v")
    newer = version_tuple(latest) > version_tuple(installed)
    changes: list[str] = []
    if newer:
        try:
            changes = changes_between(fetcher(f"{RAW_URL}/{pins['commit']}/CHANGELOG.md"), installed, latest)
        except UpdateError:
            changes = []    # the notes are a courtesy; the release link below still works
    return {"installed": installed, "latest": latest, "tag": pins["tag"], "commit": pins["commit"],
            "newer": newer, "changes": changes, "release_url": f"{REPO_URL}/releases/tag/{pins['tag']}",
            "_installer": installer}


def state_file(home: Path) -> Path:
    return state_dir(home) / "update.json"


def save_check(home: Path, result: dict, now: float | None = None) -> None:
    """Remember the last check so other commands can mention a newer release without the network."""
    data = {"checked_at": int(now if now is not None else time.time()), "installed": result["installed"],
            "latest": result["latest"], "newer": result["newer"]}
    path = state_file(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def run_installer(installer_text: str, install: dict, runner: Callable[..., "subprocess.CompletedProcess"] = subprocess.run) -> int:
    """Run the downloaded installer for the folders this copy lives in. It verifies the release itself."""
    with tempfile.TemporaryDirectory(prefix="tokenstretch-update-") as tmp:
        script = Path(tmp) / "install.sh"
        script.write_text(installer_text, encoding="utf-8")
        argv = ["sh", str(script), "--data-dir", str(install["data_dir"])]
        if install.get("bin_dir"):
            argv += ["--bin-dir", str(install["bin_dir"])]
        return runner(argv).returncode


def run_update(home: Path, *, yes: bool = False, check_only: bool = False, as_json: bool = False,
               interactive: bool | None = None, input_fn: Callable[[], str] = input,
               fetcher: Callable[[str], str] = fetch, install: dict | None | str = "detect",
               runner: Callable[..., "subprocess.CompletedProcess"] = subprocess.run,
               installed: str = VERSION, out: Callable[[str], None] = print) -> int:
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if install == "detect":
        install = find_install()
    try:
        res = check(fetcher, installed)
    except UpdateError as exc:
        if as_json:
            out(json.dumps({"error": str(exc)}, indent=2))
        else:
            out(f"Could not check for a newer release: {exc}")
        return 1
    installer_text = res.pop("_installer")
    save_check(home, res)
    res["managed_install"] = bool(install)
    res["updated"] = False
    apply_cmd = f"{CLI_NAME} update --yes"

    def finish(code: int) -> int:
        if as_json:
            out(json.dumps(res, indent=2))
        return code

    say = (lambda _line="": None) if as_json else out
    if not res["newer"]:
        say(f"{CLI_NAME} {res['installed']} is the newest release. Nothing to do.")
        return finish(0)
    say(f"Installed: {res['installed']}. Newest release: {res['latest']}.")
    if res["changes"]:
        say("What changed:")
        for line in res["changes"]:
            say(line)
    say(f"Release notes: {res['release_url']}")
    if check_only:
        say(f"To install it, run: {CLI_NAME} update")
        return finish(EXIT_NEWER)
    if not install:
        say("This copy was not put here by the tokenstretch installer, so it is not changed. "
            "Update it the way you installed it (for a git checkout: `git pull`).")
        return finish(EXIT_NEWER)
    if os.name == "nt" or not shutil.which("sh"):
        say(f"The installer needs a POSIX shell, which was not found. Install by hand: {REPO_URL}#install")
        return finish(EXIT_NEWER)
    if not yes:
        if as_json or not interactive:
            say("Nothing was changed. To install the new release, run:")
            say(f"  {apply_cmd}")
            return finish(EXIT_NEWER)
        say(f"Install {res['latest']} now? The release is checked against its pinned commit first. [y/N] ")
        try:
            answer = input_fn()
        except EOFError:
            answer = ""
        if answer.strip().lower() not in {"y", "yes"}:
            say(f"Not updated. To install it later, run: {apply_cmd}")
            return finish(EXIT_NEWER)
    code = run_installer(installer_text, install, runner)
    if code != 0:
        say(f"The installer stopped (exit {code}). Your installed copy is the one it reports above; "
            f"run `{CLI_NAME} --version` to check.")
        return finish(1)
    res["updated"] = True
    save_check(home, {**res, "installed": res["latest"], "newer": False})
    say(f"Updated to {res['latest']}. Your settings and hooks are unchanged and now run the new release.")
    say(f"If the notes above list new setup items, run `{CLI_NAME} start` to see and add them.")
    return finish(0)
