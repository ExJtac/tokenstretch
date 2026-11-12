"""Host/ecosystem adapter: third-party tools, ports, timers, and cross-agent checks.

Tool-specific paths for the third-party ecosystem live here (not in cli/context).
Credential files are stat'd (and checked for holders) but never opened.
"""
from __future__ import annotations

import hashlib
import json
import stat
import re
from pathlib import Path

from ..context import Context
from ..identity import ENV_PREFIX, PRODUCT_NAME, STATE_DIRNAME
from ..files import read_json, read_toml, text_if_small, walk_named
from ..util import is_loopback_url
from .base import Adapter, ConfigLocation, DetectResult, InstructionFile, SetupItem
from . import host_logs

# Known local-proxy ports → tool names (doctor-checks.md W10 / P04).
PORT_TOOLS: dict[int, tuple[str, ...]] = {
    8787: ("Caveman", "Headroom"),
    8788: ("Headroom MCP",),
    8817: ("Kompressor",),
    8840: ("Kompressor intercept",),
    3425: ("magpie",),
    7778: ("Tamp",),
    8080: ("Squeezr",),
    8081: ("Squeezr",),
    20128: ("OmniRoute",),
}
CLAUDE_MEM_PORTS = range(37700, 37800)

OFFICIAL_HOSTS = (
    "anthropic.com", "api.anthropic.com", "claude.ai",
    "openai.com", "api.openai.com", "chatgpt.com",
)
OFFICIAL_HOLDERS = re.compile(r"^(claude|codex)(\.exe)?$", re.I)
CONFIG_DIR_VARS = ("CLAUDE_CONFIG_DIR", "CODEX_HOME")
WARMUP_RE = re.compile(
    r"(\bclaude\b.*\s-p\b|\bclaude\b.*--print|\bcodex\b.*\bexec\b|\bmagpie\b.*warm)",
    re.I,
)
INSTRUCTION_NAMES = {"CLAUDE.md", "AGENTS.md", "GEMINI.md", ".cursorrules", ".clinerules", ".windsurfrules"}


def _capped_list(items: list[str], limit: int = 5) -> str:
    """Unique-preserving display: up to `limit` items, then 'and N more'."""
    if len(items) <= limit:
        return ", ".join(items)
    return ", ".join(items[:limit]) + f" and {len(items) - limit} more"
SENSITIVE_NAMES = {".credentials.json", "auth.json", "credentials"}


def _sensitive(path: Path) -> bool:
    parts = path.parts
    if path.name in SENSITIVE_NAMES and any(p in {".claude", ".codex", ".caveman"} for p in parts):
        return True
    if ".cache" in parts and "anthropic" in parts:
        return True
    if ".claude-mem" in parts and path.name.lower() in SENSITIVE_NAMES | {"token"}:
        return True
    return False


def _json(path: Path):
    if _sensitive(path):
        return None
    data = read_json(path)
    if isinstance(data, dict) and "__error__" not in data:
        return data
    return None



def _local_compression_proxy_label(url: str) -> str | None:
    """Name the documented local compression proxy for a loopback base URL, if any."""
    raw = (url or "").strip()
    if not raw:
        return None
    # Parse host:port without urllib (P00 forbids importing urllib).
    # Paths such as http://127.0.0.1:8787/v1 (Headroom Codex/OpenAI form) still match.
    port = None
    m = re.search(r":(\d+)(?:[/\s]|$)", raw)
    if m:
        try:
            port = int(m.group(1))
        except ValueError:
            port = None
    if port == 8787:
        return "A local compression proxy (Headroom and Caveman both default to port 8787)"
    if port == 8817:
        return "Kompressor"
    return None


def _toml(path: Path):
    if _sensitive(path):
        return None
    data = read_toml(path)
    if isinstance(data, dict) and "__error__" not in data:
        return data
    return None


def _text(path: Path) -> str:
    if _sensitive(path):
        return ""
    return text_if_small(path) or ""


def _truthy(value) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _walk_get(data: dict, *keys):
    cur = data
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def _tool_for_port(port: int, process: str = "") -> str | None:
    if port in CLAUDE_MEM_PORTS:
        return "claude-mem"
    names = PORT_TOOLS.get(port)
    if not names:
        return None
    proc = (process or "").lower()
    if len(names) > 1:
        for n in names:
            if n.lower() in proc:
                return n
        return " or ".join(names)
    return names[0]


def _env_aliases(text: str, name: str) -> list[str]:
    vals = re.findall(
        rf"(?:export\s+|set\s+-gx\s+)?{re.escape(name)}[= ]\s*['\"]?([^\s'\"]+)",
        text or "",
    )
    # alias foo='VAR=value cmd'
    vals += re.findall(
        rf"alias\s+\S+=['\"](?:export\s+)?{re.escape(name)}=([^\s'\"]+)",
        text or "",
    )
    return vals


HOME_SKIP = frozenset({"Library", ".Trash", "Applications", "Movies", "Music", "Pictures", "AppData", ".cache", ".npm",
                       ".cargo", ".rustup", ".local", "go", "snap"})


