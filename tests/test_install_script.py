"""scripts/install.sh against a local file:// repo and tarball, with the network blocked where possible."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "install.sh"
TAG = "v9.9.9"

pytestmark = pytest.mark.skipif(shutil.which("git") is None or os.name != "posix", reason="needs git and a POSIX sh")


def _unshare_ok() -> bool:
    if not shutil.which("unshare"):
        return False
    return subprocess.run(["unshare", "-rn", "true"], capture_output=True).returncode == 0


NO_NET = _unshare_ok()


@pytest.fixture(scope="module")
def release(tmp_path_factory):
    """A local copy of this tree as a tagged git repo, plus a git-archive tarball and its SHA-256."""
    base = tmp_path_factory.mktemp("release")
    repo = base / "repo"
    files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=REPO,
                           capture_output=True, check=True).stdout.split(b"\0")
    for rel in filter(None, files):
        src = REPO / rel.decode()
        if src.is_file():
            dst = repo / rel.decode()
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-qm", "release"], cwd=repo, check=True)
    subprocess.run([*git, "tag", TAG], cwd=repo, check=True)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    tarball = base / f"{commit}.tar.gz"
    subprocess.run(["git", "archive", "--format=tar.gz", f"--prefix=tokenstretch-{commit}/", "-o", str(tarball), "HEAD"],
                   cwd=repo, check=True)
    return {"repo": repo, "commit": commit, "tarball": tarball,
            "sha": hashlib.sha256(tarball.read_bytes()).hexdigest()}


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


PIN_RE = re.compile(r'^(TS_PIN_TAG|TS_PIN_COMMIT|TS_PIN_TARBALL_SHA256)="[^"]*"', re.M)


def _unpinned(tmp_path: Path) -> Path:
    """A copy of install.sh with the pin block emptied, whatever the repo's current pin is."""
    p = tmp_path / "install-unpinned.sh"
    p.write_text(PIN_RE.sub(lambda m: f'{m.group(1)}=""', SCRIPT.read_text()))
    return p


def _run(home: Path, *args: str, path: str | None = None, shell: str = "/bin/zsh",
         script: Path | None = None) -> subprocess.CompletedProcess:
    env = {"HOME": str(home), "PATH": path or "/usr/bin:/bin", "SHELL": shell, "LANG": "C"}
    cmd = ["sh", str(script or SCRIPT), "--python", sys.executable, *args]
    if NO_NET:
        cmd = ["unshare", "-rn", *cmd]
    return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)


def _git_args(rel) -> list[str]:
    return ["--source", f"file://{rel['repo']}", "--tag", TAG, "--commit", rel["commit"]]


def _tree(home: Path) -> dict[str, bytes]:
    return {str(p.relative_to(home)): (p.read_bytes() if p.is_file() else b"")
            for p in sorted(home.rglob("*")) if "__pycache__" not in p.parts}


def test_install_from_pinned_tag_runs_offline(tmp_path, release):
    home = _home(tmp_path)
    r = _run(home, *_git_args(release))
    assert r.returncode == 0, r.stderr
    assert f"verified: HEAD is {release['commit']}" in r.stdout and "Installed tokenstretch" in r.stdout
    shim = home / ".local" / "bin" / "tokenstretch"
    data = home / ".local" / "share" / "tokenstretch"
    assert shim.is_file() and os.access(shim, os.X_OK)
    assert (data / "src" / "tokenstretch" / "__init__.py").is_file() and not (data / "src" / ".git").exists()
    assert f"commit:{release['commit']}" in (data / "install-manifest.txt").read_text()
    out = subprocess.run([str(shim), "--version"], capture_output=True, text=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0 and out.stdout.startswith("tokenstretch ")
    # Nothing set up, no shell file touched, no state dir created.
    assert not (home / ".tokenstretch").exists() and not (home / ".zshrc").exists()
    assert "not on your PATH" in r.stdout
    # Re-running (an upgrade to the same pin) works in place.
    assert _run(home, *_git_args(release)).returncode == 0


def test_dry_run_changes_nothing(tmp_path, release):
    home = _home(tmp_path)
    (home / ".zshrc").write_text("# mine\n")
    before = _tree(home)
    r = _run(home, *_git_args(release), "--dry-run", "--add-to-path")
    assert r.returncode == 0, r.stderr
    assert "[dry run] would run: git clone --depth 1 --branch v9.9.9" in r.stdout
    assert "[dry run] would write the launcher" in r.stdout and "append to" in r.stdout
    assert "Dry run: nothing was downloaded or changed." in r.stdout
    assert _tree(home) == before


def test_uninstall_removes_exactly_what_it_created(tmp_path, release):
    home = _home(tmp_path)
    (home / ".zshrc").write_text("# mine\nalias ll='ls -l'\n")
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "other-tool").write_text("#!/bin/sh\n")
    (home / ".local" / "share" / "keep-me").mkdir(parents=True)
    before = _tree(home)
    r = _run(home, *_git_args(release), "--add-to-path")
    assert r.returncode == 0, r.stderr
    assert "managed by the tokenstretch installer" in (home / ".zshrc").read_text()
    assert list((home / ".local" / "share" / "tokenstretch" / "backups").iterdir())
    dry = _run(home, "--uninstall", "--dry-run")
    assert "would remove the launcher" in dry.stdout and (home / ".local" / "bin" / "tokenstretch").exists()
    r = _run(home, "--uninstall")
    assert r.returncode == 0, r.stderr
    assert _tree(home) == before
    assert "Nothing to remove" in _run(home, "--uninstall").stdout


