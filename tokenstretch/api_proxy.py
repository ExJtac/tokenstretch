"""Opt-in API-key proxy profile (Headroom or Kompressor, never both, never Clean).

Selected only with an explicit flag such as `install --profile api-proxy --proxy headroom`.
Dry-run by default. Headroom --apply is allowed after a hash-checked
`uv pip install --require-hashes` into ~/.tokenstretch/tools/headroom, except
on macOS below 14.0. Kompressor apply is allowed after the pinned release
binary's sha256 is verified.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .catalog import (
    HEADROOM_MACOS_UNTESTED_NOTE,
    HEADROOM_MCP_PORT,
    HEADROOM_PORT,
    KOMPRESSOR_PORT,
    headroom_lock_dest,
    headroom_macos_refusal,
    headroom_packaged_lock,
    headroom_wrapper_path,
    kompressor_asset_for_host,
    kompressor_cargo_fallback_argv,
    verify_headroom_lock,
)
from .identity import PRODUCT_NAME, STATE_DIRNAME
from .context import Context

ALLOWED_PROXIES = ("headroom", "kompressor")
ALLOWED_BINDS = frozenset({"127.0.0.1", "localhost", "::1"})
REFUSED_BIND_HOSTS = frozenset({
    "0.0.0.0", "::", "*", "[::]", "0:0:0:0:0:0:0:0",
})
# Desktop intercept keys — never write or keep these.
INTERCEPT_ENV_KEYS = ("HTTPS_PROXY", "NODE_EXTRA_CA_CERTS")

HEADROOM_TELEMETRY_ENV = {
    "HEADROOM_BEACON": "off",
    "HEADROOM_TELEMETRY": "off",
    "HEADROOM_UPDATE_CHECK": "off",
    "DO_NOT_TRACK": "1",
    "HEADROOM_OFFLINE": "1",
}
# Never set HEADROOM_ALLOW_UNAUTHENTICATED_BIND or HEADROOM_PROXY_TOKEN.
FORBIDDEN_HEADROOM_ENV = ("HEADROOM_ALLOW_UNAUTHENTICATED_BIND", "HEADROOM_PROXY_TOKEN")

# Documented env only. Do not set keep-warm (default off).
KOMPRESSOR_ENV = {
    "KOMPRESSOR__SERVER__LISTEN": f"127.0.0.1:{KOMPRESSOR_PORT}",
    "KOMPRESSOR__PROXY__WEBSOCKET_RELAY": "false",
}


class ApiProxyError(ValueError):
    """User-facing refusal for the api-proxy profile (exit set by the installer)."""

    def __init__(self, message: str, *, exit_code: int = 2):
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message


def normalize_proxy_selection(proxy) -> list[str]:
    """Return selected proxy ids. Empty list means none selected."""
    if proxy is None or proxy is False:
        return []
    if isinstance(proxy, (list, tuple, set)):
        items = [str(p).strip().lower() for p in proxy if str(p).strip()]
    else:
        items = [p.strip().lower() for p in str(proxy).replace(";", ",").split(",") if p.strip()]
    return items


def parse_bind_host(bind: str | None) -> str:
    """Return the host portion of a --bind value (address or address:port)."""
    raw = (bind or "127.0.0.1").strip()
    if not raw:
        return ""
    if raw.startswith("["):
        end = raw.find("]")
        if end == -1:
            return raw.lower()
        return raw[1:end].lower()
    # IPv4 or name, optional :port. Leave bare IPv6 alone (more than one colon).
    if raw.count(":") == 1:
        return raw.split(":", 1)[0].lower()
    return raw.lower()


def bind_is_loopback(bind: str | None) -> bool:
    host = parse_bind_host(bind)
    return host in ALLOWED_BINDS


def bind_is_refused(bind: str | None) -> bool:
    host = parse_bind_host(bind)
    if host in REFUSED_BIND_HOSTS:
        return True
    if host.startswith("0.0.0.0"):
        return True
    return not bind_is_loopback(bind)


def canonical_bind(bind: str | None) -> str:
    """Always write 127.0.0.1, even if the user passed localhost or ::1."""
    if bind_is_refused(bind):
        raise ApiProxyError(
            f"Refusing bind {bind!r}. The API-key proxy may listen on 127.0.0.1 only, never 0.0.0.0.",
            exit_code=2,
        )
    return "127.0.0.1"


def proxy_port(proxy_id: str) -> int:
    if proxy_id == "kompressor":
        return KOMPRESSOR_PORT
    return HEADROOM_PORT


def proxy_base_url(proxy_id: str, bind: str | None = "127.0.0.1") -> str:
    """Loopback origin with no path. Claude's ANTHROPIC_BASE_URL uses this form."""
    host = canonical_bind(bind)
    return f"http://{host}:{proxy_port(proxy_id)}"


