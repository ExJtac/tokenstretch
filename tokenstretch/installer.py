"""Dry-run-first installer, license checks, 8787 conflict, backups, and undo.

File writes happen only on `install --apply`. Fetch commands run only through a
runner; tests inject FakeRunner so no install process starts and no network is used.
The default profile is Clean. The opt-in API-key proxy profile is selected only
with `--profile api-proxy --proxy headroom|kompressor`. Headroom is installed
with `uv pip install --require-hashes` into a dedicated venv; macOS below 14.0
is refused. allow_unpinned_for_tests is a test-only hook and is not on the CLI.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .adapters import REGISTRY
from .adapters.base import HookPlan
from .api_proxy import (
    ApiProxyError,
    existing_wildcard_bind_reasons,
    manual_proxy_steps,
    merge_file_changes,
    pins_block_apply,
    plan_proxy_file_changes,
    subscription_auth_reasons,
    validate_profile_flags,
)
from .catalog import (
    ALLOWED_LICENSES,
    CAVEMAN_COMMIT,
    HEADROOM_MACOS_UNTESTED_NOTE,
    HEADROOM_MCP_PORT,
    CCUSAGE_OFFLINE_CONFIG,
    CLEAN_HOOK_PLAN_ITEMS,
    PinMismatchError,
    RTK_COMMIT,
    RTK_INSTALL_SH_SHA256,
    RTK_UNSUPPORTED_PLATFORMS,
    TE_CLEAN_CONFIG,
    TE_COMMIT,
    api_proxy_tool,
    ccusage_pin_dir,
    headroom_macos_refusal,
    headroom_packaged_lock,
    kompressor_asset_for_host,
    verify_headroom_lock,
    verify_kompressor_asset,
    clean_tools,
    read_git_head,
    this_host_is_unsupported_rtk,
    verify_checksums_txt,
    verify_commit,
    verify_rtk_asset,
    verify_rtk_install_sh,
)
from .context import Context
from .identity import PRODUCT_NAME, STATE_DIRNAME, VERSION
from .runner import FakeRunner, default_runner

MANIFEST_DIRNAME = STATE_DIRNAME
ALLOWED_WRITE_ROOTS = ("home", "root")  # writes must stay under ctx.home or ctx.root


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str | None:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError:
        return None



@dataclass
class InstallArtifacts:
    """Optional local files used to verify pins (tests, or a staging dir after a real fetch)."""
    rtk_install_sh: Path | None = None
    rtk_checksums_txt: Path | None = None
    rtk_assets: dict[str, Path] = field(default_factory=dict)
    caveman_repo: Path | None = None
    caveman_head: str | None = None
    te_repo: Path | None = None
    te_head: str | None = None
    kompressor_binary: Path | None = None
    kompressor_asset_name: str | None = None


def verify_install_artifacts(artifacts: InstallArtifacts) -> None:
    """Refuse on a hash or commit mismatch. Never fetches."""
    if artifacts.rtk_install_sh:
        verify_rtk_install_sh(Path(artifacts.rtk_install_sh).read_bytes())
    if artifacts.rtk_checksums_txt:
        verify_checksums_txt(Path(artifacts.rtk_checksums_txt).read_bytes())
    for name, path in (artifacts.rtk_assets or {}).items():
        verify_rtk_asset(name, Path(path).read_bytes())
    if artifacts.caveman_repo or artifacts.caveman_head is not None:
        head = artifacts.caveman_head if artifacts.caveman_head is not None else read_git_head(Path(artifacts.caveman_repo))
        verify_commit(head, CAVEMAN_COMMIT, what="Caveman")
    if artifacts.te_repo or artifacts.te_head is not None:
        head = artifacts.te_head if artifacts.te_head is not None else read_git_head(Path(artifacts.te_repo))
        verify_commit(head, TE_COMMIT, what="TokenEconomizer")
    if artifacts.kompressor_binary:
        name = artifacts.kompressor_asset_name or Path(artifacts.kompressor_binary).name
        verify_kompressor_asset(name, Path(artifacts.kompressor_binary).read_bytes())


def caveman_skill_destinations(ctx: Context) -> list[Path]:
    from .adapters import REGISTRY
    out = []
    for ad in REGISTRY:
        fn = getattr(ad, "caveman_skill_dir", None)
        if callable(fn):
            dest = fn(ctx)
            if dest:
                out.append(Path(dest))
    return out


def plan_caveman_skill_copy(ctx: Context, repo: Path) -> list[dict]:
    """Copy only skills/caveman (never skills/caveman-setup or the rest of the plugin)."""
    src = Path(repo) / "skills" / "caveman"
    if not src.is_dir():
        raise PinMismatchError(f"Caveman repo at {repo} has no skills/caveman directory.")
    changes = []
    for dest_root in caveman_skill_destinations(ctx):
        for path in src.rglob("*"):
            if not path.is_file():
                continue
            # Defence: never copy a caveman-setup path if the tree is mixed.
            if "caveman-setup" in path.parts:
                continue
            rel = path.relative_to(src)
            dest = dest_root / rel
            changes.append({
                "path": str(dest),
                "kind": "modify" if dest.is_file() else "create",
                "content": path.read_text(encoding="utf-8"),
                "reason": "Caveman skill only (skills/caveman), after verifying the pinned commit. Not caveman-setup.",
            })
    return changes


def plan_ccusage_pin_files(ctx: Context) -> list[dict]:
    """Stage the pinned package.json / lockfile under the kit state dir (default ~/.tokenstretch)/ccusage/."""
    src = ccusage_pin_dir()
    dest_root = kit_root(ctx) / "ccusage"
    changes = []
    for name in ("package.json", "package-lock.json"):
        sp = src / name
        if not sp.is_file():
            continue
        dp = dest_root / name
        content = sp.read_text(encoding="utf-8")
        if dp.is_file() and dp.read_text(encoding="utf-8") == content:
            continue
        changes.append({
            "path": str(dp),
            "kind": "modify" if dp.is_file() else "create",
            "content": content,
            "reason": "Pinned ccusage@20.0.48 lockfile.",
        })
    return changes


def platform_notes() -> list[str]:
    notes = [
        f"Unsupported RTK oss platforms: {label} ({reason})."
        for label, reason in RTK_UNSUPPORTED_PLATFORMS
    ]
    host = this_host_is_unsupported_rtk()
    if host:
        notes.append(f"This host matches an unsupported RTK platform ({host}); the RTK oss fetch will be skipped.")
    return notes


def default_snapshot_dir() -> Path:
    return Path(__file__).resolve().parent / "license_snapshots"


def load_snapshots(directory: Path | None = None) -> dict[str, dict]:
    directory = Path(directory or default_snapshot_dir())
    out: dict[str, dict] = {}
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            out[path.stem] = {"id": path.stem, "error": f"unreadable snapshot: {path}", "allowed": False}
            continue
        data.setdefault("id", path.stem)
        out[data["id"]] = data
    return out


@dataclass
class LicenseVerdict:
    tool_id: str
    ok: bool
    license: str = ""
    version: str = ""
    message: str = ""


def check_licenses(tools=None, snapshots: dict[str, dict] | None = None) -> list[LicenseVerdict]:
    tools = tools if tools is not None else clean_tools()
    snapshots = snapshots if snapshots is not None else load_snapshots()
    verdicts = []
    for tool in tools:
        snap = snapshots.get(tool.license_id) or snapshots.get(tool.id)
        if not snap:
            verdicts.append(LicenseVerdict(tool.id, False, "", tool.version,
                                           f"No license snapshot for {tool.id} at the pinned version {tool.version}."))
            continue
        if snap.get("error"):
            verdicts.append(LicenseVerdict(tool.id, False, "", tool.version, str(snap["error"])))
            continue
        lic = str(snap.get("license") or "")
        allowed_flag = snap.get("allowed", lic in ALLOWED_LICENSES)
        version_ok = not snap.get("version") or str(snap.get("version")) == str(tool.version)
        ok = bool(allowed_flag) and lic in ALLOWED_LICENSES and version_ok
        if not ok:
            if lic and lic not in ALLOWED_LICENSES:
                msg = f"{tool.name} snapshot license {lic} is not installable (allowed: {', '.join(sorted(ALLOWED_LICENSES))})."
            elif not version_ok:
                msg = f"{tool.name} snapshot is for {snap.get('version')}, but the catalog pins {tool.version}."
            else:
                msg = f"{tool.name} license snapshot is marked not allowed."
        else:
            msg = f"{tool.name} {tool.version} is {lic} (snapshot)."
        verdicts.append(LicenseVerdict(tool.id, ok, lic, tool.version, msg))
    return verdicts


def detect_8787_conflict(ctx: Context) -> dict[str, Any] | None:
    """Caveman's proxy and Headroom both default to 127.0.0.1:8787."""
    listeners = ctx.listener_on(8787)
    procs = ctx.process_matches(r"\b(headroom|caveman)\b")
    names = sorted({(l.process or "unknown") for l in listeners} | set())
    # Also count distinct tool names on the port or in the process list.
    toolish = []
    blob = " ".join(procs + [l.process for l in listeners]).lower()
    if "headroom" in blob:
        toolish.append("Headroom")
    if "caveman" in blob:
        toolish.append("Caveman")
    if not listeners and len(toolish) < 2:
        return None
    # A single occupant of 8787 is still a conflict with the documented default
    # (the other tool would fail to bind). Two named tools is the loud case.
    occupants = toolish or names or ["a process"]
    return {
        "port": 8787,
        "occupants": occupants,
        "listeners": [f"{l.address}:{l.port} {l.process}".strip() for l in listeners],
        "message": (
            "Port 8787 is already in use"
            + (f" ({', '.join(occupants)})" if occupants else "")
            + ". Caveman's proxy and Headroom both default to 127.0.0.1:8787. "
            "The Clean profile does not install either proxy; resolve the conflict before --apply."
        ),
    }


