#!/bin/bash
# Store the three souljah-vendor-check secrets in the macOS Keychain.
# The values are read silently, never echoed, and never written anywhere else.

set -euo pipefail

SERVICES=(
  "souljah-vendor-check-client-id"
  "souljah-vendor-check-client-secret"
  "souljah-vendor-check-password"
)

# The prompts are the first thing this script does.
declare -a VALUES=()
for service in "${SERVICES[@]}"; do
  printf 'Enter %s: ' "$service" >&2
  IFS= read -rs value
  printf '\n' >&2
  if [ -z "$value" ]; then
    printf 'error: empty value for %s\n' "$service" >&2
    exit 1
  fi
  VALUES+=("$value")
done

for index in "${!SERVICES[@]}"; do
  service="${SERVICES[$index]}"
  security add-generic-password -U -a "${USER:-$(id -un)}" -s "$service" -w "${VALUES[$index]}"
  printf 'stored %s\n' "$service" >&2
done
