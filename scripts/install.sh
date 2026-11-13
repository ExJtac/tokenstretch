#!/bin/sh
# tokenstretch installer for macOS and Linux.
#
# What it does, in order (run with --dry-run to see every step without touching anything):
#   1. Finds Python 3.11 or newer.
#   2. Fetches the one pinned release below (git tag checked against the pinned commit, or a
#      tarball checked against its pinned SHA-256). That fetch is the only network use.
#   3. Copies it into its own folder under your data dir and creates a private venv there.
#      No pip, no PyPI: tokenstretch has no dependencies, so nothing else is downloaded.
#   4. Puts a `tokenstretch` launcher in ~/.local/bin. Shell startup files are edited only
#      with --add-to-path (backed up first).
# It never runs `tokenstretch setup` or `tokenstretch install`, never installs other tools,
# and sends nothing anywhere: no telemetry, no analytics, no install ping.
# `--uninstall` removes exactly what this script created (its folder, its launcher, its PATH line).
set -eu

# ==== PINNED RELEASE ==========================================================================
# The maintainer sets these at release time, in a commit after the tag (a commit can't name its
# own hash). Nothing else in this file changes per release. Empty means "no release pinned yet".
TS_PIN_TAG="v0.7.0"            # release tag, e.g. v0.1.0
TS_PIN_COMMIT="1cc57401a74e8c599111bf5406bdc604d649ca9a"         # full 40-character commit the tag must point at
TS_PIN_TARBALL_SHA256="af8de734ad7516e3660e940523846f2c6512dd21679a10c8e56cd071a9d29750" # SHA-256 of $TS_REPO/archive/$TS_PIN_COMMIT.tar.gz (tarball method only)
# ==============================================================================================

TS_REPO="https://github.com/ExJtac/tokenstretch"
MARKER="# managed by the tokenstretch installer (scripts/install.sh)"

DRY_RUN=0
UNINSTALL=0
ADD_TO_PATH=0
METHOD=""
SOURCE=""
TARBALL_URL=""
PYTHON=""
TAG="$TS_PIN_TAG"
COMMIT="$TS_PIN_COMMIT"
SHA256="$TS_PIN_TARBALL_SHA256"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/tokenstretch"
BIN_DIR="$HOME/.local/bin"

usage() {
    cat <<'USAGE'
Usage: sh install.sh [options]

  --dry-run          print every step; change nothing
  --uninstall        remove what this script installed (keeps ~/.tokenstretch backups)
  --add-to-path      also add ~/.local/bin to PATH in your shell startup file (backed up)
  --python PATH      Python 3.11+ to use (default: newest python3.x found on PATH)
  --data-dir DIR     install folder (default: ${XDG_DATA_HOME:-~/.local/share}/tokenstretch)
  --bin-dir DIR      launcher folder (default: ~/.local/bin)

For testing a release before it is pinned (the commit check still applies):
  --source URL       git source (default: the GitHub repo); file:// works
  --tag TAG          tag to clone          --commit SHA   commit it must equal
  --method git|tarball                     --tarball-url URL  --sha256 HEX
USAGE
}

say() { printf '%s\n' "$*"; }
die() { printf 'tokenstretch installer: %s\n' "$*" >&2; exit 1; }
step() { if [ "$DRY_RUN" = 1 ]; then say "[dry run] would $*"; else say "-> $*"; fi; }

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --uninstall) UNINSTALL=1 ;;
        --add-to-path) ADD_TO_PATH=1 ;;
        --python) PYTHON="${2:?--python needs a path}"; shift ;;
        --data-dir) DATA_DIR="${2:?--data-dir needs a folder}"; shift ;;
        --bin-dir) BIN_DIR="${2:?--bin-dir needs a folder}"; shift ;;
        --source) SOURCE="${2:?--source needs a URL}"; shift ;;
        --tag) TAG="${2:?--tag needs a tag}"; shift ;;
        --commit) COMMIT="${2:?--commit needs a commit}"; shift ;;
        --method) METHOD="${2:?--method needs git or tarball}"; shift ;;
        --tarball-url) TARBALL_URL="${2:?--tarball-url needs a URL}"; shift ;;
        --sha256) SHA256="${2:?--sha256 needs a hash}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
    shift
done

