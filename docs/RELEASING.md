# Releasing

The maintainer approves and runs every release. Nothing in this repo creates tags or releases by itself.

## Pinning the installer

`scripts/install.sh` installs exactly one release. The pin is the `PINNED RELEASE` block at the top of the script, and nothing else in the script changes per release:

```sh
TS_PIN_TAG=""            # release tag, e.g. v0.1.0
TS_PIN_COMMIT=""         # full 40-character commit the tag must point at
TS_PIN_TARBALL_SHA256="" # SHA-256 of https://github.com/ExJtac/tokenstretch/archive/<commit>.tar.gz
```

A commit can't contain its own hash, so the pin goes in a follow-up commit:

1. Tag the release commit (for example `v0.1.0`) and push the tag.
2. Fill in the pin from that tag. Get the commit with `git rev-parse v0.1.0^{commit}`. For the tarball hash, download `https://github.com/ExJtac/tokenstretch/archive/<commit>.tar.gz` and run `shasum -a 256` on it.
3. Check it: `sh scripts/install.sh --dry-run`, then install and uninstall into a temporary `HOME`.
4. Commit the pin to `main`. Put that commit's raw `scripts/install.sh` link in the release notes as the immutable install link.

If a tag ever moves, the git method refuses (HEAD ≠ pinned commit). If GitHub's tarball bytes change, the tarball method refuses (SHA-256 mismatch). Neither falls back to unverified code.