def proxy_client_base_url(proxy_id: str, bind: str | None, *, adapter_id: str) -> str:
    """Adapter-specific base URL.

    Headroom v0.44.0 (source e2e/init and cli/init.py): Claude uses
    http://127.0.0.1:8787 with no path; Codex/OpenAI must use
    http://127.0.0.1:8787/v1. Kompressor is left pathless unless research
    quotes a suffix.
    """
    url = proxy_base_url(proxy_id, bind)
    if proxy_id == "headroom" and adapter_id in {"codex"}:
        return url.rstrip("/") + "/v1"
    return url


def proxy_env(proxy_id: str, bind: str | None = "127.0.0.1") -> dict[str, str]:
    url = proxy_base_url(proxy_id, bind)
    if proxy_id == "kompressor":
        env = dict(KOMPRESSOR_ENV)
        env["KOMPRESSOR__SERVER__LISTEN"] = f"{canonical_bind(bind)}:{KOMPRESSOR_PORT}"
    else:
        env = dict(HEADROOM_TELEMETRY_ENV)
        env["HEADROOM_HOST"] = canonical_bind(bind)
        env["HEADROOM_PORT"] = str(HEADROOM_PORT)
        for k in FORBIDDEN_HEADROOM_ENV:
            env.pop(k, None)
    env["ANTHROPIC_BASE_URL"] = url
    # Never set _CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL (research: third-party proxy).
    return env


def validate_profile_flags(*, profile: str, proxy, bind: str | None) -> tuple[str, str | None]:
    """Return (profile, selected_proxy_or_None) or raise ApiProxyError."""
    profile = (profile or "clean").strip().lower()
    selected = normalize_proxy_selection(proxy)
    if profile not in {"clean", "api-proxy"}:
        raise ApiProxyError(
            f"Unknown install profile {profile!r}. Use clean (default) or api-proxy.",
            exit_code=1,
        )
    if profile == "clean":
        if selected:
            raise ApiProxyError(
                "Headroom and Kompressor are never part of Clean. "
                "Pass --profile api-proxy --proxy headroom (or --proxy kompressor).",
                exit_code=2,
            )
        return "clean", None
    if len(selected) > 1 or set(selected) >= set(ALLOWED_PROXIES):
        raise ApiProxyError(
            "Choose at most one of Headroom or Kompressor. "
            "Passing both --proxy values is refused.",
            exit_code=2,
        )
    if not selected:
        raise ApiProxyError(
            "The api-proxy profile needs exactly one proxy. "
            "Pass --proxy headroom or --proxy kompressor.",
            exit_code=2,
        )
    name = selected[0]
    if name not in ALLOWED_PROXIES:
        raise ApiProxyError(
            f"Unknown --proxy {name!r}. Choose headroom or kompressor.",
            exit_code=1,
        )
    canonical_bind(bind)  # raises if 0.0.0.0 or non-loopback
    return "api-proxy", name


def _auth_without_plan(adapter, ctx: Context) -> str:
    saved = ctx.plan_override
    try:
        ctx.plan_override = None
        return adapter.auth_mode(ctx)
    finally:
        ctx.plan_override = saved