case "$DATA_DIR$BIN_DIR" in
    *[\"\'\`\$\\]*) die "install paths may not contain quotes, backticks, \$ or backslashes" ;;
esac
VENV="$DATA_DIR/venv"
SRC_DIR="$DATA_DIR/src"
MANIFEST="$DATA_DIR/install-manifest.txt"
SHIM="$BIN_DIR/tokenstretch"
PATH_LINE="export PATH=\"$BIN_DIR:\$PATH\" $MARKER"

is_ours_shim() { [ -f "$SHIM" ] && grep -qF "$MARKER" "$SHIM"; }

# ---- uninstall --------------------------------------------------------------------------------
if [ "$UNINSTALL" = 1 ]; then
    found=0
    if is_ours_shim; then
        found=1; step "remove the launcher $SHIM"
        [ "$DRY_RUN" = 1 ] || rm -f "$SHIM"
    elif [ -e "$SHIM" ]; then
        say "Leaving $SHIM alone: this installer did not create it."
    fi
    if [ -f "$MANIFEST" ]; then
        created=$(grep '^rc-created:' "$MANIFEST" || true)
        while IFS= read -r line; do
            case "$line" in
                rc:*)
                    rc="${line#rc:}"
                    if [ -f "$rc" ] && grep -qF "$MARKER" "$rc"; then
                        found=1; step "remove the PATH line this installer added to $rc"
                        if [ "$DRY_RUN" = 0 ]; then
                            tmp="$rc.tokenstretch-tmp.$$"
                            grep -vF "$MARKER" "$rc" > "$tmp" || true
                            cat "$tmp" > "$rc" && rm -f "$tmp"
                            case "$created" in *"rc-created:$rc"*) [ -s "$rc" ] || rm -f "$rc" ;; esac
                        fi
                    fi ;;
            esac
        done < "$MANIFEST"
    fi
    if [ -f "$MANIFEST" ] && grep -qF "$MARKER" "$MANIFEST"; then
        found=1; step "remove $DATA_DIR (the installed copy and its venv)"
        dirs=$(sed -n 's/^dir-created://p' "$MANIFEST" | sort -ru)
        if [ "$DRY_RUN" = 0 ]; then
            rm -rf "$DATA_DIR"
            # Folders the installer created, deepest first, only if they are now empty.
            printf '%s\n' "$dirs" | while IFS= read -r d; do
                if [ -n "$d" ]; then rmdir "$d" 2>/dev/null || true; fi
            done
        fi
    elif [ -e "$DATA_DIR" ]; then
        say "Leaving $DATA_DIR alone: it has no installer manifest."
    fi
    [ "$found" = 1 ] || say "Nothing to remove: tokenstretch is not installed by this script here."
    say "Kept ~/.tokenstretch (setup backups and manifests). Run \`tokenstretch undo\` before uninstalling to revert setup changes."
    [ "$DRY_RUN" = 1 ] && say "Dry run: nothing was changed."
    exit 0
fi

