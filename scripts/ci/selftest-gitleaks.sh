#!/usr/bin/env bash
# selftest-gitleaks.sh — prove the secret-scan gate is not blind, before the
# run that matters is believed.
#
# Owner: Node 6 (Antigravity).
#
# Three positive canaries and a file of negative controls. The canaries are
# derived at runtime so no secret-shaped string ever exists in git history, and
# the derivation is deterministic so a failure here is reproducible rather than
# flaky.
#
# Runnable locally once gitleaks, jq, openssl and ssh-keygen are on PATH:
#   bash scripts/ci/selftest-gitleaks.sh
#
# Exit codes: 0 every canary caught and no false positive · 1 the gate is blind
# or fires on a benign line.

set -euo pipefail

workspace="${GITHUB_WORKSPACE:-$(git rev-parse --show-toplevel)}"
scratch="${RUNNER_TEMP:-$(mktemp -d)}"
dir="$scratch/canaries"
report="$scratch/canary-report.json"
rm -rf "$dir"
mkdir -p "$dir"

derive() { printf '%s' "$1" | openssl dgst -sha256 -binary | base64 | tr -dc 'A-Za-z0-9' | cut -c1-32; }
api_value="$(derive nexusdev-canary-api-v1)"
url_value="$(derive nexusdev-canary-url-v1)"

ssh-keygen -q -t ed25519 -N '' -C canary -f "$dir/id_canary"
printf 'NEXUS_API_TOKEN="%s"\n' "$api_value" > "$dir/api.env"
printf 'HERMES_ENDPOINT=https://%s:%s@%s\n' nexus "$url_value" hermes.example.invalid > "$dir/endpoint.env"

# Negative controls: none of these may be flagged as url-embedded-credentials.
cat > "$dir/benign.txt" <<'EOF'
HERMES_ENDPOINT=http://localhost:11434
git@github.com:mntoyg/NexusDev.git
a URL of the form https://user:pass@host
postgres://app:${DB_PASSWORD}@db:5432/app
EOF

gitleaks dir "$dir" \
  --config "$workspace/.gitleaks.toml" \
  --no-banner --redact --exit-code 0 \
  --report-format json --report-path "$report"
test -s "$report" || { echo "::error::gitleaks wrote no report"; exit 1; }

fail=0
expect() { # <rule-id> <file-suffix>
  if jq -e --arg r "$1" --arg f "$2" \
       'any(.[]; .RuleID == $r and (.File | endswith($f)))' "$report" >/dev/null; then
    echo "ok: $1 detected in $2"
  else
    echo "::error::gate is blind: $1 was not detected in $2"
    fail=1
  fi
}
expect private-key              id_canary
expect generic-api-key          api.env
expect url-embedded-credentials endpoint.env

if jq -e 'any(.[]; .RuleID == "url-embedded-credentials" and (.File | endswith("benign.txt")))' "$report" >/dev/null; then
  echo "::error::url-embedded-credentials fired on a benign control line"
  fail=1
else
  echo "ok: no false positive on benign controls"
fi
exit "$fail"
