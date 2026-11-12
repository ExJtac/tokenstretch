# Releasing

The maintainer approves and runs every release. Nothing in this repo creates tags, pushes, or publishes by itself.

## Pinning the installer

`scripts/install.sh` installs exactly one release. The pin is the `PINNED RELEASE` block at the top of the script, and nothing else in the script changes per release:

```sh
TS_PIN_TAG=""            # release tag, e.g. v0.1.0
TS_PIN_COMMIT=""         # full 40-character commit the tag must point at
TS_PIN_TARBALL_SHA256="" # SHA-256 of https://github.com/ExJtac/tokenstretch/archive/<commit>.tar.gz
```

A commit can't contain its own hash, so the pin always goes in a follow-up commit. `scripts/release-pin.sh` fills it from a local tag. It is local only: no tags created, nothing pushed, nothing downloaded.

## Steps

1. **Release commit (A).** Set the version in `pyproject.toml` and `tokenstretch/identity.py`, and move `CHANGELOG.md` `[Unreleased]` into a dated `[X.Y.Z]` section. Run the tests, including the network-blocked run. That run needs Linux (`unshare -rn`); on macOS, skip it locally and rely on CI, which runs it on every push. Don't publish until CI is green on the release commit.
2. **Tag A locally:** `git tag -a vX.Y.Z -m "tokenstretch X.Y.Z"`. Make the tag on the machine that will push. A commit applied with `git am` gets a new hash, so never copy a hash from another machine.
3. **Pin commit (B):** `sh scripts/release-pin.sh vX.Y.Z`. It checks that the tagged commit has version X.Y.Z everywhere, then fills `TS_PIN_TAG` and `TS_PIN_COMMIT` from the local tag and removes the README's "Not released yet" line. It leaves the tarball SHA-256 empty and commits. Preview with `--dry-run`.
4. **Check B locally:** in a temporary `HOME`, run `sh scripts/install.sh --source "file://$PWD"`. It clones the tag from this repo and checks HEAD against the pin. Then `tokenstretch --version`, then `--uninstall`.
5. **Publish** (only with the maintainer's yes): `git push origin main vX.Y.Z`, then create the GitHub release from the tag with the release notes. Put B's raw `scripts/install.sh` link in the notes as the link that never changes. `release-pin.sh` prints it.
6. **Optional, after the tag is on GitHub: the tarball SHA-256.** GitHub builds `archive/<commit>.tar.gz` itself. Its bytes aren't guaranteed to match a local `git archive`, so the hash can only be taken from GitHub's file:
   ```sh
   curl -fsSLo /tmp/ts.tar.gz https://github.com/ExJtac/tokenstretch/archive/<commit>.tar.gz
   shasum -a 256 /tmp/ts.tar.gz
   sh scripts/release-pin.sh vX.Y.Z --sha256 <hash>    # commits "Pin installer tarball SHA-256"
   git push origin main
   ```
   Until then, the git-clone check is the only install path. Without git, the installer refuses rather than installing unverified code.

If a tag ever moves, the git method refuses (HEAD ≠ pinned commit), and `release-pin.sh` refuses to re-pin a tag to a different commit. If GitHub's tarball bytes change, the tarball method refuses (SHA-256 mismatch). Neither falls back to unverified code.