def subscription_auth_reasons(ctx: Context) -> list[str]:
    """Refuse when a detected Claude/Codex tool looks like subscription/OAuth.

    Detection uses auth_mode rules already in the adapters: env-var *names*
    and credential-file existence (stat only). Credential files are never opened.
    --plan overrides are ignored so a flag cannot bypass the ToS check.
    """
    from .adapters.claude_code import ClaudeCodeAdapter
    from .adapters.codex import CodexAdapter

    reasons: list[str] = []
    claude = ClaudeCodeAdapter()
    if claude.detect(ctx):
        mode = _auth_without_plan(claude, ctx)
        if mode == "subscription":
            reasons.append(
                "Claude Code looks like a subscription/OAuth login "
                "(CLAUDE_CODE_OAUTH_TOKEN or a credential file is present, "
                "and no API-key env name / apiKeyHelper / forceLoginMethod=api|console). "
                "The API-key proxy profile is API-key only and is refused here."
            )
    codex = CodexAdapter()
    if codex.detect(ctx):
        mode = _auth_without_plan(codex, ctx)
        if mode == "subscription":
            reasons.append(
                "Codex looks like a subscription/OAuth login "
                "(auth.json is present and forced_login_method is not api). "
                "The API-key proxy profile is API-key only and is refused here. "
                "Kompressor must not be used in front of a Codex subscription login "
                "(wrap codex reads auth.json); this is detected by stat only."
            )
    return reasons


def pins_block_apply(proxy_id: str | None = None, *, allow_unpinned_for_tests: bool = False) -> str | None:
    """Return a refusal message if this proxy cannot be applied.

    Headroom apply is allowed when the packaged hashed lock is present and
    contains the pinned wheel sha256. Kompressor apply is allowed when a
    pinned release binary exists for this host. macOS < 14 is handled
    separately by headroom_macos_refusal.
    """
    if not proxy_id:
        return None
    if proxy_id == "headroom":
        try:
            verify_headroom_lock()
        except Exception as exc:
            return str(exc)
        return None
    if proxy_id == "kompressor":
        if kompressor_asset_for_host() is not None:
            return None
        cargo = " ".join(kompressor_cargo_fallback_argv())
        return (
            "No pinned Kompressor 1.5.14 release binary for this platform "
            "(no linux aarch64 and no Windows assets). "
            f"A cargo fallback is documented (`{cargo}`); the crate is not on "
            "crates.io (crates.io 'kompressor' is a different project). Apply stays "
            "blocked here because that path has no artifact sha256."
        )
    return None


def _strip_hash_comments(text: str) -> str:
    lines = []
    for line in (text or "").splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if " #" in line:
            line = line[:line.index(" #")]
        lines.append(line)
    return "\n".join(lines)


def existing_wildcard_bind_reasons(ctx: Context, planned_contents: list[str]) -> list[str]:
    """Refuse if a planned file or existing kompressor config would bind 0.0.0.0.

    Comments (including the warning about the in-tree Docker CMD) are ignored.
    """
    reasons = []
    pattern = re.compile(r"0\.0\.0\.0|\[::\](?!\d)")
    for raw in planned_contents:
        if raw and pattern.search(_strip_hash_comments(raw)):
            reasons.append(
                "A planned proxy config would bind 0.0.0.0 (or [::]). "
                "The API-key proxy may listen on 127.0.0.1 only."
            )
            break
    cfg = ctx.home / ".config" / "kompressor" / "config.toml"
    if cfg.is_file():
        try:
            body = _strip_hash_comments(cfg.read_text(encoding="utf-8"))
        except OSError:
            body = ""
        if pattern.search(body):
            reasons.append(
                "Existing ~/.config/kompressor/config.toml binds 0.0.0.0. "
                "Refuse until that listen address is 127.0.0.1."
            )
    return reasons


