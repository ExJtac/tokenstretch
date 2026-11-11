"""Shared fixtures: network block, doctor runner, credential-file guard."""
from __future__ import annotations

import io
import json
import shutil
import socket
from argparse import Namespace
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CRED_NAMES = {
    ".credentials.json", "auth.json", "credentials", "token",
    "oauth_creds.json", "gemini-credentials.json", "google_accounts.json",
    "oauth-keys.env",
}
CANARIES = (
    "CANARY_CREDENTIAL_DO_NOT_READ_CLAUDE",
    "CANARY_CREDENTIAL_DO_NOT_READ_CODEX",
    "CANARY_CREDENTIAL_DO_NOT_READ_CAVEMAN",
    "CANARY_CREDENTIAL_DO_NOT_READ_ANTHROPIC_CACHE",
    "CANARY_CREDENTIAL_DO_NOT_READ_GEMINI_OAUTH",
    "CANARY_CREDENTIAL_DO_NOT_READ_AIDER_OAUTH",
)


class NetworkBlockedError(RuntimeError):
    """Raised if a test tries to open an inet network connection."""


class CredentialReadError(RuntimeError):
    """Raised if production code (or a test) opens a credential file."""


def is_credential_path(path) -> bool:
    try:
        p = Path(path)
    except TypeError:
        return False
    parts = set(p.parts)
    if p.name in {".credentials.json", "auth.json", "credentials"} and parts & {".claude", ".codex", ".caveman"}:
        return True
    if p.name in {"oauth_creds.json", "gemini-credentials.json", "google_accounts.json"} and ".gemini" in parts:
        return True
    if p.name == "oauth-keys.env" and ".aider" in parts:
        return True
    if ".cache" in parts and "anthropic" in parts and p.name != "anthropic":
        return True
    if ".claude-mem" in parts and p.name.lower() in {".credentials.json", "auth.json", "credentials", "token"}:
        return True
    return False


# ---- in-process socket guard (autouse) ---------------------------------------

@pytest.fixture(autouse=True)
def _block_inet_sockets(monkeypatch):
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def _inet(sock) -> bool:
        return sock.family in (socket.AF_INET, socket.AF_INET6)

    def connect(self, address):
        if _inet(self):
            raise NetworkBlockedError(f"tests must not open network connections ({address})")
        return real_connect(self, address)

    def connect_ex(self, address):
        if _inet(self):
            raise NetworkBlockedError(f"tests must not open network connections ({address})")
        return real_connect_ex(self, address)

    def create_connection(address, *args, **kwargs):
        raise NetworkBlockedError(f"tests must not open network connections ({address})")

    def getaddrinfo(*args, **kwargs):
        raise NetworkBlockedError(f"tests must not resolve or open network connections ({args[:2]})")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


# ---- credential open guard (used around doctor / adapter calls) ---------------

@contextmanager
def credential_guard():
    import builtins

    real_open = builtins.open
    real_path_open = Path.open
    real_read_text = Path.read_text
    real_read_bytes = Path.read_bytes

    def _check(path):
        if is_credential_path(path):
            raise CredentialReadError(f"credential file was opened: {path}")

    def open_wrapper(file, *args, **kwargs):
        _check(file)
        return real_open(file, *args, **kwargs)

    def path_open(self, *args, **kwargs):
        _check(self)
        return real_path_open(self, *args, **kwargs)

    def read_text(self, *args, **kwargs):
        _check(self)
        return real_read_text(self, *args, **kwargs)

    def read_bytes(self, *args, **kwargs):
        _check(self)
        return real_read_bytes(self, *args, **kwargs)

    builtins.open = open_wrapper
    Path.open = path_open
    Path.read_text = read_text
    Path.read_bytes = read_bytes
    try:
        yield
    finally:
        builtins.open = real_open
        Path.open = real_path_open
        Path.read_text = real_read_text
        Path.read_bytes = real_read_bytes


def copy_fixture(name: str, tmp_path: Path) -> Path:
    dest = tmp_path / name
    shutil.copytree(FIXTURES / name, dest)
    return dest


def run_doctor(name: str, tmp_path: Path, *, privacy: bool = False, as_json: bool = True, plan: str | None = None):
    """Copy a fixture to tmp_path and run doctor against it (isolated env snapshot)."""
    dest = copy_fixture(name, tmp_path)
    args = Namespace(
        home=dest / "home",
        root=dest / "root",
        sys_snapshot=dest / "sys",
        plan=plan,
        privacy=privacy,
        json=as_json,
    )
    buf = io.StringIO()
    err = io.StringIO()
    from tokenstretch.cli import doctor

    with credential_guard(), redirect_stdout(buf), redirect_stderr(err):
        code = doctor(args)
    out = buf.getvalue()
    err_out = err.getvalue()
    for canary in CANARIES:
        assert canary not in out
        assert canary not in err_out
    findings = json.loads(out) if as_json else None
    return code, findings, out, dest


def finding_ids(findings, adapter: str | None = None) -> set[str]:
    return {f["id"] for f in findings if adapter is None or f.get("adapter") == adapter}


def findings_with(findings, fid: str, adapter: str | None = None) -> list[dict]:
    return [f for f in findings if f["id"] == fid and (adapter is None or f.get("adapter") == adapter)]
