"""The only place the product's display and runtime names are defined.

Keep this file in sync with pyproject.toml (dist name, console script, package).
"""
from __future__ import annotations

from pathlib import Path

# Shown in --version, reports, and user-facing text.
PRODUCT_NAME = "tokenstretch"
VERSION = "0.4.0"

# argv0 / console-script name (pyproject [project.scripts]).
CLI_NAME = "tokenstretch"

# Python import package directory name (pyproject packages.find).
PACKAGE_NAME = "tokenstretch"

# Packaging / dist name (pyproject [project].name).
DIST_NAME = "tokenstretch"

# Directory under the user's home for manifests, backups, hooks, and pins.
# Default: ~/.tokenstretch
STATE_DIRNAME = ".tokenstretch"

# Env prefix for kit-owned variables (e.g. TOKENSTRETCH_CCUSAGE).
ENV_PREFIX = "TOKENSTRETCH"


def state_dir(home: Path | str) -> Path:
    return Path(home) / STATE_DIRNAME