def detect_8788_conflict(ctx: Context) -> dict[str, Any] | None:
    """Headroom MCP HTTP transport defaults to 127.0.0.1:8788."""
    listeners = ctx.listener_on(HEADROOM_MCP_PORT)
    if not listeners:
        return None
    names = sorted({(l.process or "unknown") for l in listeners})
    return {
        "port": HEADROOM_MCP_PORT,
        "occupants": names,
        "listeners": [f"{l.address}:{l.port} {l.process}".strip() for l in listeners],
        "message": (
            f"Port {HEADROOM_MCP_PORT} is already in use"
            + (f" ({', '.join(names)})" if names else "")
            + f". Headroom MCP HTTP transport defaults to 127.0.0.1:{HEADROOM_MCP_PORT}. "
            "Resolve the conflict before --apply."
        ),
    }


def detect_proxy_port_conflicts(ctx: Context, proxy_id: str | None = None) -> dict[str, Any] | None:
    """8787 always (Caveman / Headroom). 8788 when installing Headroom (MCP HTTP)."""
    conflict = detect_8787_conflict(ctx)
    mcp = detect_8788_conflict(ctx) if proxy_id == "headroom" else None
    if conflict and mcp:
        merged = dict(conflict)
        merged["mcp_8788"] = mcp
        merged["message"] = conflict["message"] + " " + mcp["message"]
        return merged
    return conflict or mcp


