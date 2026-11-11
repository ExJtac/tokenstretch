"""Command runner for installer fetch steps.

Tests inject FakeRunner, which records argv and never execs, so installs cannot
run during the suite and no network call is made. LiveRunner is only used from
the CLI on `install --apply`, and it refuses to start if pytest is imported.
"""
from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field


class NetworkInstallRefused(RuntimeError):
    """Raised if a live install command would run under pytest."""


@dataclass
class RunResult:
    argv: list[str]
    env: dict[str, str]
    network: bool
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    skipped: bool = False


class FakeRunner:
    """Records commands and pretends success. Never starts a process."""

    def __init__(self) -> None:
        self.calls: list[RunResult] = []

    def run(self, argv: list[str], env: dict[str, str] | None = None, *, network: bool = False, cwd: str | None = None) -> RunResult:
        result = RunResult(
            argv=list(argv),
            env=dict(env or {}),
            network=network,
            exit_code=0,
            stdout="fake-runner: command not executed",
            skipped=True,
        )
        self.calls.append(result)
        return result


class LiveRunner:
    """Executes fetch commands. Refuses to run inside the test suite."""

    def run(self, argv: list[str], env: dict[str, str] | None = None, *, network: bool = False, cwd: str | None = None) -> RunResult:
        if "pytest" in sys.modules:
            raise NetworkInstallRefused("refusing to run install commands under pytest")
        import os
        merged = dict(os.environ)
        merged.update(env or {})
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, env=merged, timeout=300, cwd=cwd)
            return RunResult(list(argv), dict(env or {}), network, proc.returncode, proc.stdout, proc.stderr, False)
        except (OSError, subprocess.SubprocessError) as exc:
            return RunResult(list(argv), dict(env or {}), network, 1, "", str(exc), False)


def default_runner():
    """Always fake under pytest; live only in a real --apply outside tests."""
    if "pytest" in sys.modules:
        return FakeRunner()
    return LiveRunner()
