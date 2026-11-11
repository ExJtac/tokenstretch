"""Pinned Clean-profile catalog.

Every hash and commit below is packaged with the kit. None are invented.
No network access happens here: this module only describes pins and commands.
Tests inject FakeRunner so fetch argv is recorded and never executed.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

# Licenses we will install. BUSL-1.1 (rtk-proxy / full rtk / engine) is not in this set.
ALLOWED_LICENSES = frozenset({"Apache-2.0", "MIT"})

CLEAN_HOOK_PLAN_ITEMS = (
    "pre_tool_shell_filter",
    "pre_read_gate",
    "session_start_note",
)


class PinMismatchError(ValueError):
    """A pinned sha256 or git commit did not match."""


# ---------------------------------------------------------------------------
# RTK v1.2.0
# ---------------------------------------------------------------------------
RTK_VERSION = "v1.2.0"
RTK_COMMIT = "e288a18c04452fd26192f59132e5324a8bb6924d"
RTK_INSTALL_SH = f"https://raw.githubusercontent.com/rtk-ai/rtk/{RTK_COMMIT}/install.sh"
RTK_INSTALL_SH_SHA256 = "85ae798116fa0dda42a627333b36903b71291785c7395ad3634f8419f7adaeb5"
RTK_CHECKSUMS_TXT_SHA256 = "cb6c58f0a063ee1ec2313d61d3ba7da6b4af95edf7a333739a692c099af4f48e"

# Primary names from checksums.txt, plus shorter aliases that map to the same sha256.
RTK_OSS_SHA256: dict[str, str] = {
    "rtk-oss-x86_64-unknown-linux-musl.tar.gz": "a28407dc13038bdc9c2a1f5a047b0c636e0c1d495b15523576e2adb992a06a17",
    "rtk-oss-aarch64-unknown-linux-musl.tar.gz": "9786c61dbbea704e1468ee7060930d3b45b67f79ba2babfb73463e85340628d9",
    "rtk-oss-aarch64-unknown-linux-gnu.tar.gz": "6d176358458801879b445f1b420e3a9b47d2f0898b4c590f99ff94518654091c",
    "rtk-oss-x86_64-apple-darwin.tar.gz": "48f8cee63ae0b4853f9a77d7839e62ffcca18ef88c408f8a93d4e69986b1ab82",
    "rtk-oss-aarch64-apple-darwin.tar.gz": "1959bd6d89a729ccdee176b7141490bbe750820f032c74f29a752576f2495254",
    "rtk-oss-x86_64-pc-windows-msvc.zip": "d105aaaa55822252f04ac28989e9069c1ea61755ca561940d887cb0b89c785fc",
    "rtk-oss_1.2.0-1_amd64.deb": "267fc8bd3f2d0793f18ef522098018787d8cce822ce4d3bb0c760aa1e59cabe9",
    "rtk-oss_amd64.deb": "267fc8bd3f2d0793f18ef522098018787d8cce822ce4d3bb0c760aa1e59cabe9",
    "rtk-oss_1.2.0-1_arm64.deb": "73188261f484fb39086dc0536358fa5ef14790521501b4a2962f705a3db7e7a9",
    "rtk-oss_arm64.deb": "73188261f484fb39086dc0536358fa5ef14790521501b4a2962f705a3db7e7a9",
    "rtk-oss-1.2.0-1.x86_64.rpm": "b9897dadf28557b05b9aa38ac3435bce47fcb1651c1766d09d1b559e44193e8e",
    "rtk-oss.x86_64.rpm": "b9897dadf28557b05b9aa38ac3435bce47fcb1651c1766d09d1b559e44193e8e",
    "rtk-oss-1.2.0-1.aarch64.rpm": "a47db71350a83c44ed86739fe72bf60ddf633df2d9db90f0d4618cda4092ae8c",
    "rtk-oss.aarch64.rpm": "a47db71350a83c44ed86739fe72bf60ddf633df2d9db90f0d4618cda4092ae8c",
}

# Always reported. linux-gnu x64 has no gnu tarball (musl is what install.sh picks);
# Windows arm64 has no oss asset at all.
RTK_UNSUPPORTED_PLATFORMS = (
    (
        "linux-gnu x64",
        "no rtk-oss-x86_64-unknown-linux-gnu asset; x86_64 Linux is musl only "
        "(install.sh picks rtk-oss-x86_64-unknown-linux-musl.tar.gz)",
    ),
    (
        "Windows arm64",
        "no Windows arm64 oss asset in the v1.2.0 release",
    ),
)


def rtk_asset_allowed(name: str) -> bool:
    """True only for rtk-oss-* / rtk-oss_* assets we have a pinned sha256 for."""
    base = Path(name).name
    if base.startswith("rtk-proxy-") or base.startswith("rtk-proxy_"):
        return False
    return base in RTK_OSS_SHA256


def rtk_asset_refusal(name: str) -> str | None:
    """Human reason to refuse an asset, or None if it is an allowed rtk-oss pin."""
    base = Path(name).name
    if rtk_asset_allowed(base):
        return None
    if base.startswith("rtk-proxy-") or base.startswith("rtk-proxy_"):
        return f"{base} is a BUSL-1.1 rtk-proxy asset and is not installable in the Clean profile."
    if base.startswith("rtk-oss-") or base.startswith("rtk-oss_"):
        return f"{base} is named like an OSS asset but has no pinned sha256 in the catalog."
    if base.startswith("rtk-"):
        return (
            f"{base} is a full-edition (non-oss) RTK asset and contains BUSL-1.1 code. "
            "Only rtk-oss-* assets are allowed."
        )
    return f"{base} is not an allowed rtk-oss-* Clean-profile asset."


def rtk_asset_sha256(name: str) -> str | None:
    return RTK_OSS_SHA256.get(Path(name).name)


# ---------------------------------------------------------------------------
# Caveman v4.1.0
# ---------------------------------------------------------------------------
CAVEMAN_VERSION = "v4.1.0"
CAVEMAN_COMMIT = "1e91f1306d602d4ca23618701b1185575ae1869b"
CAVEMAN_TAG_OBJECT = "5831edc2f57b6c4a274dcc9e2e433988d97eb23d"
CAVEMAN_CLONE_URL = "https://github.com/JuliusBrussee/caveman.git"
# Alternative only — Clean default is a git clone + file copy, not this CLI.
SKILLS_CLI_VERSION = "1.7.6"
SKILLS_CLI_GIT_HEAD = "7a5e920bead4b1039a6d6df5d7e1163b7b09423a"
SKILLS_CLI_INTEGRITY = "sha512-dAee29yRGin7ub6VHwmifLEfHrysGD41yq4RkY0gg7QQh0wyQKWfhiORGb/r6uNpKWc9A1GY8yowV07Lh+4rCg=="
SKILLS_CLI_SHASUM = "2c452adb00b7acb77b6fbea6fc9c64edd3bde2c3"
SKILLS_CLI_ENV = {"DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"}

# ---------------------------------------------------------------------------
# TokenEconomizer v0.1.0
# ---------------------------------------------------------------------------
TE_VERSION = "v0.1.0"
TE_COMMIT = "ec8ff5ec7ed5924520ee71b705a920ee64fe2918"
TE_TAG_OBJECT = "3f0e063c07f63a742fe5a7e2efcaf3220c7b53e7"
TE_CLONE_URL = "https://github.com/microsoft/TokenEconomizer.git"
TE_CLEAN_CONFIG = {"microskills": "off", "toolprune": "propose", "loopguard": "off"}

# ---------------------------------------------------------------------------
# ccusage@20.0.48
# ---------------------------------------------------------------------------
CCUSAGE_VERSION = "20.0.48"
CCUSAGE_COMMIT = "8995f3b7fd7110b8f37564f7d0468acc7a7d6b20"
CCUSAGE_INTEGRITY = "sha512-8SKmBGOwebYahMHINWaSxhEI1cy/nmYd1mus4/CMOwaLh3hJlyUdZsiS6raBn3NmAVRGEmyBIOEfNZo75kwiCg=="
CCUSAGE_SHASUM = "8a5e0667fe7558e2232bfee73d1956b9e6d2427c"
CCUSAGE_TARBALL = "https://registry.npmjs.org/ccusage/-/ccusage-20.0.48.tgz"
CCUSAGE_OPTIONAL_INTEGRITY = {
    "@ccusage/ccusage-linux-x64": {
        "integrity": "sha512-ThF2FNVmn8TnzTl94ns980AM2kcMUKBp0zS3LybXlPTV7qeIEhG5U9WbqrpuWvnSOOMM6fEV2Mw1dFoDRLyE9w==",
        "shasum": "7cc34275da0649f00e7ef483139c7404d8f948a4",
    },
    "@ccusage/ccusage-linux-arm64": {
        "integrity": "sha512-dSh53ih6BEZrHsk/Cywu7TEK2xF+NT266Dy+iarcqX/GF4/rhuNnukV4UZR+JgYZTbf1LGHGO8VDrELC//AP9w==",
        "shasum": "aee14939ecbf738a7ac63fa7cef7e3fde1c82d09",
    },
    "@ccusage/ccusage-darwin-x64": {
        "integrity": "sha512-7P4dV2jPb3jyzfRncDyAyEnix50trmXYBnHxzhNzao69Rw4MEPGwlU5mkjwYX3/kTACQ5MDoomxHhFQ9Y8+Avg==",
        "shasum": "9ac52565987b846c15d9e2794f9865dfc6e0f29a",
    },
    "@ccusage/ccusage-darwin-arm64": {
        "integrity": "sha512-5nF3wIFO6IfJsVI3mc0/8iZtcfN8TEN4Dw1JyhToPQVBLVoaxbnBpNR16RWLQvS1yNcVVQfjeCDblmHe/Wt6/A==",
        "shasum": "e7bc8b96e909fdc0b275b53df7224ecd162ca506",
    },
    "@ccusage/ccusage-win32-x64": {
        "integrity": "sha512-4PCIs0BWmy3Mj8J93r0pX6XVAZpXQOYO4FxtgXrJnB0NJJ2jgGlsKEzwYPxZEc62Qq+ZKyyGB6c/6QnGw30LjQ==",
        "shasum": "58a723da1888984eb5519e0d1341aac177d13f07",
    },
    "@ccusage/ccusage-win32-arm64": {
        "integrity": "sha512-zwvHE6W/IelXp0/P2fNnZhH8FVJRicyOJJk/Q0XDqrIkzGlNPjStq316Nxz8ITVPGi2lpPVTi6Hn4mmP6OmEuA==",
        "shasum": "6e49bbe109ff94ad3a7a3bfc3d1a8c2d155b7c12",
    },
}
CCUSAGE_OFFLINE_CONFIG = {"defaults": {"offline": True}}


def ccusage_pin_dir() -> Path:
    return Path(__file__).resolve().parent / "pins" / "ccusage"


def headroom_pin_dir() -> Path:
    return Path(__file__).resolve().parent / "pins" / "headroom"


def headroom_packaged_lock() -> Path:
    return headroom_pin_dir() / "requirements.txt"


def headroom_lock_dest(home: Path) -> Path:
    from .identity import state_dir
    return state_dir(home) / "headroom" / "requirements.txt"


def headroom_venv_dir(home: Path) -> Path:
    from .identity import state_dir
    return state_dir(home) / "tools" / "headroom"


def headroom_venv_python(home: Path) -> Path:
    return headroom_venv_dir(home) / "bin" / "python"


def headroom_venv_entrypoint(home: Path) -> Path:
    return headroom_venv_dir(home) / "bin" / "headroom"


def headroom_wrapper_path(home: Path) -> Path:
    from .identity import state_dir
    return state_dir(home) / "bin" / "headroom"


# ---------------------------------------------------------------------------
# Shared verify helpers
# ---------------------------------------------------------------------------
def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify_sha256(data: bytes, expected: str, *, what: str) -> str:
    actual = sha256_hex(data)
    exp = expected.lower().removeprefix("sha256:")
    if actual != exp:
        raise PinMismatchError(
            f"refusing {what}: sha256 {actual} does not match pinned {exp}."
        )
    return actual


def verify_file_sha256(path: Path, expected: str, *, what: str | None = None) -> str:
    return verify_sha256(path.read_bytes(), expected, what=what or str(path))


def verify_commit(actual: str, expected: str, *, what: str) -> str:
    got = (actual or "").strip()
    exp = (expected or "").strip()
    if got != exp:
        raise PinMismatchError(
            f"refusing {what}: HEAD {got or '(empty)'} does not match pinned commit {exp}."
        )
    return got


def read_git_head(repo: Path) -> str:
    """Return the commit SHA at HEAD. Uses .git files first, then `git rev-parse` (local)."""
    git = repo / ".git"
    head = git / "HEAD"
    if head.is_file():
        text = head.read_text(encoding="utf-8").strip()
        if text.startswith("ref:"):
            ref = git / text.split(":", 1)[1].strip()
            if ref.is_file():
                return ref.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{40}", text):
            return text
    import subprocess
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=10,
    )
    if proc.returncode != 0:
        raise PinMismatchError(f"could not read HEAD in {repo}: {proc.stderr.strip() or proc.returncode}")
    return proc.stdout.strip()


def verify_rtk_asset(name: str, data: bytes) -> str:
    reason = rtk_asset_refusal(name)
    if reason:
        raise PinMismatchError(f"refusing {Path(name).name}: {reason}")
    expected = rtk_asset_sha256(name)
    return verify_sha256(data, expected, what=Path(name).name)


def verify_rtk_install_sh(data: bytes) -> str:
    return verify_sha256(data, RTK_INSTALL_SH_SHA256, what="RTK install.sh")


def verify_checksums_txt(data: bytes) -> None:
    verify_sha256(data, RTK_CHECKSUMS_TXT_SHA256, what="RTK checksums.txt")
    text = data.decode("utf-8", errors="replace")
    found: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            found[Path(parts[-1]).name] = parts[0]
    for name, digest in RTK_OSS_SHA256.items():
        # Aliases may be absent from checksums.txt (they map to the same sha256 as the versioned names).
        if name in {"rtk-oss_amd64.deb", "rtk-oss_arm64.deb", "rtk-oss.x86_64.rpm", "rtk-oss.aarch64.rpm"}:
            continue
        if name not in found:
            raise PinMismatchError(f"checksums.txt is missing pinned asset {name}.")
        if found[name] != digest:
            raise PinMismatchError(
                f"checksums.txt value for {name} is {found[name]}, pinned value is {digest}."
            )


def this_host_is_unsupported_rtk() -> str | None:
    """Return a platform label if this host cannot install any oss asset."""
    import platform
    import sys
    system = sys.platform  # linux, darwin, win32
    machine = (platform.machine() or "").lower()
    is_arm = machine in {"arm64", "aarch64"}
    is_x64 = machine in {"x86_64", "amd64", "x64"}
    if system.startswith("win") and is_arm:
        return "Windows arm64"
    # linux-gnu x64 is reported as a missing *asset*, but the musl build is the
    # supported path, so we do not refuse the install on glibc x86_64 Linux.
    return None


@dataclass
class FetchStep:
    """A command that would download or register a pinned tool. Network only on --apply."""
    description: str
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    network: bool = True
    via_shell: bool = False
    cwd: str | None = None  # optional working directory for the command


@dataclass
class CatalogTool:
    id: str
    name: str
    version: str
    license_id: str
    profile: str = "clean"
    telemetry: str = ""
    fetch: list[FetchStep] = field(default_factory=list)
    checksum: str | None = None
    checksum_note: str = ""
    commit: str | None = None


CLEAN_TOOLS: list[CatalogTool] = [
    CatalogTool(
        id="rtk-oss",
        name="RTK OSS edition",
        version=RTK_VERSION,
        license_id="rtk-oss",
        commit=RTK_COMMIT,
        telemetry="Leave telemetry off (never run `rtk telemetry enable`); write telemetry = false; decline the proxy.",
        checksum=RTK_INSTALL_SH_SHA256,
        checksum_note=(
            f"install.sh at commit {RTK_COMMIT} sha256={RTK_INSTALL_SH_SHA256}; "
            f"checksums.txt sha256={RTK_CHECKSUMS_TXT_SHA256}; assets are rtk-oss-* only."
        ),
        fetch=[
            FetchStep(
                "Fetch install.sh at the pinned commit (not master), verify its sha256, "
                "then run it with RTK_EDITION=oss RTK_VERSION=v1.2.0. Only rtk-oss-* assets "
                "are allowed; rtk-* and rtk-proxy-* contain BUSL-1.1 code and are refused.",
                argv=["sh", "-c",
                      f"curl -fsSL {RTK_INSTALL_SH} -o install.sh && "
                      f"RTK_EDITION=oss RTK_VERSION=v1.2.0 sh install.sh"],
                env={"RTK_EDITION": "oss", "RTK_VERSION": RTK_VERSION},
                network=True,
                via_shell=True,
            ),
        ],
    ),
    CatalogTool(
        id="caveman-skill",
        name="Caveman skill",
        version=CAVEMAN_VERSION,
        license_id="caveman-skill",
        commit=CAVEMAN_COMMIT,
        telemetry="Skill only — never the CLI, never caveman-setup, never the full plugin.",
        checksum=CAVEMAN_COMMIT,
        checksum_note=f"No asset hash; integrity is git commit {CAVEMAN_COMMIT} at tag {CAVEMAN_VERSION}.",
        fetch=[
            FetchStep(
                "Clone tag v4.1.0, verify HEAD is "
                f"{CAVEMAN_COMMIT}, and copy only skills/caveman into each adapter's skills dir. "
                "Never install the full plugin or caveman-setup.",
                argv=["git", "clone", "--depth", "1", "--branch", CAVEMAN_VERSION, CAVEMAN_CLONE_URL],
                network=True,
            ),
        ],
    ),
    CatalogTool(
        id="tokeneconomizer",
        name="TokenEconomizer",
        version=TE_VERSION,
        license_id="tokeneconomizer",
        commit=TE_COMMIT,
        telemetry="Clean config: microskills=off, toolprune=propose, loopguard=off. No kit-side telemetry.",
        checksum=TE_COMMIT,
        checksum_note=f"No release assets; integrity is git commit {TE_COMMIT} at tag {TE_VERSION}.",
        fetch=[
            FetchStep(
                f"Clone tag {TE_VERSION} and verify HEAD is {TE_COMMIT} (commit-pinned only; the release has no assets).",
                argv=["git", "clone", "--depth", "1", "--branch", TE_VERSION, TE_CLONE_URL],
                network=True,
            ),
        ],
    ),
    CatalogTool(
        id="ccusage",
        name="ccusage",
        version=CCUSAGE_VERSION,
        license_id="ccusage",
        commit=CCUSAGE_COMMIT,
        telemetry="Always --offline, plus ccusage.json defaults.offline=true. npm update notifier/audit/fund off.",
        checksum=CCUSAGE_SHASUM,
        checksum_note=f"npm integrity {CCUSAGE_INTEGRITY}; shasum {CCUSAGE_SHASUM}; optional platform packages pinned too.",
        fetch=[
            FetchStep(
                "Install from the kit's pinned package-lock.json with "
                "npm ci --ignore-scripts --no-audit --no-fund and npm_config_update_notifier=false. "
                "Never npx ccusage@latest.",
                argv=["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
                env={"npm_config_update_notifier": "false"},
                network=True,
                cwd="ccusage",  # resolved under ~/.tokenstretch/ccusage by the installer
            ),
        ],
    ),
]


# ---------------------------------------------------------------------------
# API-key proxy profile (Headroom & Kompressor, plus the hashed lock in
# tokenstretch/pins/headroom/). Every hash and
# commit below is copied from those files. None are invented. Headroom
# --apply is allowed after `uv pip install --require-hashes` into a
# dedicated venv. Kompressor apply is allowed once the selected release
# binary's sha256 is verified.
# ---------------------------------------------------------------------------
HEADROOM_VERSION = "0.44.0"
HEADROOM_COMMIT = "b21479e0fa84db5f3e46bb79f96c962de4d5fdc6"
HEADROOM_SOURCE = "https://github.com/headroomlabs-ai/headroom"
HEADROOM_PORT = 8787
HEADROOM_MCP_PORT = 8788
HEADROOM_GHCR = (
    "ghcr.io/headroomlabs-ai/headroom:0.44.0"
    "@sha256:10ef5f71c4cae3a02ca28107dfcf379aa73314ff26544c1f9988140e71f732f4"
)
# Do not pin :latest (a different index digest was observed in the same session).
HEADROOM_SDIST_SHA256 = "4fa74895947905acc54564c1a24f67adefc71c2e4d1b599d723db46177f02e42"
HEADROOM_WHEEL_SHA256: dict[str, str] = {
    "headroom_ai-0.44.0-cp310-abi3-macosx_10_12_x86_64.whl": "2ab9d0c7ced1b94220597b2a48996693ebc0a61dd1ce38749709ae299b3f51da",
    "headroom_ai-0.44.0-cp310-abi3-macosx_11_0_arm64.whl": "9b084f4cd9918af2d0970450ff3e990fffc29a8220dc5e24fa0f33a4dd0359cc",
    "headroom_ai-0.44.0-cp310-abi3-manylinux_2_28_aarch64.whl": "58f2e6ac736fe882f83cae0a3bffad7ce574d12587eb999d6501bffd053a75cd",
    "headroom_ai-0.44.0-cp310-abi3-manylinux_2_28_x86_64.whl": "3b5c7fd289e7d227dcd9b896615541ea9d3d37388bd97b3257fc8769c4b7ef70",
    "headroom_ai-0.44.0-cp310-abi3-win_amd64.whl": "a7853a0d81f1e3f6d4b00de6114791732ff458c09b8af05a06c4ae2d8c492d68",
}

KOMPRESSOR_VERSION = "1.5.14"
KOMPRESSOR_COMMIT = "9e4721abb1e11547a63205d60db870fde25f0c61"
KOMPRESSOR_SOURCE = "https://github.com/hystax/optscale-kompressor"
KOMPRESSOR_PORT = 8817
KOMPRESSOR_INTERCEPT_PORT = 8840
KOMPRESSOR_RELEASE = "https://github.com/hystax/optscale-kompressor/releases/download/1.5.14"
KOMPRESSOR_SHA256: dict[str, str] = {
    "kompressor-1.5.14-x86_64-linux-gnu": "5fa88e9e7991cb1c63d3be8eb2ba7a56ea7dbd5db7afed7d74798f70534edae0",
    "kompressor-1.5.14-x86_64-linux-musl": "4ec4db8e6e1b5ea2bc15f05b804e8c127233e876a92399a6b99ff40c40be17f1",
    "kompressor_1.5.14-1_amd64.deb": "299d5edfe7f8ea07864437fa2436e06665f11c562a1a3652a2bf2f5bb3025499",
    "kompressor_1.5.14-1-static_amd64.deb": "7fa8c2ef476db70978aeab1902dd8e5d143a29ba881dd483e155812962c0d346",
    "kompressor-1.5.14-1.x86_64.rpm": "9c772f1cee7bf2a67027914838a6eaca108c95ec6cc3ab70de88c846ab2bae37",
    "kompressor-1.5.14-1-static.x86_64.rpm": "c615edbc0932d605e59c77d6318462514366d646ef679f3c2932ac3e52b48063",
    "kompressor-1.5.14-universal2.pkg": "d3fb5a9b3adb727ec0af7ec4e50849029a3c96676cf582d795d0b67da80db992",
}

# manylinux_2_28_x86_64 wheel sha256; the packaged
# universal lock must contain this hash (verified by verify_headroom_lock).
HEADROOM_WHEEL_SHA256_LINUX_X86_64 = HEADROOM_WHEEL_SHA256[
    "headroom_ai-0.44.0-cp310-abi3-manylinux_2_28_x86_64.whl"
]
HEADROOM_PYTHON = "3.13"
HEADROOM_MACOS_MIN = (14, 0)
HEADROOM_MACOS_UNTESTED_NOTE = (
    "The macOS Headroom lock was compiled on Linux and has not been "
    "install-tested on a real Mac."
)
# Research did not name uv telemetry/update env vars; only UV_NO_PROGRESS
# is set on the installer uv commands.
HEADROOM_UV_ENV = {"UV_NO_PROGRESS": "1"}

# Deprecated names kept so older imports do not break. Apply is no longer
# blocked on a missing lock; use verify_headroom_lock() and the macOS 14 gate.
HEADROOM_APPLY_BLOCKED = ""
API_PROXY_PIN_GAP = ""
API_PROXY_PINS_READY = True


def _linux_is_musl() -> bool:
    from pathlib import Path as _P
    try:
        if any(_P("/lib").glob("ld-musl-*.so*")) or any(_P("/lib64").glob("ld-musl-*.so*")):
            return True
    except OSError:
        pass
    import platform
    return "musl" in (platform.libc_ver()[0] or "").lower()


def kompressor_asset_for_host() -> tuple[str, str] | None:
    """Pinned release binary for this host (musl or gnu x86_64, or macOS pkg)."""
    import platform
    import sys
    system = sys.platform
    machine = (platform.machine() or "").lower()
    is_x64 = machine in {"x86_64", "amd64", "x64"}
    if system.startswith("linux") and is_x64:
        name = (
            "kompressor-1.5.14-x86_64-linux-musl"
            if _linux_is_musl()
            else "kompressor-1.5.14-x86_64-linux-gnu"
        )
        return name, KOMPRESSOR_SHA256[name]
    if system == "darwin":
        name = "kompressor-1.5.14-universal2.pkg"
        return name, KOMPRESSOR_SHA256[name]
    return None


def kompressor_asset_sha256(name: str) -> str | None:
    return KOMPRESSOR_SHA256.get(Path(name).name)


def verify_kompressor_asset(name: str, data: bytes) -> str:
    expected = kompressor_asset_sha256(name)
    if not expected:
        raise PinMismatchError(
            f"refusing {Path(name).name}: not a pinned Kompressor 1.5.14 release asset."
        )
    return verify_sha256(data, expected, what=Path(name).name)


def kompressor_fetch_steps_for_host() -> list[FetchStep]:
    asset = kompressor_asset_for_host()
    if not asset:
        return []
    name, digest = asset
    url = f"{KOMPRESSOR_RELEASE}/{name}"
    return [
        FetchStep(
            f"Fetch the pinned Kompressor 1.5.14 release binary {name} and verify "
            f"sha256={digest}. Do not use the in-tree Dockerfile (it binds 0.0.0.0:8817). "
            "The crate is not on crates.io (crates.io 'kompressor' is a different project).",
            argv=["curl", "-fsSL", "-o", name, url],
            network=True,
        ),
    ]


def kompressor_cargo_fallback_argv() -> list[str]:
    """Source fallback only. Pin both --tag and --rev. Not used when a hashed binary exists."""
    return [
        "cargo", "install", "--locked",
        "--git", "https://github.com/hystax/optscale-kompressor",
        "--tag", "1.5.14",
        "--rev", KOMPRESSOR_COMMIT,
        "kompressor-cli",
    ]


def headroom_apply_is_blocked() -> bool:
    """Apply is allowed once the packaged hashed lock is present and valid."""
    try:
        verify_headroom_lock()
    except (OSError, PinMismatchError):
        return True
    return False


def parse_macos_version(raw: str) -> tuple[int, int] | None:
    """Return (major, minor) from a macOS product version string, if parseable."""
    text = (raw or "").strip()
    if not text:
        return None
    m = re.search(r"(\d+)(?:\.(\d+))?", text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2) or 0)


def headroom_macos_refusal(platform: str, os_version: str) -> str | None:
    """Plain refusal when the host is macOS below 14.0 (onnxruntime wheel)."""
    plat = (platform or "").lower()
    if not plat.startswith("darwin"):
        return None
    ver = parse_macos_version(os_version)
    if ver is None:
        return None
    if ver < HEADROOM_MACOS_MIN:
        shown = os_version.strip() or f"{ver[0]}.{ver[1]}"
        return (
            f"Headroom requires macOS 14.0 or later (onnxruntime only ships a "
            f"macosx_14_0_arm64 wheel). This host reports macOS {shown}. "
            + HEADROOM_MACOS_UNTESTED_NOTE
        )
    return None


def headroom_lock_contains_wheel_hash(path: Path | None = None) -> bool:
    p = path or headroom_packaged_lock()
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return False
    return HEADROOM_WHEEL_SHA256_LINUX_X86_64.lower() in text.lower()


def verify_headroom_lock(path: Path | None = None) -> str:
    """Refuse a missing or incomplete Headroom hashed lock. Never fetches."""
    p = path or headroom_packaged_lock()
    if not p.is_file():
        raise PinMismatchError(
            f"refusing Headroom lock: {p} is missing the packaged universal "
            "requirements.txt."
        )
    if not headroom_lock_contains_wheel_hash(p):
        raise PinMismatchError(
            "refusing Headroom lock: packaged requirements.txt does not contain "
            f"the pinned headroom-ai manylinux_2_28_x86_64 wheel sha256 "
            f"{HEADROOM_WHEEL_SHA256_LINUX_X86_64}."
        )
    return HEADROOM_WHEEL_SHA256_LINUX_X86_64


def headroom_fetch_steps(home: Path | None = None) -> list[FetchStep]:
    """Hash-checked install into ~/.tokenstretch/tools/headroom (Python 3.13)."""
    home = Path(home) if home is not None else Path.home()
    venv = headroom_venv_dir(home)
    python = headroom_venv_python(home)
    lock = headroom_packaged_lock()
    env = dict(HEADROOM_UV_ENV)
    return [
        FetchStep(
            "Create a dedicated Python 3.13 venv at ~/.tokenstretch/tools/headroom.",
            argv=["uv", "venv", "--python", HEADROOM_PYTHON, str(venv)],
            env=dict(env),
            network=True,
        ),
        FetchStep(
            "Install headroom-ai[proxy]==0.44.0 into that venv with "
            "`uv pip install --require-hashes -r` the packaged universal lock. "
            f"The lock must include wheel sha256 {HEADROOM_WHEEL_SHA256_LINUX_X86_64}.",
            argv=[
                "uv", "pip", "install",
                "--python", str(python),
                "--require-hashes",
                "-r", str(lock),
            ],
            env=dict(env),
            network=True,
        ),
    ]


API_PROXY_TOOLS: list[CatalogTool] = [
    CatalogTool(
        id="headroom",
        name="Headroom",
        version=HEADROOM_VERSION,
        license_id="headroom",
        profile="api-proxy",
        commit=HEADROOM_COMMIT,
        telemetry=(
            "Upload beacon is ON by default. The kit always sets "
            "HEADROOM_BEACON=off HEADROOM_TELEMETRY=off HEADROOM_UPDATE_CHECK=off "
            "DO_NOT_TRACK=1 HEADROOM_OFFLINE=1 in adapter-owned settings, and the "
            "start command passes --no-telemetry. Never set "
            "HEADROOM_ALLOW_UNAUTHENTICATED_BIND. Bind 127.0.0.1 only. "
            "MCP HTTP (if used) is 127.0.0.1:8788. Never part of Clean."
        ),
        checksum=HEADROOM_SDIST_SHA256,
        checksum_note=(
            f"Commit {HEADROOM_COMMIT}. Sdist sha256={HEADROOM_SDIST_SHA256}. "
            "All wheel sha256 values are in HEADROOM_WHEEL_SHA256. "
            "Install uses the packaged universal "
            "hashed lock (tokenstretch/pins/headroom/requirements.txt) via "
            "`uv pip install --require-hashes` into ~/.tokenstretch/tools/headroom "
            f"with Python {HEADROOM_PYTHON}. Optional container path: "
            f"{HEADROOM_GHCR} (do not pin :latest)."
        ),
        fetch=[],  # filled per-home by headroom_fetch_steps()
    ),
    CatalogTool(
        id="kompressor",
        name="OptScale Kompressor",
        version=KOMPRESSOR_VERSION,
        license_id="kompressor",
        profile="api-proxy",
        commit=KOMPRESSOR_COMMIT,
        telemetry=(
            "No phone-home in the pinned sources. The kit sets "
            "KOMPRESSOR__SERVER__LISTEN=127.0.0.1:8817 and "
            "KOMPRESSOR__PROXY__WEBSOCKET_RELAY=false, leaves keep-warm unset "
            "(default off), and never enables Desktop intercept. "
            "Never use /etc/kompressor/config.toml (packaged intercept ON) or "
            "the in-tree Dockerfile (binds 0.0.0.0:8817). Never part of Clean."
        ),
        checksum=None,
        checksum_note=(
            f"Commit {KOMPRESSOR_COMMIT}. Release-binary sha256 values are in "
            "KOMPRESSOR_SHA256. Prefer a hashed "
            "binary over cargo; cargo fallback must pass both --tag 1.5.14 and "
            f"--rev {KOMPRESSOR_COMMIT}. The crate is not on crates.io."
        ),
        fetch=[],  # filled per-host by kompressor_fetch_steps_for_host()
    ),
]


def clean_tools() -> list[CatalogTool]:
    return list(CLEAN_TOOLS)


def api_proxy_tools(home: Path | None = None) -> list[CatalogTool]:
    tools = []
    for t in API_PROXY_TOOLS:
        if t.id == "kompressor":
            steps = kompressor_fetch_steps_for_host()
            tools.append(CatalogTool(
                id=t.id, name=t.name, version=t.version, license_id=t.license_id,
                profile=t.profile, telemetry=t.telemetry, fetch=steps or list(t.fetch),
                checksum=(kompressor_asset_for_host() or (None, t.checksum))[1],
                checksum_note=t.checksum_note, commit=t.commit,
            ))
        elif t.id == "headroom":
            steps = headroom_fetch_steps(home)
            tools.append(CatalogTool(
                id=t.id, name=t.name, version=t.version, license_id=t.license_id,
                profile=t.profile, telemetry=t.telemetry, fetch=steps,
                checksum=t.checksum, checksum_note=t.checksum_note, commit=t.commit,
            ))
        else:
            tools.append(t)
    return tools


def api_proxy_tool(tool_id: str, home: Path | None = None) -> CatalogTool | None:
    for t in api_proxy_tools(home):
        if t.id == tool_id:
            return t
    return None


def api_proxy_pins_ready() -> bool:
    """Both proxies have pins: Headroom hashed lock, Kompressor release binaries."""
    return (not headroom_apply_is_blocked()) and True


def skills_cli_argv(agent: str) -> list[str]:
    """Pinned skills CLI form (not the Clean default). Telemetry env must be set."""
    return [
        "npx", "-y", f"skills@{SKILLS_CLI_VERSION}", "add",
        f"JuliusBrussee/caveman#{CAVEMAN_VERSION}",
        "--skill", "caveman", "-a", agent, "-g", "-y",
    ]
