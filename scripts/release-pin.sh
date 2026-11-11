#!/bin/sh
# Maintainer helper: fill the PINNED RELEASE block in scripts/install.sh from a local tag and
# commit it. Local only: it never creates or moves tags, never pushes, and never downloads.
#
#   sh scripts/release-pin.sh v0.1.0                 # pin tag + commit (SHA-256 left empty)
#   sh scripts/release-pin.sh v0.1.0 --sha256 HEX    # later: add the GitHub tarball's SHA-256
#   add --dry-run to print the change without writing or committing
#
# Run it on the branch that contains the tag (the tagged commit or a descendant), with a clean
# worktree. The commit uses your git identity (or GIT_AUTHOR_* / GIT_COMMITTER_* if set).
set -eu

die() { printf 'release-pin: %s\n' "$*" >&2; exit 1; }

TAG=""
SHA=""
DRY_RUN=0
while [ $# -gt 0 ]; do
    case "$1" in
        --sha256) SHA="${2:?--sha256 needs a hash}"; shift ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        -*) die "unknown option: $1" ;;
        *) [ -z "$TAG" ] || die "only one tag"; TAG="$1" ;;
    esac
    shift
done
[ -n "$TAG" ] || die "usage: sh scripts/release-pin.sh TAG [--sha256 HEX] [--dry-run]"
printf '%s' "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' || die "tag must look like v1.2.3: $TAG"
[ -z "$SHA" ] || printf '%s' "$SHA" | grep -Eq '^[0-9a-f]{64}$' || die "--sha256 must be 64 lowercase hex characters"

ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || die "run this inside the tokenstretch git repo"
cd "$ROOT"
SCRIPT=scripts/install.sh
[ -f "$SCRIPT" ] || die "$SCRIPT not found"
[ -z "$(git status --porcelain)" ] || die "the worktree has uncommitted changes; commit or stash them first"

COMMIT=$(git rev-parse -q --verify "refs/tags/$TAG^{commit}") || die "no local tag $TAG (create it first: git tag -a $TAG -m \"tokenstretch ${TAG#v}\" <commit>)"
git merge-base --is-ancestor "$COMMIT" HEAD || die "$TAG ($COMMIT) is not HEAD or an ancestor of HEAD"

# The tagged commit must carry the same version everywhere.
VER=${TAG#v}
git show "$COMMIT:pyproject.toml" | grep -qx "version = \"$VER\"" || die "pyproject.toml at $TAG is not version $VER"
git show "$COMMIT:tokenstretch/identity.py" | grep -qx "VERSION = \"$VER\"" || die "tokenstretch/identity.py at $TAG is not VERSION $VER"
git show "$COMMIT:CHANGELOG.md" | grep -q "^## \[$VER\]" || die "CHANGELOG.md at $TAG has no [$VER] section"

cur_tag=$(sed -n 's/^TS_PIN_TAG="\([^"]*\)".*/\1/p' "$SCRIPT")
cur_commit=$(sed -n 's/^TS_PIN_COMMIT="\([^"]*\)".*/\1/p' "$SCRIPT")
cur_sha=$(sed -n 's/^TS_PIN_TARBALL_SHA256="\([^"]*\)".*/\1/p' "$SCRIPT")
[ "$(grep -c '^TS_PIN_TAG=' "$SCRIPT")" = 1 ] || die "expected exactly one TS_PIN_TAG line in $SCRIPT"
[ -n "$SHA" ] || SHA="$cur_sha"
if [ "$cur_tag" = "$TAG" ] && [ "$cur_commit" = "$COMMIT" ] && [ "$cur_sha" = "$SHA" ]; then
    printf 'Already pinned: %s at %s (tarball SHA-256: %s). Nothing to do.\n' "$TAG" "$COMMIT" "${SHA:-not set}"
    exit 0
fi
if [ "$cur_tag" = "$TAG" ] && [ -n "$cur_commit" ] && [ "$cur_commit" != "$COMMIT" ]; then
    die "$SCRIPT already pins $TAG to $cur_commit, but the local tag is $COMMIT. Tags must not move."
fi

TMP=$(mktemp "${TMPDIR:-/tmp}/release-pin.XXXXXX")
trap 'rm -f "$TMP" "$TMP.readme"' EXIT INT TERM
sed -e "s/^TS_PIN_TAG=\"[^\"]*\"/TS_PIN_TAG=\"$TAG\"/" \
    -e "s/^TS_PIN_COMMIT=\"[^\"]*\"/TS_PIN_COMMIT=\"$COMMIT\"/" \
    -e "s/^TS_PIN_TARBALL_SHA256=\"[^\"]*\"/TS_PIN_TARBALL_SHA256=\"$SHA\"/" "$SCRIPT" > "$TMP"

# Drop the README's "Not released yet" paragraph (and the blank line after it), if present.
README_CHANGED=0
if grep -q '^\*\*Not released yet\.\*\*' README.md; then
    awk 'skip && /^$/ {skip=0; next} /^\*\*Not released yet\.\*\*/ {skip=1; next} {skip=0; print}' README.md > "$TMP.readme"
    README_CHANGED=1
fi

printf 'Pin: tag %s, commit %s, tarball SHA-256 %s\n' "$TAG" "$COMMIT" "${SHA:-(empty: git clone check only)}"
diff -u "$SCRIPT" "$TMP" || true
[ "$README_CHANGED" = 0 ] || diff -u README.md "$TMP.readme" || true
if [ "$DRY_RUN" = 1 ]; then
    echo "Dry run: nothing written or committed."
    exit 0
fi

cat "$TMP" > "$SCRIPT"
[ "$README_CHANGED" = 0 ] || cat "$TMP.readme" > README.md
sh -n "$SCRIPT" || die "edited $SCRIPT does not parse; not committing"
git add "$SCRIPT" README.md
if [ -n "$SHA" ] && [ "$cur_commit" = "$COMMIT" ]; then
    msg="Pin installer tarball SHA-256 for $TAG"
else
    msg="Pin installer to $TAG ($COMMIT)"
fi
git commit -q -m "$msg" -m "scripts/install.sh now installs $TAG at $COMMIT.${SHA:+ Tarball SHA-256: $SHA.}"
PIN=$(git rev-parse HEAD)
echo "Committed $PIN: $msg"
echo "Immutable install link (after you push): https://raw.githubusercontent.com/ExJtac/tokenstretch/$PIN/scripts/install.sh"
echo "Nothing was pushed. Check it: sh scripts/install.sh --dry-run"
