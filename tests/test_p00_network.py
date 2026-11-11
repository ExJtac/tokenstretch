"""P00: the kit makes zero network calls; the suite must pass with the network blocked."""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "tokenstretch"

# Imports that would let doctor/bench (or a future module outside install-apply) hit the network.
IMPORT_RE = re.compile(
    r"^\s*(?:import\s+(urllib|requests|httpx|aiohttp|httplib)|"
    r"from\s+(urllib|requests|httpx|aiohttp|http\.client|httpx|httplib)\b)",
    re.M,
)


def test_p00_source_has_no_network_imports():
    hits = []
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if IMPORT_RE.search(text):
            hits.append(str(path.relative_to(REPO)))
    assert hits == []


@pytest.mark.p00_outer
def test_p00_suite_under_unshare_rn():
    if os.environ.get("TOKENSTRETCH_P00_INNER") == "1":
        pytest.skip("already inside the network-blocked inner run")
    probe = subprocess.run(["unshare", "-rn", "true"], capture_output=True, text=True)
    if probe.returncode != 0 or not Path("/usr/bin/unshare").exists() and shutil_which("unshare") is None:
        # probe failure is the real skip condition; also skip if unshare is missing
        if probe.returncode != 0:
            pytest.skip(f"unshare -rn unavailable: {probe.stderr.strip() or probe.stdout.strip() or probe.returncode}")
    env = os.environ.copy()
    env["TOKENSTRETCH_P00_INNER"] = "1"
    proc = subprocess.run(
        [
            # No route off the machine; loopback is brought up (when `ip` exists) so the
            # dashboard's 127.0.0.1 server tests run here too instead of skipping.
            "unshare", "-rn", "sh", "-c", 'ip link set lo up 2>/dev/null || true; exec "$@"', "sh",
            sys.executable, "-m", "pytest", "-q", "--tb=short",
            "-p", "no:cacheprovider",
            "-m", "not p00_outer",
            str(REPO / "tests"),
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"network-blocked suite failed (exit {proc.returncode})\n"
        f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    )


def shutil_which(name: str):
    import shutil
    return shutil.which(name)
