#!/usr/bin/env bash
# install-gitleaks.sh — fetch a checksum-verified gitleaks onto the runner.
#
# Owner: Node 6 (Antigravity).
#
# The version and the digest come from the workflow's `env:` block, so they sit
# next to the comment telling a maintainer to bump them together. They are bound
# and checked here rather than trusted: `set -u` catches a variable that is
# unset, but an empty one would build a plausible URL for release "v" and the
# failure would surface as a confusing 404 instead of the real cause.
#
# Exit codes: 0 installed · 1 misconfigured, download failed, or the digest did
# not match.

set -euo pipefail

VERSION="${GITLEAKS_VERSION:-}"
DIGEST="${GITLEAKS_SHA256:-}"
SCRATCH="${RUNNER_TEMP:-}"
PATH_FILE="${GITHUB_PATH:-}"

require() { # <description> <value>
  if [ -z "$2" ]; then
    echo "::error::$1 must be set and non-empty for this step to mean anything" >&2
    exit 1
  fi
}

require GITLEAKS_VERSION "$VERSION"
require GITLEAKS_SHA256 "$DIGEST"
require RUNNER_TEMP "$SCRATCH"
require GITHUB_PATH "$PATH_FILE"

archive="gitleaks_${VERSION}_linux_x64.tar.gz"
curl -fsSL --proto '=https' --tlsv1.2 --retry 3 \
  -o "$SCRATCH/$archive" \
  "https://github.com/gitleaks/gitleaks/releases/download/v${VERSION}/${archive}"
(cd "$SCRATCH" && echo "${DIGEST}  ${archive}" | sha256sum --check --strict -)
mkdir -p "$SCRATCH/bin"
tar -xzf "$SCRATCH/$archive" -C "$SCRATCH/bin" gitleaks
echo "$SCRATCH/bin" >> "$PATH_FILE"
"$SCRATCH/bin/gitleaks" version