# ---- 1. Python 3.11+ --------------------------------------------------------------------------
has_cmd() { command -v "$1" >/dev/null 2>&1; }
# On macOS, /usr/bin/python3 and /usr/bin/git are stubs that open an install dialog without the
# Command Line Tools; skip them unless the tools are present.
mac_stub() {
    [ "$(uname -s)" = Darwin ] || return 1
    case "$(command -v "$1" 2>/dev/null)" in /usr/bin/*) xcode-select -p >/dev/null 2>&1 && return 1; return 0 ;; esac
    return 1
}
py_ok() {
    v=$("$1" -c 'import sys; print("%d %d" % sys.version_info[:2])' 2>/dev/null) || return 1
    maj=${v% *}; min=${v#* }
    [ "$maj" -gt 3 ] || { [ "$maj" -eq 3 ] && [ "$min" -ge 11 ]; }
}
if [ -n "$PYTHON" ]; then
    py_ok "$PYTHON" || die "$PYTHON is not Python 3.11 or newer ($("$PYTHON" --version 2>&1 || echo unknown)). Install Python 3.11+ and pass --python."
else
    for c in python3.14 python3.13 python3.12 python3.11 python3; do
        if has_cmd "$c" && ! mac_stub "$c" && py_ok "$c"; then PYTHON=$(command -v "$c"); break; fi
    done
    [ -n "$PYTHON" ] || die "Python 3.11 or newer was not found. Install it (python.org, Homebrew, or your package manager) and run this again, or pass --python."
fi
say "Python: $PYTHON ($("$PYTHON" --version 2>&1))"

# ---- 2. the pinned source ---------------------------------------------------------------------
[ -n "$COMMIT" ] || die "No release is pinned in this copy of install.sh yet (TS_PIN_COMMIT is empty). Use the install line from a published release, or pass --tag and --commit to test one."
printf '%s' "$COMMIT" | grep -Eq '^[0-9a-f]{40}$' || die "the pinned commit must be a full 40-character hex hash: $COMMIT"
if [ -z "$METHOD" ]; then
    if [ -n "$TAG" ] && has_cmd git && ! mac_stub git; then METHOD=git
    else METHOD=tarball; fi
fi
case "$METHOD" in
    git) [ -n "$TAG" ] || die "the git method needs a tag (--tag)"; has_cmd git || die "git is not installed; use --method tarball" ;;
    tarball) [ -n "$SHA256" ] || die "the tarball method needs a pinned SHA-256 (TS_PIN_TARBALL_SHA256 or --sha256); refusing to install unverified code" ;;
    *) die "--method must be git or tarball" ;;
esac
[ -n "$SOURCE" ] || SOURCE="$TS_REPO.git"
[ -n "$TARBALL_URL" ] || TARBALL_URL="$TS_REPO/archive/$COMMIT.tar.gz"
say "Release: ${TAG:-untagged} at commit $COMMIT (method: $METHOD)"

if is_ours_shim; then :; elif [ -e "$SHIM" ]; then
    die "$SHIM exists and was not created by this installer; move it or pass --bin-dir"
fi
if [ -e "$DATA_DIR" ] && ! { [ -f "$MANIFEST" ] && grep -qF "$MARKER" "$MANIFEST"; }; then
    die "$DATA_DIR exists and was not created by this installer; move it or pass --data-dir"
fi

if [ "$DRY_RUN" = 1 ]; then
    if [ "$METHOD" = git ]; then
        step "run: git clone --depth 1 --branch $TAG $SOURCE <temp dir>  (the only network use)"
        step "check that the clone's HEAD is $COMMIT, else stop"
    else
        step "download $TARBALL_URL  (the only network use)"
        step "check its SHA-256 is $SHA256, else stop"
    fi
else
    TMP=$(mktemp -d "${TMPDIR:-/tmp}/tokenstretch-install.XXXXXX")
    trap 'rm -rf "$TMP"' EXIT INT TERM
    if [ "$METHOD" = git ]; then
        step "clone $SOURCE at tag $TAG (the only network use)"
        GIT_TERMINAL_PROMPT=0 GIT_CONFIG_NOSYSTEM=1 git -c core.hooksPath=/dev/null -c advice.detachedHead=false \
            clone --quiet --depth 1 --branch "$TAG" --template= "$SOURCE" "$TMP/src" \
            || die "could not clone $SOURCE at $TAG"
        head=$(git -C "$TMP/src" rev-parse HEAD)
        [ "$head" = "$COMMIT" ] || die "tag $TAG points at $head, not the pinned commit $COMMIT. Refusing to install."
        rm -rf "$TMP/src/.git"
        say "   verified: HEAD is $COMMIT"
    else
        step "download $TARBALL_URL (the only network use)"
        case "$TARBALL_URL" in file://*) proto="=file" ;; *) proto="=https" ;; esac
        curl --proto "$proto" -fsSL -o "$TMP/src.tar.gz" "$TARBALL_URL" || die "could not download $TARBALL_URL"
        got=$("$PYTHON" -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$TMP/src.tar.gz")
        [ "$got" = "$SHA256" ] || die "SHA-256 mismatch for the download (got $got, pinned $SHA256). Refusing to install."
        say "   verified: SHA-256 $got"
        "$PYTHON" - "$TMP/src.tar.gz" "$TMP/src" <<'PY' || die "could not unpack the tarball"
import os, sys, tarfile
src, dest = sys.argv[1], sys.argv[2]
with tarfile.open(src, "r:gz") as tf:
    names = [m.name for m in tf.getmembers()]
    tops = {n.split("/", 1)[0] for n in names if n}
    if len(tops) != 1 or any(n.startswith("/") or ".." in n.split("/") for n in names):
        sys.exit("unexpected tarball layout")
    os.makedirs(dest + ".x")
    kw = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
    tf.extractall(dest + ".x", **kw)
os.rename(os.path.join(dest + ".x", tops.pop()), dest)
PY
    fi
    if [ ! -f "$TMP/src/tokenstretch/__init__.py" ] || [ ! -f "$TMP/src/pyproject.toml" ]; then
        die "the fetched source does not look like tokenstretch"
    fi
fi

# ---- 3. copy + private venv -------------------------------------------------------------------
step "copy the release to $SRC_DIR"
step "create a private venv at $VENV (python -m venv --without-pip; nothing downloaded)"
step "point that venv at $SRC_DIR (a .pth file; no pip install)"
if [ "$DRY_RUN" = 0 ]; then
    new_dirs=""
    for d in "$BIN_DIR" "$(dirname "$BIN_DIR")" "$(dirname "$DATA_DIR")" "$(dirname "$(dirname "$DATA_DIR")")"; do
        [ -e "$d" ] || new_dirs="$new_dirs
dir-created:$d"
    done
    mkdir -p "$DATA_DIR"
    printf '%s\n%s%s\n' "$MARKER" "commit:$COMMIT" "$new_dirs" > "$MANIFEST.new"
    if [ -f "$MANIFEST" ]; then grep -e '^rc' -e '^dir-created:' "$MANIFEST" >> "$MANIFEST.new" || true; fi
    mv "$MANIFEST.new" "$MANIFEST"
    rm -rf "$SRC_DIR" "$VENV"
    mv "$TMP/src" "$SRC_DIR"
    "$PYTHON" -m venv --without-pip "$VENV" || die "could not create a venv with $PYTHON (on Debian/Ubuntu: install python3-venv)"
    purelib=$("$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')
    mkdir -p "$purelib"
    printf '%s\n' "$SRC_DIR" > "$purelib/tokenstretch-src.pth"
fi

# ---- 4. launcher, PATH ------------------------------------------------------------------------
step "write the launcher $SHIM"
if [ "$DRY_RUN" = 0 ]; then
    mkdir -p "$BIN_DIR"
    cat > "$SHIM" <<SH
#!/bin/sh
$MARKER
exec "$VENV/bin/python" -m tokenstretch "\$@"
SH
    chmod 755 "$SHIM"
fi

on_path=0
case ":$PATH:" in *":$BIN_DIR:"*) on_path=1 ;; esac
if [ "$on_path" = 0 ]; then
    if [ "$ADD_TO_PATH" = 1 ]; then
        case "${SHELL:-}" in
            */zsh) rc="$HOME/.zshrc" ;;
            */bash) if [ "$(uname -s)" = Darwin ]; then rc="$HOME/.bash_profile"; else rc="$HOME/.bashrc"; fi ;;
            *) rc="$HOME/.profile" ;;
        esac
        if [ -f "$rc" ] && grep -qF "$MARKER" "$rc"; then
            say "$rc already has the PATH line."
        else
            [ -f "$rc" ] && step "back up $rc to $DATA_DIR/backups/"
            step "append to $rc: $PATH_LINE"
            if [ "$DRY_RUN" = 0 ]; then
                if [ -f "$rc" ]; then mkdir -p "$DATA_DIR/backups"; cp -p "$rc" "$DATA_DIR/backups/$(basename "$rc").$(date +%Y%m%d%H%M%S)"
                else printf 'rc-created:%s\n' "$rc" >> "$MANIFEST"; fi
                if [ -s "$rc" ] && [ "$(tail -c 1 "$rc")" != "" ]; then printf '\n' >> "$rc"; fi
                printf '%s\n' "$PATH_LINE" >> "$rc"
                printf 'rc:%s\n' "$rc" >> "$MANIFEST"
            fi
        fi
    else
        say "Note: $BIN_DIR is not on your PATH. Add it yourself, e.g.:"
        say "  export PATH=\"$BIN_DIR:\$PATH\""
        say "or re-run with --add-to-path to have the installer add that line (backed up; --uninstall removes it)."
    fi
fi

if [ "$DRY_RUN" = 1 ]; then
    say "Dry run: nothing was downloaded or changed."
    exit 0
fi
"$SHIM" --version >/dev/null || die "the launcher did not run"
say "Installed $("$SHIM" --version) at $DATA_DIR."
say "Nothing was set up yet. Next: \`tokenstretch start\` shows what it would change and asks before changing anything."
say "Remove it any time: sh install.sh --uninstall"