def kit_root(ctx: Context) -> Path:
    return ctx.home / MANIFEST_DIRNAME


def _rel_backup_path(path: Path, home: Path, root: Path) -> str:
    for base, prefix in ((home, "home"), (root, "root")):
        try:
            return str(Path(prefix) / path.resolve().relative_to(base.resolve()))
        except (ValueError, OSError):
            continue
    # Fallback: flatten with a safe name
    return "other/" + "__".join(path.parts[-6:])


def _writable(path: Path, ctx: Context) -> bool:
    try:
        resolved = path.expanduser().resolve()
        home = ctx.home.resolve()
        root = ctx.root.resolve()
    except OSError:
        return False
    return resolved == home or resolved == root or home in resolved.parents or root in resolved.parents


def plan_file_changes(ctx: Context, plan: HookPlan | None = None, tool_ids: set[str] | None = None) -> list[dict[str, str]]:
    plan = plan or HookPlan(items=list(CLEAN_HOOK_PLAN_ITEMS))
    wanted = [t.id for t in clean_tools() if not tool_ids or t.id in tool_ids]
    changes: list[dict[str, str]] = []
    seen: set[str] = set()
    for ad in REGISTRY:
        result = ad.install_hooks(ctx, plan, dry_run=True) or {}
        for ch in result.get("changes") or []:
            if not isinstance(ch, dict):
                ch = {"path": getattr(ch, "path", ""), "kind": getattr(ch, "kind", "modify"),
                      "content": getattr(ch, "content", ""), "reason": getattr(ch, "reason", "")}
            key = str(ch.get("path") or "")
            if not key or key in seen:
                continue
            seen.add(key)
            changes.append({
                "path": key,
                "kind": ch.get("kind") or "modify",
                "content": ch.get("content") or "",
                "reason": ch.get("reason") or "",
            })
        extra = getattr(ad, "planned_support_files", None)
        if callable(extra):
            for ch in extra(ctx, wanted) or []:
                key = str(ch.get("path") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                changes.append({
                    "path": key,
                    "kind": ch.get("kind") or "modify",
                    "content": ch.get("content") or "",
                    "reason": ch.get("reason") or "",
                })
    for ch in (plan_ccusage_pin_files(ctx) if "ccusage" in wanted else []):
        key = str(ch.get("path") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        changes.append(ch)
    return changes


def _now_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")


def apply_changes(ctx: Context, changes: list[dict[str, str]], stamp: str | None = None, *, profile: str = "clean", extra_manifest: dict | None = None) -> dict[str, Any]:
    """Write planned changes, backing up every touched existing file first."""
    stamp = stamp or _now_stamp()
    root = kit_root(ctx)
    backup_dir = root / "backups" / stamp
    manifest_dir = root / "manifests"
    backup_dir.mkdir(parents=True, exist_ok=True)
    manifest_dir.mkdir(parents=True, exist_ok=True)
    records = []
    dirs_created: list[str] = []

    for ch in changes:
        path = Path(ch["path"])
        if not _writable(path, ctx):
            raise PermissionError(f"refusing to write outside the home/project tree: {path}")
        existed = path.is_file()
        before = path.read_bytes() if existed else None
        before_hash = sha256_bytes(before) if before is not None else None
        backup_rel = None
        if existed:
            backup_rel = _rel_backup_path(path, ctx.home, ctx.root)
            bpath = backup_dir / backup_rel
            bpath.parent.mkdir(parents=True, exist_ok=True)
            bpath.write_bytes(before or b"")
        # mkdir parents; record new directories
        parent = path.parent
        to_make = []
        cursor = parent
        while not cursor.exists() and cursor not in {cursor.parent, ctx.home, ctx.root}:
            to_make.append(cursor)
            cursor = cursor.parent
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
            dirs_created.extend(str(d) for d in reversed(to_make) if d not in [Path(x) for x in dirs_created])
            if str(parent) not in dirs_created:
                dirs_created.append(str(parent))
        content = ch.get("content") or ""
        data = content.encode("utf-8") if isinstance(content, str) else content
        path.write_bytes(data)
        mode = ch.get("mode")
        if mode is not None:
            path.chmod(int(mode, 8) if isinstance(mode, str) else int(mode))
        after_hash = sha256_bytes(path.read_bytes())
        kind = "created" if not existed else "modified"
        records.append({
            "path": str(path),
            "action": kind,
            "sha256_before": before_hash,
            "sha256_after": after_hash,
            "backup": backup_rel,
            "reason": ch.get("reason") or "",
        })

    manifest = {
        "product": PRODUCT_NAME,
        "version": VERSION,
        "profile": profile,
        "created_at": stamp,
        "backup_dir": str(backup_dir),
        "files": records,
        "dirs_created": dirs_created,
    }
    if extra_manifest:
        manifest.update(extra_manifest)
    manifest_path = manifest_dir / f"{stamp}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return {"manifest_path": str(manifest_path), "backup_dir": str(backup_dir), "manifest": manifest}


class DriftError(RuntimeError):
    """A file changed after the install; undo refused without --force."""

    def __init__(self, path: str, expected: str | None, actual: str | None):
        self.path = path
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"refusing to undo {path}: it changed since the install "
            f"(expected sha256 {expected}, found {actual}). Re-run with --force to override."
        )


def undo_manifest(manifest_path: Path, *, force: bool = False) -> dict[str, Any]:
    data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    backup_dir = Path(data["backup_dir"])
    restored: list[str] = []
    deleted: list[str] = []
    skipped: list[str] = []
    refused: list[str] = []

    def current_hash(path: Path) -> str | None:
        return sha256_file(path)

    # Process files; refuse the whole undo if any drifted and not --force
    if not force:
        for rec in data.get("files") or []:
            path = Path(rec["path"])
            actual = current_hash(path)
            expected = rec.get("sha256_after")
            before = rec.get("sha256_before")
            action = rec.get("action")
            # Already undone: created file gone, or modified file back to before.
            if action == "created" and actual is None:
                continue
            if action == "modified" and actual == before:
                continue
            if actual == expected:
                continue
            refused.append(rec["path"])
        if refused:
            rec = next(r for r in data["files"] if r["path"] == refused[0])
            raise DriftError(refused[0], rec.get("sha256_after"), current_hash(Path(refused[0])))

    for rec in data.get("files") or []:
        path = Path(rec["path"])
        actual = current_hash(path)
        expected = rec.get("sha256_after")
        before = rec.get("sha256_before")
        action = rec.get("action")
        if action == "created":
            if actual is None:
                skipped.append(str(path))
                continue
            if not force and actual != expected:
                raise DriftError(str(path), expected, actual)
            try:
                path.unlink()
                deleted.append(str(path))
            except OSError as exc:
                raise RuntimeError(f"could not delete {path}: {exc}") from exc
        else:  # modified
            if actual == before:
                skipped.append(str(path))
                continue
            if not force and actual != expected:
                raise DriftError(str(path), expected, actual)
            backup_rel = rec.get("backup")
            if not backup_rel:
                raise RuntimeError(f"no backup recorded for {path}")
            bpath = backup_dir / backup_rel
            try:
                path.write_bytes(bpath.read_bytes())
            except OSError as exc:
                raise RuntimeError(f"could not restore {path} from {bpath}: {exc}") from exc
            restored.append(str(path))

    # Remove empty directories we created (deepest first)
    for d in reversed(data.get("dirs_created") or []):
        p = Path(d)
        try:
            if p.is_dir() and not any(p.iterdir()):
                p.rmdir()
        except OSError:
            pass

    return {"restored": restored, "deleted": deleted, "skipped": skipped, "manifest": str(manifest_path)}


def latest_manifest(ctx: Context) -> Path | None:
    d = kit_root(ctx) / "manifests"
    if not d.is_dir():
        return None
    files = sorted(d.glob("*.json"))
    return files[-1] if files else None


@dataclass
class InstallResult:
    dry_run: bool
    ok: bool
    exit_code: int
    profile: str = "clean"
    proxy: str | None = None
    licenses: list[LicenseVerdict] = field(default_factory=list)
    conflict_8787: dict | None = None
    fetch_steps: list[dict] = field(default_factory=list)
    fetch_results: list[dict] = field(default_factory=list)
    file_changes: list[dict] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    manifest_path: str = ""
    backup_dir: str = ""
    messages: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    unsupported_platforms: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        d = asdict(self)
        d["licenses"] = [asdict(v) for v in self.licenses]
        return d


def install(
    ctx: Context,
    *,
    apply: bool = False,
    runner=None,
    snapshot_dir: Path | None = None,
    force: bool = False,
    artifacts: InstallArtifacts | None = None,
    profile: str = "clean",
    proxy=None,
    bind: str = "127.0.0.1",
    allow_unpinned_for_tests: bool = False,
    tool_ids: set[str] | None = None,
    manifest_stamp: str | None = None,
    extra_manifest: dict | None = None,
) -> InstallResult:
    """Plan or apply an install.

    allow_unpinned_for_tests is a test-only hook and is not exposed on the CLI.
    Headroom apply uses the packaged hashed lock; it is no longer a pin-gap bypass.
    """
    messages: list[str] = []
    notices = platform_notes()
    selected_proxy = None
    try:
        profile, selected_proxy = validate_profile_flags(profile=profile, proxy=proxy, bind=bind)
    except ApiProxyError as exc:
        return InstallResult(
            dry_run=not apply,
            ok=False,
            exit_code=exc.exit_code,
            profile=(profile or "clean"),
            proxy=None,
            messages=[exc.message],
            notices=notices,
        )

    if profile == "api-proxy" and selected_proxy == "headroom":
        mac_reason = headroom_macos_refusal(ctx.os_name(), ctx.os_version)
        if mac_reason:
            return InstallResult(
                dry_run=not apply,
                ok=False,
                exit_code=1,
                profile=profile,
                proxy=selected_proxy,
                messages=[mac_reason],
                notices=[HEADROOM_MACOS_UNTESTED_NOTE],
            )

    tools = [t for t in clean_tools() if not tool_ids or t.id in tool_ids]
    if profile == "api-proxy" and selected_proxy:
        tool = api_proxy_tool(selected_proxy, ctx.home)
        if tool:
            tools = clean_tools() + [tool]
        for step in manual_proxy_steps(selected_proxy, bind):
            notices.append(step)
        notices.append(
            "This profile is API-key only. Subscription/OAuth logins are refused. "
            "The proxy is pointed at through adapter-owned settings, never shell rc."
        )
        if selected_proxy == "headroom" and ctx.os_name().startswith("darwin"):
            if HEADROOM_MACOS_UNTESTED_NOTE not in notices:
                notices.append(HEADROOM_MACOS_UNTESTED_NOTE)

    verdicts = check_licenses(tools, load_snapshots(snapshot_dir))
    conflict = detect_proxy_port_conflicts(ctx, selected_proxy)
    if conflict and profile == "api-proxy":
        occupants = ", ".join(conflict.get("occupants") or []) or "a process"
        if selected_proxy == "headroom":
            note = (
                "The api-proxy profile with --proxy headroom would also use 127.0.0.1:8787; "
                "resolve the conflict before --apply."
            )
        else:
            note = (
                "The api-proxy profile still runs this check (Caveman's proxy and Headroom "
                "share 8787) even when the selected proxy is Kompressor on 8817."
            )
        conflict = dict(conflict)
        conflict["message"] = (
            f"Port 8787 is already in use ({occupants}). "
            f"Caveman's proxy and Headroom both default to 127.0.0.1:8787. {note}"
        )
    changes = plan_file_changes(ctx, tool_ids=tool_ids)
    if profile == "api-proxy" and selected_proxy:
        proxy_changes = plan_proxy_file_changes(ctx, selected_proxy, bind)
        changes = merge_file_changes(changes, proxy_changes)

    for dest in caveman_skill_destinations(ctx):
        notices.append(
            f"Caveman skill destination ({Path(dest)}): copied only after a verified "
            f"HEAD == {CAVEMAN_COMMIT}; never caveman-setup or the full plugin."
        )
    ok_licenses = all(v.ok for v in verdicts)

    result = InstallResult(
        dry_run=not apply,
        ok=ok_licenses and not (conflict and apply and not force),
        exit_code=0 if ok_licenses and not conflict else (1 if not ok_licenses else 2),
        profile=profile,
        proxy=selected_proxy,
        licenses=verdicts,
        conflict_8787=conflict,
        fetch_steps=[
            {"tool": t.id, "version": t.version, "description": s.description, "argv": s.argv,
             "env": s.env, "network": s.network, "telemetry": t.telemetry, "cwd": s.cwd,
             "commit": t.commit}
            for t in tools for s in t.fetch
        ],
        file_changes=[{k: v for k, v in ch.items() if k != "content"} | {"bytes": len(ch.get("content") or "")}
                      for ch in changes],
        messages=messages,
        notices=notices,
        unsupported_platforms=[label for label, _ in RTK_UNSUPPORTED_PLATFORMS],
    )

    if profile == "api-proxy":
        auth_reasons = subscription_auth_reasons(ctx)
        if auth_reasons:
            messages.extend(auth_reasons)
            messages.append("Refusing the API-key proxy profile. No files were written.")
            result.ok = False
            result.exit_code = 2
            return result
        bind_reasons = existing_wildcard_bind_reasons(ctx, [ch.get("content") or "" for ch in changes])
        if bind_reasons:
            messages.extend(bind_reasons)
            messages.append("Refusing a non-loopback bind. No files were written.")
            result.ok = False
            result.exit_code = 2
            return result
        pin_block = pins_block_apply(selected_proxy, allow_unpinned_for_tests=allow_unpinned_for_tests)
        if pin_block:
            messages.append(pin_block)
            messages.append(
                "Dry-run can show the plan; --apply writes nothing for this proxy until the block is lifted."
            )
            result.ok = False
            result.exit_code = 1
            if apply:
                messages.append("Refusing --apply.")
            # Fall through so licenses, 8787/8788, and planned files are still reported.

    if not ok_licenses:
        messages.append("License check failed. No files were written.")
        result.ok = False
        result.exit_code = 1
        return result

    if artifacts is not None:
        try:
            verify_install_artifacts(artifacts)
            if artifacts.caveman_repo:
                extra = plan_caveman_skill_copy(ctx, Path(artifacts.caveman_repo))
                changes.extend(extra)
                result.file_changes = [
                    {k: v for k, v in ch.items() if k != "content"} | {"bytes": len(ch.get("content") or "")}
                    for ch in changes
                ]
        except PinMismatchError as exc:
            messages.append(str(exc))
            messages.append("Pin verification failed. No files were written and no fetch ran.")
            result.ok = False
            result.exit_code = 1
            result.notices = notices
            return result

    if conflict:
        messages.append(conflict["message"])
        if apply and not force:
            messages.append("Refusing --apply because of the port 8787 conflict. Resolve it or pass --force.")
            result.ok = False
            result.exit_code = 2
            return result

    pin_blocked = bool(profile == "api-proxy" and pins_block_apply(selected_proxy, allow_unpinned_for_tests=allow_unpinned_for_tests))
    if not apply or pin_blocked:
        if not apply:
            messages.append("Dry run: no files were written and no install commands were executed.")
            if not pin_blocked:
                messages.append("Re-run with --apply to apply these changes.")
        result.ok = (not conflict) and (not pin_blocked) and ok_licenses
        if conflict:
            result.exit_code = 2
        elif pin_blocked:
            result.exit_code = 1
        return result

    # --apply: run fetch steps through the runner (fake in tests), then write files.
    runner = runner if runner is not None else default_runner()
    fetch_results = []
    skip_rtk = bool(this_host_is_unsupported_rtk())
    for t in tools:
        if t.id == "rtk-oss" and skip_rtk:
            messages.append("Skipping RTK fetch on this unsupported platform.")
            continue
        # Never fetch an unpinned proxy in production; the pin gate above already
        # returned. The test-only bypass may record argv through FakeRunner.
        for step in t.fetch:
            cwd = None
            if step.cwd == "ccusage":
                cwd = str(kit_root(ctx) / "ccusage")
                Path(cwd).mkdir(parents=True, exist_ok=True)
            elif t.id == "kompressor":
                cwd = str(kit_root(ctx) / "kompressor")
                Path(cwd).mkdir(parents=True, exist_ok=True)
            if t.id == "headroom":
                try:
                    verify_headroom_lock(headroom_packaged_lock())
                except PinMismatchError as exc:
                    messages.append(str(exc))
                    messages.append("Headroom lock failed verification. No further fetch ran and no files were written.")
                    result.fetch_results = fetch_results
                    result.ok = False
                    result.exit_code = 1
                    return result
            ran = runner.run(step.argv, step.env, network=step.network, cwd=cwd)
            fetch_results.append({
                "tool": t.id,
                "argv": ran.argv,
                "network": ran.network,
                "exit_code": ran.exit_code,
                "skipped": ran.skipped,
                "stdout": ran.stdout,
                "cwd": cwd,
            })
            if ran.exit_code != 0:
                messages.append(f"Fetch step for {t.id} failed (exit {ran.exit_code}). Stopping before file writes.")
                result.fetch_results = fetch_results
                result.ok = False
                result.exit_code = 1
                return result
            if t.id == "kompressor" and cwd and not ran.skipped:
                asset = kompressor_asset_for_host()
                if asset:
                    dest = Path(cwd) / asset[0]
                    if dest.is_file():
                        try:
                            verify_kompressor_asset(asset[0], dest.read_bytes())
                        except PinMismatchError as exc:
                            messages.append(str(exc))
                            messages.append("Kompressor sha256 did not match the pin. No files were written.")
                            result.fetch_results = fetch_results
                            result.ok = False
                            result.exit_code = 1
                            return result
    result.fetch_results = fetch_results

    extra = dict(extra_manifest or {})
    if selected_proxy:
        extra.update({"proxy": selected_proxy, "bind": "127.0.0.1"})
    applied = apply_changes(
        ctx,
        changes,
        stamp=manifest_stamp,
        profile=profile,
        extra_manifest=extra or None,
    )
    result.manifest_path = applied["manifest_path"]
    result.backup_dir = applied["backup_dir"]
    result.written = [r["path"] for r in applied["manifest"]["files"]]
    messages.append(f"Wrote {len(result.written)} file(s). Manifest: {result.manifest_path}")
    result.ok = True
    result.exit_code = 2 if conflict and force else 0
    return result


def format_install_report(result: InstallResult) -> str:
    lines = []
    title = "DRY RUN" if result.dry_run else "APPLY"
    lines.append(f"{PRODUCT_NAME} install ({title}) — profile: {result.profile}")
    if result.profile == "api-proxy":
        lines.append(f"API-key proxy: {result.proxy or '(none selected)'}")
        if result.proxy:
            t = api_proxy_tool(result.proxy)
            if t:
                lines.append(f"  - {t.name} {t.version} [{t.license_id}]")
                lines.append(f"    telemetry: {t.telemetry}")
                if t.checksum_note:
                    lines.append(f"    pin: {t.checksum_note}")
        block = pins_block_apply(result.proxy)
        if block:
            lines.append(f"Apply block: {block}")
    lines.append("Pinned Clean tools:")
    for t in clean_tools():
        pin = t.version
        lines.append(f"  - {t.name} {pin} [{t.license_id}]")
        lines.append(f"    telemetry: {t.telemetry}")
        if t.commit:
            lines.append(f"    commit: {t.commit}")
        if t.checksum:
            lines.append(f"    checksum: {t.checksum}")
        if t.checksum_note:
            lines.append(f"    pin: {t.checksum_note}")
    lines.append("Unsupported RTK oss platforms:")
    for label, reason in RTK_UNSUPPORTED_PLATFORMS:
        lines.append(f"  - {label}: {reason}")
    lines.append("License checks:")
    for v in result.licenses:
        flag = "ok" if v.ok else "FAIL"
        lines.append(f"  - {flag}: {v.message}")
    if result.conflict_8787:
        lines.append("Port conflict:")
        lines.append(f"  - {result.conflict_8787['message']}")
    lines.append("Fetch steps (executed only on --apply, never in tests):")
    if not result.fetch_steps:
        lines.append("  - none")
    for s in result.fetch_steps:
        lines.append(f"  - {s['tool']} {s['version']}: {s['description']}")
        lines.append(f"    argv: {' '.join(s['argv'])}")
    lines.append("Planned file changes:")
    for ch in result.file_changes:
        lines.append(f"  - {ch['kind']} {ch['path']} ({ch.get('bytes', 0)} bytes) — {ch.get('reason', '')}")
    if not result.file_changes:
        lines.append("  - none")
    for m in result.messages:
        lines.append(m)
    if result.notices:
        lines.append("Notices:")
        for n in result.notices:
            lines.append(f"  - {n}")
    if result.manifest_path:
        lines.append(f"backup: {result.backup_dir}")
    return "\n".join(lines) + "\n"