def test_add_to_path_creates_and_uninstall_removes_a_new_rc_file(tmp_path, release):
    home = _home(tmp_path)
    assert _run(home, *_git_args(release), "--add-to-path").returncode == 0
    assert (home / ".zshrc").read_text().startswith("export PATH=")
    assert _run(home, "--uninstall").returncode == 0
    assert not (home / ".zshrc").exists() and not (home / ".local" / "bin" / "tokenstretch").exists()
    assert _tree(home) == {}  # the folders it created are gone too


def test_wrong_commit_is_refused_and_nothing_installed(tmp_path, release):
    home = _home(tmp_path)
    r = _run(home, "--source", f"file://{release['repo']}", "--tag", TAG, "--commit", "0" * 40)
    assert r.returncode != 0 and "not the pinned commit" in r.stderr
    assert _tree(home) == {}


def test_old_python_is_refused(tmp_path, release):
    home = _home(tmp_path)
    fake = tmp_path / "python3"
    fake.write_text('#!/bin/sh\ncase "$1" in --version) echo "Python 3.9.6";; *) echo "3 9";; esac\n')
    fake.chmod(0o755)
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "SHELL": "/bin/sh"}
    r = subprocess.run(["sh", str(SCRIPT), "--python", str(fake), *_git_args(release)], env=env,
                       capture_output=True, text=True)
    assert r.returncode != 0 and "not Python 3.11 or newer" in r.stderr and "3.9.6" in r.stderr
    assert _tree(home) == {}


def test_tarball_method_checks_sha256(tmp_path, release):
    if not shutil.which("curl"):
        pytest.skip("curl not installed")
    home = _home(tmp_path)
    url = f"file://{release['tarball']}"
    bad = _run(home, "--method", "tarball", "--commit", release["commit"], "--tarball-url", url, "--sha256", "a" * 64)
    assert bad.returncode != 0 and "SHA-256 mismatch" in bad.stderr and _tree(home) == {}
    none = _run(home, "--method", "tarball", "--commit", release["commit"], "--tarball-url", url,
                script=_unpinned(tmp_path))
    assert none.returncode != 0 and "refusing to install unverified code" in none.stderr
    ok = _run(home, "--method", "tarball", "--commit", release["commit"], "--tarball-url", url, "--sha256", release["sha"])
    assert ok.returncode == 0, ok.stderr
    assert (home / ".local" / "share" / "tokenstretch" / "src" / "pyproject.toml").is_file()


def test_unpinned_script_refuses_and_foreign_launcher_is_kept(tmp_path, release):
    home = _home(tmp_path)
    r = _run(home, script=_unpinned(tmp_path))
    assert r.returncode != 0 and "No release is pinned" in r.stderr and _tree(home) == {}
    shim = home / ".local" / "bin" / "tokenstretch"
    shim.parent.mkdir(parents=True)
    shim.write_text("#!/bin/sh\necho someone else\n")
    r = _run(home, *_git_args(release))
    assert r.returncode != 0 and "not created by this installer" in r.stderr
    assert shim.read_text() == "#!/bin/sh\necho someone else\n"


def test_script_has_no_telemetry_and_one_pin_block():
    text = SCRIPT.read_text()
    assert text.count("==== PINNED RELEASE") == 1
    pins = dict(m.group(0).split("=", 1) for m in PIN_RE.finditer(text))
    assert set(pins) == {"TS_PIN_TAG", "TS_PIN_COMMIT", "TS_PIN_TARBALL_SHA256"}
    tag, commit, sha = (pins[k].strip('"') for k in ("TS_PIN_TAG", "TS_PIN_COMMIT", "TS_PIN_TARBALL_SHA256"))
    assert (tag == "" and commit == "") or (re.fullmatch(r"v\d+\.\d+\.\d+", tag) and re.fullmatch(r"[0-9a-f]{40}", commit))
    assert sha == "" or re.fullmatch(r"[0-9a-f]{64}", sha)
    lowered = text.lower()
    for bad in ("-m pip", "setup --yes", "tokenstretch install ", "analytics.", "wget "):
        assert bad not in lowered
    # The only network commands are the pinned clone and the pinned download.
    assert lowered.count("git ") and lowered.count("curl --proto") == 1


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_clean():
    for shell in ("sh", "bash"):
        r = subprocess.run(["shellcheck", "-s", shell, str(SCRIPT)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout


def test_pyproject_console_script():
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert data["project"]["scripts"]["tokenstretch"] == "tokenstretch.cli:main"