class HostAdapter(Adapter):
    id = "host"
    display_name = "Host"
    tier = 2
    vendor_terms = ""
    implemented_checks = frozenset({
        "W10", "W11", "W12", "W17", "W18",
        "T02", "T03", "T04", "T05", "T07",
        "P01", "P02", "P03", "P04", "P06",
    })

    def detect(self, ctx: Context) -> DetectResult:
        return DetectResult(installed=True, auth_mode="local", config_roots=[str(ctx.home)])

    def config_locations(self, ctx: Context) -> list[ConfigLocation]:
        creds = [
            ctx.home / ".claude" / ".credentials.json",
            ctx.home / ".codex" / "auth.json",
            ctx.home / ".caveman" / "credentials",
        ]
        locs = [ConfigLocation(p, "credential", ctx.exists(p), sensitive=True) for p in creds]
        cache = ctx.home / ".cache" / "anthropic"
        locs.append(ConfigLocation(cache, "credential", ctx.exists(cache), sensitive=True))
        return locs

    def checks(self, ctx: Context) -> list:
        out = []
        out += self._w10(ctx)
        out += self._w11(ctx)
        out += self._w12(ctx)
        out += self._w17(ctx)
        out += self._w18(ctx)
        out += self._kompressor_relay_t01(ctx)
        out += self._t02(ctx)
        out += self._t03(ctx)
        out += self._t04(ctx)
        out += self._t05(ctx)
        out += self._t07(ctx)
        return out

    def privacy_checks(self, ctx: Context) -> list:
        return (self._p01(ctx) + self._p02(ctx) + self._p03(ctx) + self._p04(ctx)
                + self._kompressor_bind_p04(ctx) + self._p06(ctx))

    def default_log_paths(self, ctx: Context) -> list[Path]:
        return host_logs.default_paths(ctx)

    def sniff_log(self, path: Path, first_lines: list[str]) -> bool:
        return host_logs.sniff(path, first_lines)

    def parse_log(self, path: Path) -> list:
        recs, notices = host_logs.parse_path(path, self.id)
        existing = getattr(self, "last_log_notices", None)
        if existing is None:
            self.last_log_notices = list(notices)
        else:
            existing.extend(notices)
        return recs

    def read_usage_logs(self, ctx: Context, since_days: int = 30) -> list:
        import time
        from ..usage import drop_forked_replays, record_in_window
        cutoff = time.time() - since_days * 86400
        self.last_log_notices = []
        records = []
        for path in self.default_log_paths(ctx):
            recs = self.parse_log(path)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = cutoff
            for r in recs:
                if r.ts:
                    if record_in_window(r, cutoff, None):
                        records.append(r)
                elif mtime >= cutoff:
                    records.append(r)
        return drop_forked_replays(records)

    # ---- discovery of third-party tools --------------------------------------
    def _others(self):
        from . import REGISTRY
        return [a for a in REGISTRY if a.id != self.id]

    def _subscription(self, ctx: Context) -> bool:
        if ctx.plan_override == "subscription":
            return True
        if ctx.plan_override == "api":
            return False
        return any(a.auth_mode(ctx) == "subscription" for a in self._others())

    def _rtk(self, ctx: Context) -> bool:
        if ctx.which("rtk") or ctx.exists(ctx.home / ".claude" / "RTK.md") or ctx.exists(ctx.home / ".config" / "rtk"):
            return True
        if ctx.process_matches(r"\brtk\b"):
            return True
        return any("rtk" in cmd.lower() for a in self._others() for cmd, _ in a.hook_commands(ctx))

    def _rtk_proxy(self, ctx: Context) -> bool:
        text = _text(ctx.home / ".config" / "rtk" / "pipeline.toml")
        return "rtk:proxy" in text

    def _rtk_telemetry_on(self, ctx: Context) -> bool:
        cfg = _toml(ctx.home / ".config" / "rtk" / "config.toml") or {}
        if _truthy(cfg.get("telemetry")) or _truthy(_walk_get(cfg, "telemetry", "enabled")):
            return True
        json_cfg = _json(ctx.home / ".config" / "rtk" / "config.json") or {}
        return _truthy(json_cfg.get("telemetry")) or _truthy(_walk_get(json_cfg, "telemetry", "enabled"))

    def _caveman_cli(self, ctx: Context) -> bool:
        return bool(
            ctx.which("caveman")
            or ctx.exists(ctx.home / ".config" / "caveman")
            or ctx.exists(ctx.home / ".caveman")
            or ctx.process_matches(r"\bcaveman\b")
        )

    def _caveman_cfgs(self, ctx: Context) -> list[tuple[Path, dict]]:
        out = []
        for p in (
            ctx.home / ".config" / "caveman" / "config.json",
            ctx.home / ".caveman" / "cloud.json",
            ctx.home / ".caveman-cloud" / "config.json",
        ):
            data = _json(p)
            if data:
                out.append((p, data))
        return out

    def _caveman_telemetry_off(self, ctx: Context) -> bool:
        v, _ = ctx.env_or_rc("DO_NOT_TRACK")
        if v and v != "0":
            return True
        cav, _ = ctx.env_or_rc("CAVEMAN_TELEMETRY")
        if cav is not None and cav != "":
            # 1|true|on enables; any other non-empty value disables.
            return not _truthy(cav)
        for _, data in self._caveman_cfgs(ctx):
            tel = data.get("telemetry")
            if tel is False or (isinstance(tel, dict) and tel.get("enabled") is False):
                return True
            if isinstance(tel, str) and tel.lower() in {"off", "false", "0"}:
                return True
        return False

    def _caveman_cloud_on(self, ctx: Context) -> bool:
        for _, data in self._caveman_cfgs(ctx):
            for key in ("routing", "auto", "dashboard", "dashboardSync", "cloud", "gateway"):
                val = data.get(key)
                if val is True or (isinstance(val, dict) and (val.get("enabled") is True or val.get("on") is True)):
                    return True
            providers = data.get("providers") or {}
            if isinstance(providers, dict) and (
                providers.get("cloud") is True
                or (isinstance(providers.get("cloud"), dict) and providers["cloud"].get("enabled") is not False
                    and providers.get("cloud") not in {False, "off"})
            ):
                if providers.get("cloud") not in {False, "off", None}:
                    if providers.get("cloud") is True or (
                        isinstance(providers.get("cloud"), dict)
                        and _truthy(providers["cloud"].get("enabled", True))
                    ):
                        return True
        return False

    def _caveman_keepwarm(self, ctx: Context) -> bool:
        for _, data in self._caveman_cfgs(ctx):
            for key in ("wasteFixes", "waste_fixes", "keepWarm", "keep_warm"):
                if _truthy(data.get(key)) or (
                    isinstance(data.get(key), dict) and _truthy(data[key].get("enabled"))
                ):
                    return True
        return False

    def _adapter_settings_env(self, ctx: Context, name: str) -> tuple[str | None, str]:
        """Look up an env name in adapter-owned settings (not shell rc)."""
        v, where = ctx.env_or_rc(name)
        if v is not None:
            return v, where or "env"
        for a in self._others():
            fn = getattr(a, "settings_env", None)
            if not callable(fn):
                continue
            entry = (fn(ctx) or {}).get(name)
            if entry:
                val, src = entry if isinstance(entry, tuple) else (entry, "")
                if val is not None:
                    return str(val), src or "settings"
        return None, ""

    def _headroom(self, ctx: Context) -> bool:
        keys = ("HEADROOM_TELEMETRY", "HEADROOM_BEACON", "HEADROOM_OFFLINE", "HEADROOM_UPDATE_CHECK")
        return bool(
            ctx.which("headroom")
            or ctx.process_matches(r"\bheadroom\b")
            or ctx.exists(ctx.home / ".headroom")
            or any(self._adapter_settings_env(ctx, k)[0] for k in keys)
            or any(l.process.lower().find("headroom") >= 0 for l in ctx.listeners)
        )

    def _headroom_flag_off(self, value: str | None) -> bool:
        if value is None or str(value).strip() == "":
            return False
        return str(value).strip().lower() in {"off", "false", "0", "no", "disable", "disabled"}

    def _headroom_beacon_off(self, ctx: Context) -> bool:
        """Beacon uploads are on by default (fail-open). Explicit off, or a covering switch."""
        offline, _ = self._adapter_settings_env(ctx, "HEADROOM_OFFLINE")
        if offline is not None and str(offline).strip().lower() in {"1", "true", "yes", "on"}:
            return True
        dnt, _ = self._adapter_settings_env(ctx, "DO_NOT_TRACK")
        if dnt is not None and not self._headroom_flag_off(dnt):
            return True
        tel, _ = self._adapter_settings_env(ctx, "HEADROOM_TELEMETRY")
        if tel is not None and tel.strip().lower() == "off":
            return True
        beacon, _ = self._adapter_settings_env(ctx, "HEADROOM_BEACON")
        return self._headroom_flag_off(beacon)

    def _headroom_update_check_off(self, ctx: Context) -> bool:
        """Update check is a separate outbound path; TELEMETRY/BEACON off does not stop it."""
        offline, _ = self._adapter_settings_env(ctx, "HEADROOM_OFFLINE")
        if offline is not None and str(offline).strip().lower() in {"1", "true", "yes", "on"}:
            return True
        v, _ = self._adapter_settings_env(ctx, "HEADROOM_UPDATE_CHECK")
        return self._headroom_flag_off(v)

    def _headroom_telemetry_off(self, ctx: Context) -> bool:
        """P01 stays silent only when the beacon and the update check are both off."""
        return self._headroom_beacon_off(ctx) and self._headroom_update_check_off(ctx)

    def _kompressor(self, ctx: Context) -> bool:
        return bool(
            ctx.which("kompressor")
            or ctx.exists(ctx.home / ".config" / "kompressor")
            or ctx.process_matches(r"\bkompressor\b")
            or ctx.listener_on(8817)
            or ctx.listener_on(8840)
        )

    def _kompressor_keepwarm(self, ctx: Context) -> bool:
        for key in (
            "KOMPRESSOR__PROXY__CACHE_KEEP_WARM",
            "KOMPRESSOR__PROXY__PROVIDERS__ANTHROPIC__CACHE_KEEP_WARM",
        ):
            v, _ = self._adapter_settings_env(ctx, key)
            if _truthy(v):
                return True
        cfg, _ = self._kompressor_effective_config(ctx)
        if _truthy(_walk_get(cfg, "proxy", "cache_keep_warm")):
            return True
        node = _walk_get(cfg, "proxy", "providers", "anthropic") or {}
        if isinstance(node, dict) and _truthy(node.get("cache_keep_warm") or node.get("cacheKeepWarm")):
            return True
        return False

    def _kompressor_config_paths(self, ctx: Context) -> list[tuple[Path, str]]:
        """Discovery order for Kompressor config, plus /etc for packaged-intercept warnings."""
        paths: list[tuple[Path, str]] = []
        v, _ = ctx.env_or_rc("KOMPRESSOR_CONFIG")
        if v:
            paths.append((Path(v).expanduser(), "KOMPRESSOR_CONFIG"))
        paths.append((ctx.root / "kompressor.toml", "project"))
        xdg, _ = ctx.env_or_rc("XDG_CONFIG_HOME")
        if xdg:
            paths.append((Path(xdg).expanduser() / "kompressor" / "config.toml", "xdg"))
        paths.append((ctx.home / ".config" / "kompressor" / "config.toml", "user"))
        paths.append((Path("/etc/kompressor/config.toml"), "packaged"))
        return paths

    def _kompressor_effective_config(self, ctx: Context) -> tuple[dict, str]:
        # First existing discover-path file wins. /etc is last and only used if nothing else exists,
        # or always inspected separately for intercept warnings.
        for path, src in self._kompressor_config_paths(ctx):
            if src == "packaged":
                continue
            if path.is_file():
                return _toml(path) or {}, str(path)
        packaged = Path("/etc/kompressor/config.toml")
        if packaged.is_file():
            return _toml(packaged) or {}, str(packaged)
        return {}, ""

    def _kompressor_listen(self, ctx: Context) -> tuple[str, str]:
        v, where = self._adapter_settings_env(ctx, "KOMPRESSOR__SERVER__LISTEN")
        if v:
            return v, where or "env"
        cfg, src = self._kompressor_effective_config(ctx)
        server = cfg.get("server") if isinstance(cfg.get("server"), dict) else {}
        if server.get("listen"):
            return str(server["listen"]), f"{src}:server.listen"
        return "127.0.0.1:8817", "default"

    def _kompressor_websocket_relay_on(self, ctx: Context) -> tuple[bool, str]:
        v, where = self._adapter_settings_env(ctx, "KOMPRESSOR__PROXY__WEBSOCKET_RELAY")
        if v is not None:
            return (_truthy(v) and str(v).strip().lower() not in {"false", "0", "off", "no"}), where or "env"
        cfg, src = self._kompressor_effective_config(ctx)
        proxy = cfg.get("proxy") if isinstance(cfg.get("proxy"), dict) else {}
        if "websocket_relay" in proxy:
            return bool(_truthy(proxy.get("websocket_relay"))), f"{src}:proxy.websocket_relay"
        # Code and packaged defaults are true.
        return True, "default true"

    def _kompressor_intercept_on(self, ctx: Context) -> tuple[bool, str]:
        v, where = self._adapter_settings_env(ctx, "KOMPRESSOR__PROXY__INTERCEPT__LISTEN")
        if v:
            return True, where or "env"
        # Check every known file, including /etc (packaged intercept ON).
        for path, src in self._kompressor_config_paths(ctx):
            if not path.is_file():
                continue
            cfg = _toml(path) or {}
            intercept = _walk_get(cfg, "proxy", "intercept") or {}
            if isinstance(intercept, dict) and intercept.get("listen"):
                return True, f"{path}:proxy.intercept.listen"
            if _truthy(cfg.get("intercept") if not isinstance(intercept, dict) else None):
                return True, f"{path}:intercept"
        return False, ""

    def _listen_is_loopback(self, listen: str) -> bool:
        from ..api_proxy import bind_is_loopback
        return bind_is_loopback(listen)

    def _kompressor_relay_t01(self, ctx: Context) -> list:
        if not self._kompressor(ctx):
            return []
        on, src = self._kompressor_websocket_relay_on(ctx)
        if not on:
            return []
        sev = "high" if self._subscription(ctx) else "med"
        return [self.finding(
            "T01", "tos-third-party-auth", sev, "tos",
            "Kompressor websocket_relay is on (it defaults true). It can relay "
            "ChatGPT-subscription WebSocket sessions. Set KOMPRESSOR__PROXY__WEBSOCKET_RELAY=false.",
            "Set KOMPRESSOR__PROXY__WEBSOCKET_RELAY=false and keep [proxy] websocket_relay = false "
            "in ~/.config/kompressor/config.toml. Do not use kompressor wrap against a subscription login.",
            "Kompressor 1.5.14 proxy.websocket_relay",
            file=src, key="websocket_relay", value=True,
        )]

    def _kompressor_bind_p04(self, ctx: Context) -> list:
        if not self._kompressor(ctx):
            return []
        findings = []
        on, src = self._kompressor_intercept_on(ctx)
        if on:
            findings.append(self.finding(
                "P04", "listening-ports", "high", "privacy",
                "Kompressor Desktop intercept is on (a local MITM CA, default 127.0.0.1:8840). "
                "Leave intercept off for the API-key profile. Do not use /etc/kompressor/config.toml "
                "(the packaged default turns intercept ON).",
                "Comment out [proxy.intercept] listen in the user config, and do not point "
                "KOMPRESSOR_CONFIG at the packaged file.",
                "packaging/kompressor.toml intercept",
                file=src, key="proxy.intercept.listen", value="on",
            ))
        listen, lsrc = self._kompressor_listen(ctx)
        if listen and not self._listen_is_loopback(listen):
            findings.append(self.finding(
                "P04", "listening-ports", "high", "privacy",
                f"Kompressor is configured to listen on {listen}, which is not loopback. "
                "Bind 127.0.0.1:8817 only. The in-tree Dockerfile binds 0.0.0.0:8817 and must not be used.",
                "Set KOMPRESSOR__SERVER__LISTEN=127.0.0.1:8817 or [server] listen to 127.0.0.1:8817.",
                "KOMPRESSOR__SERVER__LISTEN",
                file=lsrc, key="listen", value=listen,
            ))
        return findings

    def _claude_mem(self, ctx: Context) -> bool:
        return ctx.exists(ctx.home / ".claude-mem") or bool(ctx.which("claude-mem")) or any(
            l.port in CLAUDE_MEM_PORTS for l in ctx.listeners
        )

    def _claude_mem_settings(self, ctx: Context) -> dict:
        return _json(ctx.home / ".claude-mem" / "settings.json") or {}

    def _mem_key(self, data: dict, *names):
        lower = {str(k).lower(): (k, v) for k, v in data.items()}
        for n in names:
            if n.lower() in lower:
                return lower[n.lower()][1]
            # also env-style in the file
        return None

    def _llm_router(self, ctx: Context) -> bool:
        return bool(
            ctx.which("llm-router")
            or ctx.exists(ctx.home / ".llm-router")
            or ctx.process_matches(r"llm-router")
            or any("llm-router" in cmd.lower() for a in self._others() for cmd, _ in a.hook_commands(ctx))
        )

    def _magpie(self, ctx: Context) -> bool:
        return bool(ctx.which("magpie") or ctx.process_matches(r"\bmagpie\b") or ctx.listener_on(3425)
                    or ctx.exists(ctx.home / ".magpie"))

    def _tamp(self, ctx: Context) -> bool:
        return bool(ctx.which("tamp") or ctx.process_matches(r"\btamp\b") or ctx.listener_on(7778))

    def _squeezr(self, ctx: Context) -> bool:
        return bool(ctx.which("squeezr") or ctx.process_matches(r"\bsqueezr\b")
                    or ctx.listener_on(8080) or ctx.listener_on(8081))

    def _omniroute(self, ctx: Context) -> bool:
        return bool(ctx.which("omniroute") or ctx.process_matches(r"\bomniroute\b") or ctx.listener_on(20128))

    def token_tools(self, ctx: Context) -> list[dict]:
        """Third-party token savers seen on this machine, with the telemetry state doctor reads."""
        out = []

        def add(name: str, telemetry: str, note: str = "") -> None:
            out.append({"name": name, "installed": True, "telemetry": telemetry, "note": note})

        if self._rtk(ctx):
            add("RTK", "on" if self._rtk_telemetry_on(ctx) else "off", "off by default")
        if self._caveman_cli(ctx):
            add("Caveman", "off" if self._caveman_telemetry_off(ctx) else "on",
                "" if self._caveman_telemetry_off(ctx) else "run `caveman telemetry off` or set DO_NOT_TRACK=1")
        if self._headroom(ctx):
            add("Headroom", "off" if self._headroom_telemetry_off(ctx) else "on",
                "" if self._headroom_telemetry_off(ctx) else "set HEADROOM_OFFLINE=1 (covers beacon and update check)")
        if self._claude_mem(ctx):
            tel = self._mem_key(self._claude_mem_settings(ctx), "telemetry", "telemetryEnabled", "telemetry_enabled",
                                "analytics", "analyticsEnabled")
            on = tel is None or _truthy(tel) or str(tel).lower() in {"on", "enabled"}
            add("claude-mem", "on" if on else "off", "`npx claude-mem telemetry disable`" if on else "")
        for name, present in (("Kompressor", self._kompressor), ("llm-router", self._llm_router),
                              ("magpie", self._magpie), ("tamp", self._tamp), ("squeezr", self._squeezr),
                              ("OmniRoute", self._omniroute)):
            if present(ctx):
                add(name, "not checked", "see doctor findings for this tool")
        return out

    # ---- W10 port map / chain ------------------------------------------------
    def _w10(self, ctx: Context) -> list:
        hops: list[tuple[str, str, str]] = []  # (label, where, key)
        https, https_where = ctx.env_or_rc("HTTPS_PROXY")
        if not https:
            for a in self._others():
                val, src = (a.settings_env(ctx) or {}).get("HTTPS_PROXY", (None, ""))
                if val:
                    https, https_where = val, src
                    break
        if https and (is_loopback_url(https) or "127.0.0.1" in https or "localhost" in https):
            hops.append((f"HTTPS_PROXY={https}", https_where or "env", "HTTPS_PROXY"))
        for a in self._others():
            for b in a.base_urls(ctx):
                if is_loopback_url(b.url) or "127.0.0.1" in b.url or "localhost" in b.url:
                    hops.append((b.url, b.where, b.key))
        named_listeners = []
        for lis in ctx.listeners:
            tool = _tool_for_port(lis.port, lis.process)
            if tool:
                named_listeners.append((tool, lis))
        # Only the host emits a finding when we can name a tool, see a chain, or see HTTPS_PROXY.
        if not named_listeners and not (https and hops):
            return []
        if not named_listeners and len(hops) < 2 and not https:
            return []
        tools = []
        for tool, lis in named_listeners:
            tools.append(f"{tool} on {lis.address}:{lis.port}")
        for label, _, _ in hops:
            if label not in tools:
                tools.append(label)
        chain = len(named_listeners) >= 2 or (bool(named_listeners) and bool(hops)) or len({h[0] for h in hops}) >= 2
        if not named_listeners and not https and not chain:
            return []
        names = ", ".join(tools) if tools else "a local proxy"
        sev = "high" if self._subscription(ctx) else "med"
        msg = f"Your AI traffic is going through {names} first."
        if chain:
            msg += " Chained middlemen can strip the caching discount and double-process your messages."
        ev = {
            "port": named_listeners[0][1].port if named_listeners else None,
            "process": named_listeners[0][1].process if named_listeners else "",
            "key": hops[0][2] if hops else "listener",
            "value": names,
            "file": hops[0][1] if hops else (f"port {named_listeners[0][1].port}" if named_listeners else ""),
        }
        return [self.finding(
            "W10", "chained-proxies", sev, "waste", msg,
            "Keep at most one proxy, and only with an API key. Remove the base URL for subscription use.",
            "CC-ENV, CC-CACHE, tool READMEs",
            **{k: v for k, v in ev.items() if v is not None},
        )]

    def _w11(self, ctx: Context) -> list:
        present = []
        if self._rtk(ctx):
            present.append("RTK hook")
        if self._rtk_proxy(ctx):
            present.append("rtk-proxy")
        if self._headroom(ctx):
            present.append("Headroom")
        if self._kompressor(ctx):
            present.append("Kompressor")
        if self._caveman_keepwarm(ctx) or (self._caveman_cli(ctx) and ctx.process_matches(r"caveman")):
            # CLI waste-fixes overlap with proxies; skill-only does not count as a compressor.
            if self._caveman_cli(ctx):
                present.append("Caveman CLI")
        if self._squeezr(ctx):
            present.append("Squeezr")
        if self._tamp(ctx):
            present.append("Tamp")
        # unique, need 2+
        uniq = list(dict.fromkeys(present))
        if len(uniq) < 2:
            return []
        return [self.finding(
            "W11", "dup-compressor", "med", "waste",
            "Two tools are both trying to shrink the same text. You get the cost and risk of both, but not double the savings. "
            f"Installed: {', '.join(uniq)}.",
            "Keep RTK (local hook) in the Clean profile; one proxy max in API-key mode.",
            "tools.md overlap map",
            key="compressors", value=uniq,
        )]

    def _w12(self, ctx: Context) -> list:
        present = []
        if self._llm_router(ctx):
            present.append("llm-router")
        if self._caveman_cloud_on(ctx) or (self._caveman_cli(ctx) and self._caveman_cloud_on(ctx)):
            present.append("Caveman routing")
        elif self._caveman_cli(ctx):
            # routing may be in config under routing.enabled
            for _, data in self._caveman_cfgs(ctx):
                if data.get("routing") or _walk_get(data, "routing", "enabled"):
                    present.append("Caveman routing")
                    break
        mem = self._claude_mem_settings(ctx) if self._claude_mem(ctx) else {}
        provider = self._mem_key(mem, "provider", "CLAUDE_MEM_PROVIDER")
        if provider and str(provider).lower() not in {"claude", "anthropic", "", "subscription"}:
            present.append("claude-mem provider swap")
        if self._omniroute(ctx):
            present.append("OmniRoute")
        if self._magpie(ctx):
            present.append("magpie")
        uniq = list(dict.fromkeys(present))
        if len(uniq) < 2 and "llm-router" not in uniq:
            # Also flag a hook model override plus CLAUDE_CODE_SUBAGENT_MODEL when a router is present.
            return []
        if len(uniq) < 2 and "llm-router" in uniq:
            # llm-router alone is still a high finding (its own audit found $0 savings).
            pass
        elif len(uniq) < 2:
            return []
        sev = "high" if "llm-router" in uniq else "med"
        return [self.finding(
            "W12", "multi-router", sev, "waste",
            "Several tools are each picking which AI model to use. They fight each other and add delay. "
            f"Installed: {', '.join(uniq)}. Native settings do this for free."
            if len(uniq) >= 2 else
            "llm-router is installed. Its own audit found $0.00 in verified savings and a median +12.7 s delay.",
            "Uninstall the routers; use the agent's native model settings instead.",
            "llm-router audit, CC-SUB, CC-ENV",
            key="routers", value=uniq,
        )]

    def _w17(self, ctx: Context) -> list:
        files: list[tuple[Path, str]] = []
        seen: set[Path] = set()
        for root in (ctx.root, ctx.home):
            # A home folder can hold millions of files (app data, media, the trash). Leave those out
            # and stop after a few seconds; instruction files don't live there.
            for path in walk_named(root, INSTRUCTION_NAMES, max_depth=4, skip=HOME_SKIP, budget_s=5.0):
                try:
                    resolved = path.resolve()
                except OSError:
                    continue
                if resolved in seen or _sensitive(path):
                    continue
                seen.add(resolved)
                text = _text(path)
                if text and text.strip():
                    files.append((path, text.strip()))
        groups: dict[str, list[Path]] = {}
        for path, text in files:
            digest = hashlib.sha256(text.encode()).hexdigest()
            groups.setdefault(digest, []).append(path)
        dup_groups = [list(dict.fromkeys(p)) for p in groups.values() if len(p) >= 2]
        if not dup_groups:
            return []
        # One grouped finding (not one per duplicate set) to keep doctor output readable.
        all_paths = [p for g in dup_groups for p in g]
        uniq_names = list(dict.fromkeys(p.name for p in all_paths))
        n = len(dup_groups)
        sets = "1 set" if n == 1 else f"{n} sets"
        return [self.finding(
            "W17", "dup-instructions", "med", "waste",
            f"The same instruction text is loaded from more than one file ({sets} of duplicates, "
            f"{len(all_paths)} files; names: {_capped_list(uniq_names)}). "
            "Many agents read several of these, so you pay for the rules twice.",
            "Keep one AGENTS.md (or the file your agent documents) and thin pointers in the others. "
            "`tokenstretch trim` shows duplicated blocks across the files that load together.",
            "agents.md (universal instruction-file checks)",
            file=_capped_list([str(p) for p in all_paths]), key="files", value=uniq_names,
        )]

    def _w18(self, ctx: Context) -> list:
        findings = []
        # Cline auto-approve is owned by the Cline adapter (including the CLI default).
        candidates = [
            ctx.home / ".cursor" / "cli-config.json",
        ]
        keys = ("autoApprove", "auto_approve", "auto-approve", "yolo", "defaultApproval", "allowedToolsYolo")
        for path in candidates:
            data = _json(path)
            if not data:
                continue
            for k in keys:
                if k in data and data[k] not in {False, None, "false", "off", 0, "0"}:
                    findings.append(self.finding(
                        "W18", "auto-approve-default", "info", "waste",
                        "A tool is set to auto-approve actions unattended. That is risky outside a sandboxed bench copy.",
                        "Turn auto-approve off in everyday configs. The bench must pass --auto-approve false.",
                        "agents.md (Cline CLI auto-approve default)",
                        file=str(path), key=k, value=data[k],
                    ))
                    break
        return findings

    # ---- ToS -----------------------------------------------------------------
    def _t02(self, ctx: Context) -> list:
        findings = []
        cred_claude = ctx.home / ".claude" / ".credentials.json"
        cred_codex = ctx.home / ".codex" / "auth.json"
        cred_caveman = ctx.home / ".caveman" / "credentials"
        cache_anth = ctx.home / ".cache" / "anthropic"

        known = []
        if self._kompressor(ctx) and ctx.exists(cred_codex):
            known.append(("Kompressor", cred_codex))
        if self._magpie(ctx):
            known.append(("magpie", cred_claude if ctx.exists(cred_claude) else cred_codex))
        if self._llm_router(ctx) and ctx.exists(cache_anth):
            known.append(("llm-router", cache_anth))
        if self._caveman_cli(ctx) and ctx.exists(cred_caveman):
            known.append(("Caveman", cred_caveman))
        mem = self._claude_mem_settings(ctx) if self._claude_mem(ctx) else {}
        auth = self._mem_key(mem, "CLAUDE_MEM_CLAUDE_AUTH_METHOD", "claudeAuthMethod", "authMethod")
        ev, _ = ctx.env_or_rc("CLAUDE_MEM_CLAUDE_AUTH_METHOD")
        auth = ev or auth
        if self._claude_mem(ctx) and (auth is None or str(auth).lower() in {"subscription", "long-lived", "long_lived"}
                                      or _truthy(self._mem_key(mem, "longLivedToken", "long_lived_token"))):
            known.append(("claude-mem", cred_claude))

        for tool, path in known:
            findings.append(self.finding(
                "T02", "tos-cred-reader", "high", "tos",
                f"{tool} reads your saved AI login. That's both a security risk and against the plan terms.",
                "Uninstall it, or reconfigure it to an API key.",
                "CC-LEGAL, tool READMEs/SECURITY",
                file=str(path), key="tool", value=tool,
            ))

        for path in (cred_claude, cred_codex):
            if not ctx.exists(path):
                continue
            for proc in ctx.holders_of(path):
                if OFFICIAL_HOLDERS.match(proc.strip().split("/")[-1]):
                    continue
                findings.append(self.finding(
                    "T02", "tos-cred-reader", "high", "tos",
                    f"{proc} has your saved AI login file open. That's both a security risk and against the plan terms.",
                    "Stop that process and uninstall the tool that opened the login file.",
                    "CC-LEGAL",
                    file=str(path), process=proc, key="holder", value=proc,
                ))
        return findings

    def _t03(self, ctx: Context) -> list:
        findings = []
        mem = self._claude_mem_settings(ctx) if self._claude_mem(ctx) else {}
        rotation = self._mem_key(
            mem, "multiKeyRotation", "multi_key_rotation", "quotaFallback", "quota_fallback",
            "MULTI_KEY_ROTATION", "QUOTA_FALLBACK",
        )
        if rotation not in {None, False, "false", "off", 0, "0"}:
            findings.append(self.finding(
                "T03", "tos-multi-account", "high", "tos",
                "You've set things up to jump to another account or key when one runs out. "
                "Both Anthropic and OpenAI forbid getting around usage limits.",
                "Remove multi-key rotation and quota fallback. Upgrade your plan or add API credits instead.",
                "OAI-TOS, OAI-SA, CC-LEGAL, claude-mem docs",
                file=str(ctx.home / ".claude-mem" / "settings.json"),
                key="multiKeyRotation/quotaFallback", value=rotation,
            ))
        magpie_cfg = _json(ctx.home / ".magpie" / "config.json") or {}
        if self._magpie(ctx) and (
            _truthy(magpie_cfg.get("failover")) or _truthy(_walk_get(magpie_cfg, "failover", "enabled"))
        ):
            findings.append(self.finding(
                "T03", "tos-multi-account", "high", "tos",
                "magpie is configured to fail over between accounts when one runs out.",
                "Remove the failover config.",
                "OAI-TOS, magpie README",
                file=str(ctx.home / ".magpie" / "config.json"), key="failover", value=True,
            ))
        rc = ctx.rc_text or ""
        for var in CONFIG_DIR_VARS:
            vals = set(_env_aliases(rc, var))
            env_v, _ = ctx.env_or_rc(var)
            if env_v:
                vals.add(env_v)
            if len(vals) > 1:
                findings.append(self.finding(
                    "T03", "tos-multi-account", "high", "tos",
                    f"More than one {var} is configured in your environment or shell aliases, "
                    "which looks like jumping between accounts.",
                    "Use a single login directory. Upgrade your plan instead of swapping accounts.",
                    "OAI-TOS, OAI-SA, CC-LEGAL",
                    file="rc", key=var, value=sorted(vals),
                ))
        return findings

    def _t04(self, ctx: Context) -> list:
        findings = []
        blobs = [ctx.crontab] + list(ctx.timers)
        for blob in blobs:
            if blob and WARMUP_RE.search(blob):
                findings.append(self.finding(
                    "T04", "tos-warmup", "high", "tos",
                    "Something on your computer pings the AI on a schedule to 'warm up' your limits or cache. "
                    "That can count as gaming usage limits, and it also burns quota.",
                    "Remove the timer and any keep-warm setting.",
                    "OAI-SA §3.3(i), tool READMEs",
                    key="schedule", value=blob.strip().splitlines()[0][:200],
                    file="crontab" if blob == ctx.crontab else "timers",
                ))
                break
        scheduled = ctx.home / ".claude"
        if scheduled.is_dir():
            try:
                hits = [p for p in scheduled.iterdir() if p.name.startswith("scheduled")]
            except OSError:
                hits = []
            if hits:
                findings.append(self.finding(
                    "T04", "tos-warmup", "high", "tos",
                    "A scheduled Claude job is installed. Scheduled pings can count as gaming usage limits.",
                    "Remove the scheduled job.",
                    "OAI-SA §3.3(i)",
                    file=str(hits[0]), key="scheduled", value=hits[0].name,
                ))
        if self._kompressor_keepwarm(ctx):
            findings.append(self.finding(
                "T04", "tos-warmup", "high" if self._subscription(ctx) else "med", "tos",
                "Kompressor keep-warm is on. It re-sends traffic on a timer to keep the cache alive, which burns quota.",
                "Disable CACHE_KEEP_WARM / cache_keep_warm.",
                "Kompressor README, OAI-SA §3.3(i)",
                key="cache_keep_warm", value=True,
            ))
        if self._caveman_keepwarm(ctx):
            findings.append(self.finding(
                "T04", "tos-warmup", "high" if self._subscription(ctx) else "med", "tos",
                "Caveman waste-fixes keep-warm is on (re-sends with max_tokens:0).",
                "Disable Caveman keep-warm / waste-fixes.",
                "Caveman SECURITY.md",
                key="keepWarm", value=True,
            ))
        # OpenCode warming and Aider keepalive are owned by those adapters.
        return findings

    def _t05(self, ctx: Context) -> list:
        findings = []
        ev, where = ctx.env_or_rc("NODE_EXTRA_CA_CERTS")
        if not ev:
            for a in self._others():
                ev, where = (a.settings_env(ctx) or {}).get("NODE_EXTRA_CA_CERTS", (None, ""))
                if ev:
                    break
        if ev:
            findings.append(self.finding(
                "T05", "tos-mitm-cert", "high", "tos",
                "NODE_EXTRA_CA_CERTS is set, so a custom certificate can read encrypted AI traffic.",
                "Remove the CA and unset NODE_EXTRA_CA_CERTS / HTTPS_PROXY.",
                "CC-ENV, Kompressor README",
                env="NODE_EXTRA_CA_CERTS", key="NODE_EXTRA_CA_CERTS", value=ev, file=where,
            ))
        https, hwhere = ctx.env_or_rc("HTTPS_PROXY")
        if https and (is_loopback_url(https) or "127.0.0.1" in https or "localhost" in https):
            findings.append(self.finding(
                "T05", "tos-mitm-cert", "high", "tos",
                f"HTTPS_PROXY points at a local address ({https}), a common MITM intercept pattern.",
                "Unset HTTPS_PROXY and remove any locally generated CA.",
                "CC-ENV, Kompressor README",
                env="HTTPS_PROXY", key="HTTPS_PROXY", value=https, file=hwhere,
            ))
        for port, label in ((8840, "Kompressor intercept"), (8081, "Squeezr"), (20128, "OmniRoute")):
            if ctx.listener_on(port):
                findings.append(self.finding(
                    "T05", "tos-mitm-cert", "high", "tos",
                    f"{label} is listening on port {port}. That intercepts encrypted traffic with a local CA.",
                    "Stop the intercept and remove the CA.",
                    "CC-ENV, tool READMEs",
                    port=port, key="listener", value=label,
                ))
        for name in ctx.trust_certs:
            low = name.lower()
            if any(tag in low for tag in ("kompressor", "squeezr", "omniroute", "headroom", "magpie")):
                findings.append(self.finding(
                    "T05", "tos-mitm-cert", "high", "tos",
                    f"A locally generated CA named {name} is in the trust store, so a tool can read encrypted traffic.",
                    "Remove the CA from the trust store.",
                    "Kompressor README, T05 [reasoning]",
                    file=name, key="ca", value=name,
                ))
        return findings

    def _t07(self, ctx: Context) -> list:
        findings = []
        if self._claude_mem(ctx):
            mem = self._claude_mem_settings(ctx)
            auth = self._mem_key(mem, "CLAUDE_MEM_CLAUDE_AUTH_METHOD", "claudeAuthMethod", "authMethod")
            ev, _ = ctx.env_or_rc("CLAUDE_MEM_CLAUDE_AUTH_METHOD")
            auth = ev or auth
            observer = self._mem_key(mem, "observer", "observerEnabled", "observer_enabled")
            observer_on = observer is None or _truthy(observer)
            if observer_on and (auth is None or str(auth).lower() in {"", "subscription"}):
                findings.append(self.finding(
                    "T07", "tos-sdk-on-subscription", "high", "tos",
                    "A background helper uses your Claude plan login to run its own AI calls. "
                    "Anthropic says plan logins are for Claude Code and its apps; helpers should use an API key.",
                    "Set CLAUDE_MEM_CLAUDE_AUTH_METHOD=api-key, or disable the observer.",
                    "CC-LEGAL, claude-mem docs",
                    file=str(ctx.home / ".claude-mem" / "settings.json"),
                    key="CLAUDE_MEM_CLAUDE_AUTH_METHOD", value=auth or "unset",
                ))
        oauth, where = ctx.env_or_rc("CLAUDE_CODE_OAUTH_TOKEN")
        if oauth and (self._claude_mem(ctx) or ctx.process_matches(r"claude-mem|agent-sdk")):
            findings.append(self.finding(
                "T07", "tos-sdk-on-subscription", "high", "tos",
                "A background helper is configured to use CLAUDE_CODE_OAUTH_TOKEN. "
                "Plan logins are for Claude Code and native apps, not third-party daemons.",
                "Remove CLAUDE_CODE_OAUTH_TOKEN from the helper and use an API key.",
                "CC-LEGAL",
                env="CLAUDE_CODE_OAUTH_TOKEN", key="CLAUDE_CODE_OAUTH_TOKEN", value="set", file=where,
            ))
        return findings

    # ---- Privacy -------------------------------------------------------------
    def _p01(self, ctx: Context) -> list:
        findings = []
        if self._caveman_cli(ctx) and not self._caveman_telemetry_off(ctx):
            findings.append(self.finding(
                "P01", "telemetry-on", "high", "privacy",
                "Caveman CLI sends usage reports to its maker by default, including your IP address. "
                "Turn it off with: `caveman telemetry off` or DO_NOT_TRACK=1.",
                "Run `caveman telemetry off` or set DO_NOT_TRACK=1. Doctor reads the config; it does not call a status command that might hit the network.",
                "Caveman SECURITY.md",
                key="caveman_telemetry", value="on",
            ))
        if self._headroom(ctx) and not self._headroom_telemetry_off(ctx):
            beacon = "off" if self._headroom_beacon_off(ctx) else "on (default)"
            update = "off" if self._headroom_update_check_off(ctx) else "on (default)"
            findings.append(self.finding(
                "P01", "telemetry-on", "high", "privacy",
                "Headroom's upload beacon and/or daily PyPI update check is on. "
                "The beacon is on by default. The update check is a separate switch "
                "and is not stopped by HEADROOM_TELEMETRY=off or HEADROOM_BEACON=off. "
                "Set HEADROOM_BEACON=off and HEADROOM_UPDATE_CHECK=off "
                "(or HEADROOM_OFFLINE=1, which covers both), plus HEADROOM_TELEMETRY=off "
                "and DO_NOT_TRACK=1. Pass --no-telemetry when starting the proxy.",
                "Set HEADROOM_BEACON=off HEADROOM_TELEMETRY=off HEADROOM_UPDATE_CHECK=off "
                "DO_NOT_TRACK=1 HEADROOM_OFFLINE=1, and pass --no-telemetry.",
                "Headroom v0.44.0 beacon.py / update_check.py",
                key="headroom_telemetry",
                value=f"beacon={beacon}; update_check={update}",
            ))
        if self._claude_mem(ctx):
            mem = self._claude_mem_settings(ctx)
            tel = self._mem_key(mem, "telemetry", "telemetryEnabled", "telemetry_enabled", "analytics", "analyticsEnabled")
            # Default is on; only treat as opted out when we can see a disable flag.
            if tel is None or _truthy(tel) or str(tel).lower() in {"on", "enabled"}:
                findings.append(self.finding(
                    "P01", "telemetry-on", "high", "privacy",
                    "claude-mem sends anonymous analytics by default. Turn it off with: `npx claude-mem telemetry disable`.",
                    "Disable telemetry in ~/.claude-mem/settings.json (`npx claude-mem telemetry disable`). The exact key is unverified; doctor treats a missing opt-out as on.",
                    "claude-mem README",
                    file=str(ctx.home / ".claude-mem" / "settings.json"),
                    key="telemetry", value=tel if tel is not None else "unset (default on)",
                ))
        if self._rtk(ctx) and self._rtk_telemetry_on(ctx):
            findings.append(self.finding(
                "P01", "telemetry-on", "high", "privacy",
                "RTK telemetry has been opted in. RTK is off by default; turn it back off if you did not mean to share usage with its maker.",
                "Disable RTK telemetry (leave the default off).",
                "RTK README",
                file=str(ctx.home / ".config" / "rtk" / "config.toml"),
                key="telemetry", value=True,
            ))
        return findings

    def _official_remote(self, url: str) -> bool:
        low = (url or "").lower()
        return any(h in low for h in OFFICIAL_HOSTS)

    def _p02(self, ctx: Context) -> list:
        findings = []
        # Caveman signed in + cloud/routing
        if self._caveman_cli(ctx) and ctx.exists(ctx.home / ".caveman" / "credentials") and self._caveman_cloud_on(ctx):
            findings.append(self.finding(
                "P02", "third-party-cloud", "high", "privacy",
                "Your prompts or code are being sent to Caveman Cloud, not just to Anthropic/OpenAI. "
                "If you didn't mean to do that, turn it off.",
                "Run `caveman off routing && caveman providers cloud off`, and sign out.",
                "Caveman SECURITY.md",
                file=str(ctx.home / ".caveman" / "credentials"),
                key="caveman_cloud", value="signed-in",
            ))
        if self._claude_mem(ctx):
            mem = self._claude_mem_settings(ctx)
            cloud = self._mem_key(mem, "cloudSync", "cloud_sync", "cmemCloud", "cloud", "syncEnabled")
            provider = str(self._mem_key(mem, "provider", "CLAUDE_MEM_PROVIDER") or "").lower()
            observer = self._mem_key(mem, "observer", "observerEnabled", "observer_enabled")
            if cloud not in {None, False, "false", "off", 0, "0"}:
                findings.append(self.finding(
                    "P02", "third-party-cloud", "high", "privacy",
                    "claude-mem cloud sync is on, so chats or memories can be sent to cmem.ai.",
                    "Disable claude-mem cloud sync.",
                    "claude-mem README",
                    file=str(ctx.home / ".claude-mem" / "settings.json"),
                    key="cloudSync", value=cloud,
                ))
            if provider in {"openrouter", "openai-compatible", "openai_compatible"}:
                findings.append(self.finding(
                    "P02", "third-party-cloud", "high", "privacy",
                    f"claude-mem is set to provider {provider}, so prompts go to a third party.",
                    "Switch the provider back to your own Anthropic/OpenAI account, or disable the observer.",
                    "claude-mem README",
                    key="provider", value=provider,
                ))
            if observer is None or _truthy(observer):
                findings.append(self.finding(
                    "P02", "third-party-cloud", "med", "privacy",
                    "The claude-mem observer is on and sends tool-call content to a model provider.",
                    "Disable the observer if you did not mean to send every tool call to a model.",
                    "claude-mem README",
                    key="observer", value=observer if observer is not None else "default on",
                ))
        if self._llm_router(ctx):
            findings.append(self.finding(
                "P02", "third-party-cloud", "high", "privacy",
                "llm-router is installed and routes prompts to other providers.",
                "Uninstall llm-router. It is excluded from the Clean and API-key profiles.",
                "llm-router SECURITY.md, tools.md",
                file=str(ctx.home / ".llm-router"), key="llm-router", value="installed",
            ))
        for a in self._others():
            for b in a.base_urls(ctx):
                if not b.url:
                    continue
                if is_loopback_url(b.url) or "127.0.0.1" in b.url or "localhost" in b.url:
                    local = _local_compression_proxy_label(b.url)
                    if local:
                        findings.append(self.finding(
                            "P02", "third-party-cloud", "info", "privacy",
                            f"{local} is in the request path at {b.url}. "
                            "Prompts pass through that local program before they reach the provider.",
                            "This is expected for the opt-in API-key proxy profile. "
                            "Keep the proxy bound to 127.0.0.1, telemetry off, and do not use it with a subscription login.",
                            "CC-ENV, tools.md (Headroom / Kompressor)",
                            file=b.where, key=b.key, value=b.url,
                        ))
                    continue
                if self._official_remote(b.url):
                    continue
                findings.append(self.finding(
                    "P02", "third-party-cloud", "high", "privacy",
                    f"Your AI base URL points at {b.url}, a third-party gateway that would see every prompt.",
                    "Remove the remote base URL.",
                    "CC-ENV, CX-CFG",
                    file=b.where, key=b.key, value=b.url,
                ))
            for srv in a.list_mcp_servers(ctx):
                if srv.remote_url and not is_loopback_url(srv.remote_url) and "localhost" not in (srv.remote_url or ""):
                    findings.append(self.finding(
                        "P02", "third-party-cloud", "info", "privacy",
                        f"MCP server {srv.name} is remote ({srv.remote_url}) and receives tool arguments.",
                        "Use a local stdio server, or remove the remote MCP if you did not mean to share tool arguments.",
                        "CC-MCP, CX-CFG",
                        file=srv.file, key=srv.name, value=srv.remote_url,
                    ))
        return findings

    def _p03(self, ctx: Context) -> list:
        if not self._claude_mem(ctx):
            return []
        media = ctx.home / ".claude-mem" / "media-v1"
        size = 0
        if media.is_dir():
            try:
                size = sum(p.stat().st_size for p in media.rglob("*") if p.is_file())
            except OSError:
                size = 0
        return [self.finding(
            "P03", "local-plaintext-retention", "info", "privacy",
            "claude-mem keeps local memory"
            + (", including screenshots in media-v1" if size or (media.is_dir()) else "")
            + " in plain text on this computer.",
            "Purge ~/.claude-mem/media-v1/ if you don't need the screenshots, and shrink retention if you want less kept locally.",
            "claude-mem README, CC-DATA",
            file=str(ctx.home / ".claude-mem"), key="media-v1-bytes", value=size,
        )]

    def _p04(self, ctx: Context) -> list:
        findings = []
        for lis in ctx.listeners:
            tool = _tool_for_port(lis.port, lis.process)
            if not tool:
                continue
            sev = "high" if lis.exposed else "info"
            extra = ", open to other devices on your Wi-Fi" if lis.exposed else ""
            findings.append(self.finding(
                "P04", "listening-ports", sev, "privacy",
                f"{tool} is listening on {lis.address}:{lis.port}{extra}.",
                "Bind it to 127.0.0.1, or stop it.",
                "tool READMEs; P04 exposure rule",
                port=lis.port, process=lis.process or tool, key="listen",
                value=f"{lis.address}:{lis.port}",
            ))
        return findings




    _CRED_NAMES = {
        ".credentials.json", "auth.json", "credentials",
        "oauth_creds.json", "gemini-credentials.json", "google_accounts.json",
        "oauth-keys.env",
    }

    def _credential_paths(self, ctx: Context) -> list[Path]:
        paths: list[Path] = []
        seen: set[str] = set()
        for ad in [self, *self._others()]:
            try:
                locs = ad.config_locations(ctx)
            except (AttributeError, TypeError, OSError):
                continue
            for loc in locs:
                if not (getattr(loc, "sensitive", False) or getattr(loc, "kind", "") == "credential"):
                    continue
                path = loc.path
                if path.name not in self._CRED_NAMES:
                    continue
                key = str(path)
                if key in seen:
                    continue
                seen.add(key)
                paths.append(path)
        return paths

    def _p06(self, ctx: Context) -> list:
        """Stat only: mode and permissions of credential files. Never open()."""
        findings = []
        darwin = ctx.os_name().startswith("darwin") or ctx.os_name() in {"macos", "osx"}
        windows = ctx.os_name().startswith("win")
        for path in self._credential_paths(ctx):
            st = ctx.file_stat(path)
            if st is None:
                continue
            mode = stat.S_IMODE(st.st_mode)
            group_r = bool(mode & stat.S_IRGRP)
            world_r = bool(mode & stat.S_IROTH)
            is_claude_creds = path.name == ".credentials.json" and ".claude" in Path(path).parts
            issues = []
            if darwin and is_claude_creds:
                issues.append(
                    "it is present on macOS, which means the Keychain fallback wrote a plaintext login file"
                )
            if not windows and (group_r or world_r):
                who = " and ".join([n for n, flag in (("group", group_r), ("world", world_r)) if flag])
                issues.append(f"it is {who}-readable (mode {mode:04o})")
            if not issues:
                continue
            sev = "high" if world_r and not windows else "med"
            findings.append(self.finding(
                "P06", "credential-file-exposure", sev, "privacy",
                f"{path} exists: " + "; ".join(issues) + ". Mode and permissions were collected with stat only; the file was not opened.",
                "On macOS, follow Anthropic's Keychain recovery steps so the login is not stored as a plaintext file. On any OS, chmod 600 the file (owner read/write only).",
                "credential-locations.md (P06; Claude Keychain fallback is 0600; Linux store is normally ~/.claude/.credentials.json)",
                file=str(path), key="mode", value=f"{mode:04o}",
            ))
        return findings


    def setup_items(self, ctx: Context) -> list[SetupItem]:
        """Telemetry off for third-party tools that are already installed. Plans only."""
        items: list[SetupItem] = []
        if self._caveman_cli(ctx) and not self._caveman_telemetry_off(ctx):
            changes = []
            targets = [p for p, _ in self._caveman_cfgs(ctx)
                       if p.name != "credentials" and p.parent.name != "caveman"]  # ~/.config/caveman path is unverified
            if not targets:
                targets = [ctx.home / ".caveman" / "cloud.json"]
            for path in targets:
                data = dict(_json(path) or {})
                tel = data.get("telemetry")
                data["telemetry"] = {**tel, "enabled": False} if isinstance(tel, dict) else {"enabled": False}
                changes.append({
                    "path": str(path), "kind": "modify" if path.is_file() else "create",
                    "content": json.dumps(data, indent=2) + "\n", "key": "telemetry.enabled",
                    "reason": "telemetry.enabled -> false",
                })
            items.append(SetupItem(
                id="caveman-telemetry-off", adapter_id=self.id,
                title="Turn off Caveman telemetry",
                why="The Caveman CLI sends usage reports (including your IP address) to its maker by default. "
                    "That is a privacy leak, not a credit saver.",
                changes=changes, done="turned off Caveman telemetry",
                notes=["Same effect as `caveman telemetry off`, written straight to its config file "
                       "(no Caveman command is run, so nothing touches the network)."],
            ))
        if self._rtk(ctx) and self._rtk_telemetry_on(ctx):
            path = ctx.home / ".config" / "rtk" / "config.toml"
            existing = path.read_text(encoding="utf-8") if path.is_file() else ""
            items.append(SetupItem(
                id="rtk-telemetry-off", adapter_id=self.id,
                title="Turn RTK telemetry back off",
                why="RTK telemetry is off by default but was opted in here, so usage is shared with its maker.",
                changes=[{
                    "path": str(path), "kind": "modify" if path.is_file() else "create",
                    "content": _toml_telemetry_off(existing), "key": "telemetry",
                    "reason": "telemetry -> false",
                }],
                done="turned off RTK telemetry",
            ))
        return items

    def planned_support_files(self, ctx: Context, tool_ids: list[str] | None = None) -> list[dict]:
        """Plan telemetry-off and wrapper files. Never writes; the installer applies changes.

        Tool-specific paths stay in this adapter. Do not create ~/.config/caveman
        (doctor treats that path as the Caveman CLI, which Clean never installs).
        """
        tool_ids = set(tool_ids or [])
        changes: list[dict] = []

        if not tool_ids or "rtk-oss" in tool_ids:
            path = ctx.home / ".config" / "rtk" / "config.toml"
            existing = path.read_text(encoding="utf-8") if path.is_file() else ""
            content = _toml_telemetry_off(existing)
            if content != existing:
                changes.append({
                    "path": str(path),
                    "kind": "modify" if path.is_file() else "create",
                    "content": content,
                    "reason": "RTK telemetry stays off (never enable the proxy).",
                })

        if not tool_ids or "tokeneconomizer" in tool_ids:
            from ..catalog import TE_CLEAN_CONFIG
            root = _tokeneconomizer_home(ctx)
            path = root / "config.json"
            data = dict(_json(path) or {})
            desired = dict(TE_CLEAN_CONFIG)
            if not path.is_file() or any(data.get(k) != v for k, v in desired.items()):
                if path.is_file():
                    data.update(desired)
                    payload = data
                else:
                    payload = desired
                changes.append({
                    "path": str(path),
                    "kind": "modify" if path.is_file() else "create",
                    "content": json.dumps(payload, indent=2) + "\n",
                    "reason": "TokenEconomizer Clean config: microskills=off, toolprune=propose, loopguard=off "
                              "at $TOKENECONOMIZER_HOME/config.json (default ~/.tokeneconomizer).",
                })

        if not tool_ids or "ccusage" in tool_ids:
            wrapper_path = ctx.home / STATE_DIRNAME / "bin" / "ccusage"
            wrapper = (
                "#!/bin/sh\n"
                f"# {PRODUCT_NAME} ccusage wrapper: pin 20.0.48, always pass --offline,\n"
                f"# and prefer the kit-local npm ci install under $HOME/{STATE_DIRNAME}/ccusage.\n"
                f'ROOT="${{{ENV_PREFIX}_CCUSAGE:-$HOME/{STATE_DIRNAME}/ccusage}}"\n'
                "if [ -x \"$ROOT/node_modules/.bin/ccusage\" ]; then\n"
                '  exec "$ROOT/node_modules/.bin/ccusage" --offline "$@"\n'
                "fi\n"
                "# Fallback stays pinned to 20.0.48; never @latest.\n"
                'exec npx --yes ccusage@20.0.48 --offline "$@"\n'
            )
            if not wrapper_path.is_file() or wrapper_path.read_text(encoding="utf-8") != wrapper:
                changes.append({
                    "path": str(wrapper_path),
                    "kind": "modify" if wrapper_path.is_file() else "create",
                    "content": wrapper,
                    "mode": 0o755,
                    "reason": "Local ccusage wrapper always passes --offline (in addition to ccusage.json).",
                })
            cfg_path = ctx.home / ".claude" / "ccusage.json"
            from ..catalog import CCUSAGE_OFFLINE_CONFIG
            cfg = json.dumps(CCUSAGE_OFFLINE_CONFIG, indent=2) + "\n"
            if not cfg_path.is_file() or cfg_path.read_text(encoding="utf-8") != cfg:
                changes.append({
                    "path": str(cfg_path),
                    "kind": "modify" if cfg_path.is_file() else "create",
                    "content": cfg,
                    "reason": "ccusage.json defaults.offline=true; still pass --offline too.",
                })

        # Shared hook scripts referenced by Claude/Codex hook plans.
        hooks_dir = ctx.home / STATE_DIRNAME / "hooks"
        session = hooks_dir / "session-start.sh"
        session_body = (
            "#!/bin/sh\n"
            f"# {PRODUCT_NAME} session-start note. This script does not use the network.\n"
            f"echo \"{PRODUCT_NAME}: Clean profile is active. Telemetry stays off. No kit network calls.\"\n"
        )
        if not session.is_file() or session.read_text(encoding="utf-8") != session_body:
            changes.append({
                "path": str(session),
                "kind": "modify" if session.is_file() else "create",
                "content": session_body,
                "mode": 0o755,
                "reason": "Local no-network session-start note for SessionStart hooks.",
            })
        gate = hooks_dir / "read-gate.sh"
        gate_body = (
            "#!/bin/sh\n"
            f"# {PRODUCT_NAME} Codex read-gate placeholder.\n"
            "# Claude uses native permissions.deny; Codex has no equivalent, so this hook\n"
            "# is reserved for a future local filter. It does not use the network.\n"
            "exit 0\n"
        )
        if not gate.is_file() or gate.read_text(encoding="utf-8") != gate_body:
            changes.append({
                "path": str(gate),
                "kind": "modify" if gate.is_file() else "create",
                "content": gate_body,
                "mode": 0o755,
                "reason": "Local no-network Codex read-gate script (native deny is Claude-only).",
            })
        return changes


def _tokeneconomizer_home(ctx: Context) -> Path:
    val, _ = ctx.env_or_rc("TOKENECONOMIZER_HOME")
    if val:
        return Path(val).expanduser()
    return ctx.home / ".tokeneconomizer"


def _toml_telemetry_off(text: str) -> str:
    """Set a top-level telemetry = false without inventing a proxy entry."""
    if re.search(r"(?m)^\s*telemetry\s*=", text):
        return re.sub(r"(?m)^\s*telemetry\s*=\s*.*$", "telemetry = false", text)
    body = text if text.endswith("\n") or text == "" else text + "\n"
    if body and not body.endswith("\n"):
        body += "\n"
    prefix = "" if not body or body.endswith("\n") else "\n"
    return body + prefix + (
        f"# {PRODUCT_NAME} Clean profile: leave telemetry off; decline the proxy.\n"
        "telemetry = false\n"
    )


def implemented_check_ids() -> set[str]:

    """All doctor check IDs this tree implements, including P00 (enforced as a test)."""
    from . import REGISTRY
    ids: set[str] = {"P00"}
    for ad in REGISTRY:
        ids.update(getattr(ad, "implemented_checks", ()))
    return ids