def plan_headroom_runtime_files(ctx: Context) -> list[dict]:
    """Stage the hashed lock and a wrapper that exposes the venv entrypoint."""
    src = headroom_packaged_lock()
    dest = headroom_lock_dest(ctx.home)
    wrapper = headroom_wrapper_path(ctx.home)
    lock_text = src.read_text(encoding="utf-8")
    wrapper_body = (
        "#!/bin/sh\n"
        f"# {PRODUCT_NAME} Headroom 0.44.0 entrypoint from the dedicated venv.\n"
        f'BIN="$HOME/{STATE_DIRNAME}/tools/headroom/bin/headroom"\n'
        'if [ -x "$BIN" ]; then\n'
        '  exec "$BIN" "$@"\n'
        "fi\n"
        'echo "headroom is not installed in $BIN. Re-run: '
        'tokenstretch install --profile api-proxy --proxy headroom --apply" >&2\n'
        "exit 127\n"
    )
    changes = [
        {
            "path": str(dest),
            "kind": "modify" if dest.is_file() else "create",
            "content": lock_text,
            "reason": (
                "Packaged Headroom 0.44.0 universal hashed lock "
                "(tokenstretch/pins/headroom/requirements.txt). "
                "Install with uv pip install --require-hashes."
            ),
        },
        {
            "path": str(wrapper),
            "kind": "modify" if wrapper.is_file() else "create",
            "content": wrapper_body,
            "mode": 0o755,
            "reason": "Expose the Headroom entrypoint from ~/.tokenstretch/tools/headroom.",
        },
    ]
    return changes


def plan_proxy_file_changes(ctx: Context, proxy_id: str, bind: str | None = "127.0.0.1") -> list[dict[str, str]]:
    """Adapter-owned settings only (no shell rc). Manifest backup/undo covers these paths."""
    env = proxy_env(proxy_id, bind)
    changes: list[dict[str, str]] = []
    from .adapters import REGISTRY
    for ad in REGISTRY:
        planner = getattr(ad, "plan_api_proxy_settings", None)
        if not callable(planner):
            continue
        adapter_id = getattr(ad, "id", "") or ""
        base_url = proxy_client_base_url(proxy_id, bind, adapter_id=adapter_id)
        for ch in planner(ctx, proxy_id=proxy_id, base_url=base_url, env=dict(env)) or []:
            if not isinstance(ch, dict) or not ch.get("path"):
                continue
            item = {
                "path": str(ch["path"]),
                "kind": ch.get("kind") or "modify",
                "content": ch.get("content") or "",
                "reason": ch.get("reason") or "",
            }
            if ch.get("mode") is not None:
                item["mode"] = ch["mode"]
            changes.append(item)
    if proxy_id == "kompressor":
        changes.append(_plan_kompressor_config(ctx, bind))
    if proxy_id == "headroom":
        changes.extend(plan_headroom_runtime_files(ctx))
    return changes


def _plan_kompressor_config(ctx: Context, bind: str | None = "127.0.0.1") -> dict[str, str]:
    """Write documented keys only.

    [server] listen follows KOMPRESSOR__SERVER__LISTEN.
    [proxy] websocket_relay follows KOMPRESSOR__PROXY__WEBSOCKET_RELAY.
    Intercept stays off (do not set [proxy.intercept] listen). Keep-warm is left
    unset (default off). Never write /etc/kompressor/config.toml.
    """
    path = ctx.home / ".config" / "kompressor" / "config.toml"
    listen = f"{canonical_bind(bind)}:{KOMPRESSOR_PORT}"
    content = (
        "# tokenstretch API-key profile. Do not use /etc/kompressor/config.toml\n"
        "# (packaged default turns Desktop intercept ON) or the in-tree Dockerfile\n"
        "# (CMD binds 0.0.0.0:8817). Keep-warm is left unset (default off).\n"
        "#\n"
        "# [server] listen follows KOMPRESSOR__SERVER__LISTEN.\n"
        "# [proxy] websocket_relay follows KOMPRESSOR__PROXY__WEBSOCKET_RELAY.\n"
        "# Intercept stays off: do not set [proxy.intercept] listen.\n"
        "\n"
        "[server]\n"
        f'listen = "{listen}"\n'
        "\n"
        "[proxy]\n"
        "websocket_relay = false\n"
    )
    return {
        "path": str(path),
        "kind": "modify" if path.is_file() else "create",
        "content": content,
        "reason": (
            "Kompressor user config: KOMPRESSOR__SERVER__LISTEN / [server] listen "
            f"{listen}, websocket_relay false, intercept never enabled. "
            "Not /etc/kompressor/config.toml."
        ),
    }



def merge_file_changes(base: list[dict], extra: list[dict]) -> list[dict]:
    """Merge planned writes that share a path. JSON objects are deep-merged (hooks + env)."""
    by_path: dict[str, dict] = {}
    order: list[str] = []
    for ch in list(base) + list(extra):
        key = str(ch.get("path") or "")
        if not key:
            continue
        if key not in by_path:
            by_path[key] = dict(ch)
            order.append(key)
            continue
        existing = by_path[key]
        merged = _try_merge_json(existing.get("content") or "", ch.get("content") or "")
        if merged is not None:
            existing["content"] = merged
            reason_a = existing.get("reason") or ""
            reason_b = ch.get("reason") or ""
            if reason_b and reason_b not in reason_a:
                existing["reason"] = (reason_a + " " + reason_b).strip()
            if existing.get("kind") == "create" or ch.get("kind") == "create":
                # still create if the file did not exist at plan time
                pass
        else:
            # Last writer wins for non-JSON (should not happen for settings.json).
            by_path[key] = dict(ch)
    return [by_path[k] for k in order]


def _try_merge_json(a: str, b: str) -> str | None:
    try:
        da = json.loads(a) if a.strip() else {}
        db = json.loads(b) if b.strip() else {}
    except json.JSONDecodeError:
        return None
    if not isinstance(da, dict) or not isinstance(db, dict):
        return None
    return json.dumps(_deep_merge(da, db), indent=2) + "\n"


def _deep_merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def upsert_toml_key(text: str, key: str, value: str) -> str:
    """Set a top-level TOML string key, preserving the rest of the file."""
    pattern = re.compile(rf"(?m)^[ \t]*{re.escape(key)}[ \t]*=[ \t]*.*$")
    line = f'{key} = "{value}"'
    if text and pattern.search(text):
        return pattern.sub(line, text, count=1)
    body = text if text.endswith("\n") or not text else text + "\n"
    if body and not body.endswith("\n"):
        body += "\n"
    return body + line + "\n"


def manual_proxy_steps(proxy_id: str, bind: str = "127.0.0.1") -> list[str]:
    host = canonical_bind(bind)
    if proxy_id == "headroom":
        return [
            f"Start Headroom yourself, bound only to {host}: "
            f"`$HOME/{STATE_DIRNAME}/bin/headroom proxy --host {host} --port {HEADROOM_PORT} --no-telemetry` "
            f"(or the venv entrypoint at $HOME/{STATE_DIRNAME}/tools/headroom/bin/headroom). "
            "Do not run `headroom deploy` or `headroom install apply`. "
            "Do not set HEADROOM_ALLOW_UNAUTHENTICATED_BIND. "
            f"If you run `headroom mcp serve --transport http`, that defaults to {host}:{HEADROOM_MCP_PORT} "
            "(included in the port conflict check). "
            "`headroom wrap` is session-scoped; this profile points the agent through adapter-owned settings instead.",
            "Headroom's upload beacon and the daily PyPI update check are on by default. "
            "The kit writes HEADROOM_BEACON=off HEADROOM_TELEMETRY=off HEADROOM_UPDATE_CHECK=off "
            "DO_NOT_TRACK=1 HEADROOM_OFFLINE=1, and the start command passes --no-telemetry. "
            "Claude is pointed at http://127.0.0.1:8787 (no path); Codex/OpenAI at "
            "http://127.0.0.1:8787/v1 (Headroom v0.44.0 source).",
        ]
    cargo = " ".join(kompressor_cargo_fallback_argv())
    return [
        f"Start Kompressor yourself on the documented bind: "
        f"`kompressor serve --listen {host}:{KOMPRESSOR_PORT}`. "
        "Do not run `kompressor wrap` (wrap codex writes config.toml and reads auth.json). "
        f"Never enable Desktop intercept (MITM on {host}:8840) and never use "
        "/etc/kompressor/config.toml (packaged intercept is ON) or the in-tree Dockerfile "
        f"(it binds 0.0.0.0:{KOMPRESSOR_PORT}).",
        "The kit writes KOMPRESSOR__SERVER__LISTEN=127.0.0.1:8817 and "
        "KOMPRESSOR__PROXY__WEBSOCKET_RELAY=false. Keep-warm is left unset (default off). "
        f"Prefer a hashed release binary; cargo fallback if needed is `{cargo}` "
        "(the crate is not on crates.io).",
    ]
