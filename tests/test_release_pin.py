"""scripts/release-pin.sh: fills the installer pin from a local tag, commits, never pushes or tags."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import tokenstretch
from tokenstretch.identity import VERSION

REPO = Path(__file__).resolve().parents[1]
PIN = REPO / "scripts" / "release-pin.sh"
GIT_ID = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
          "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

pytestmark = pytest.mark.skipif(shutil.which("git") is None or os.name != "posix", reason="needs git and a POSIX sh")


def test_version_is_the_same_everywhere():
    py = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
    assert py == VERSION == tokenstretch.__version__
    out = subprocess.run([sys.executable, "-m", "tokenstretch", "--version"], capture_output=True, text=True, cwd=REPO)
    assert out.stdout.strip() == f"tokenstretch {VERSION}"
    assert re.search(rf"^## \[{re.escape(VERSION)}\] - \d{{4}}-\d{{2}}-\d{{2}}$", (REPO / "CHANGELOG.md").read_text(), re.M)


def _git(repo: Path, *args: str, check=True) -> subprocess.CompletedProcess:
    env = {**os.environ, **GIT_ID, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(repo.parent)}
    return subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args], cwd=repo,
                          env=env, capture_output=True, text=True, check=check)


def _pin(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, **GIT_ID, "GIT_CONFIG_NOSYSTEM": "1", "HOME": str(repo.parent), "LANG": "C"}
    cmd = ["sh", str(repo / "scripts" / "release-pin.sh"), *args]
    if shutil.which("unshare") and subprocess.run(["unshare", "-rn", "true"], capture_output=True).returncode == 0:
        cmd = ["unshare", "-rn", *cmd]
    return subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    repo = tmp_path / "repo"
    files = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=REPO,
                           capture_output=True, check=True).stdout.split(b"\0")
    for rel in filter(None, files):
        src = REPO / rel.decode()
        if src.is_file():
            dst = repo / rel.decode()
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    # Start from an unpinned script with the "Not released yet" line, like the release commit.
    script = repo / "scripts" / "install.sh"
    script.write_text(re.sub(r'^(TS_PIN_TAG|TS_PIN_COMMIT|TS_PIN_TARBALL_SHA256)="[^"]*"', r'\1=""',
                             script.read_text(), flags=re.M))
    readme = repo / "README.md"
    if "**Not released yet.**" not in readme.read_text():
        readme.write_text(readme.read_text().replace("## Install\n\n", "## Install\n\n**Not released yet.** Test.\n\n", 1))
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "release")
    return repo


def test_pin_from_local_tag_then_install_from_file_clone(tmp_path, repo):
    tag = f"v{VERSION}"
    assert "no local tag" in _pin(repo, tag).stderr
    _git(repo, "tag", "-a", tag, "-m", f"tokenstretch {VERSION}")
    commit_a = _git(repo, "rev-parse", f"{tag}^{{commit}}").stdout.strip()
    dry = _pin(repo, tag, "--dry-run")
    assert dry.returncode == 0 and "Dry run" in dry.stdout and _git(repo, "status", "--porcelain").stdout == ""
    r = _pin(repo, tag)
    assert r.returncode == 0, r.stderr
    text = (repo / "scripts" / "install.sh").read_text()
    assert f'TS_PIN_TAG="{tag}"' in text and f'TS_PIN_COMMIT="{commit_a}"' in text and 'TS_PIN_TARBALL_SHA256=""' in text
    assert "Not released yet" not in (repo / "README.md").read_text()
    assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == f"Pin installer to {tag} ({commit_a})"
    assert _git(repo, "rev-parse", f"{tag}^{{commit}}").stdout.strip() == commit_a  # tag untouched
    assert _git(repo, "tag").stdout.split() == [tag]
    assert "Already pinned" in _pin(repo, tag).stdout
    # The pinned script installs from a file:// clone of this repo: tag checked against the pinned commit.
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "SHELL": "/bin/sh", "LANG": "C"}
    inst = subprocess.run(["sh", str(repo / "scripts" / "install.sh"), "--python", sys.executable,
                           "--source", f"file://{repo}"], env=env, capture_output=True, text=True)
    assert inst.returncode == 0, inst.stderr
    assert f"verified: HEAD is {commit_a}" in inst.stdout
    out = subprocess.run([str(home / ".local" / "bin" / "tokenstretch"), "--version"], capture_output=True, text=True,
                         env=env)
    assert out.stdout.strip() == f"tokenstretch {VERSION}"
    # Later: add the GitHub tarball SHA-256 in its own commit.
    sha = "ab" * 32
    r = _pin(repo, tag, "--sha256", sha)
    assert r.returncode == 0 and f'TS_PIN_TARBALL_SHA256="{sha}"' in (repo / "scripts" / "install.sh").read_text()
    assert _git(repo, "log", "-1", "--format=%s").stdout.strip() == f"Pin installer tarball SHA-256 for {tag}"


def test_refusals(repo):
    tag = f"v{VERSION}"
    _git(repo, "tag", "-a", tag, "-m", "x")
    (repo / "scratch.txt").write_text("dirty")
    assert "uncommitted changes" in _pin(repo, tag).stderr
    (repo / "scratch.txt").unlink()
    assert "tag must look like" in _pin(repo, "latest").stderr
    assert "64 lowercase hex" in _pin(repo, tag, "--sha256", "nothex").stderr
    # A tag whose commit has a different version is refused.
    _git(repo, "tag", "v9.9.9")
    assert "is not version 9.9.9" in _pin(repo, "v9.9.9").stderr
    # Re-pinning the same tag to a different commit (a moved tag) is refused.
    assert _pin(repo, tag).returncode == 0
    (repo / "x.txt").write_text("x")
    _git(repo, "add", "x.txt")
    _git(repo, "commit", "-qm", "more")
    _git(repo, "tag", "-f", "-a", tag, "-m", "moved")
    assert "Tags must not move" in _pin(repo, tag).stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_release_pin_shellcheck_clean():
    for shell in ("sh", "bash"):
        r = subprocess.run(["shellcheck", "-s", shell, str(PIN)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout
