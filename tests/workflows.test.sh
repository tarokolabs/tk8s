#!/usr/bin/env bash
# Workflow structure that no unit can run: L3 verifies the node image built from the checkout under
# test (not whatever GHCR holds), and tokens stay read-only except where a job writes.
source "$(dirname "$0")/lib.sh"
cd "$(dirname "$0")/.." || exit 1
l3=.github/workflows/l3.yaml
b=$(grep -n 'podman build' $l3 | head -1 | cut -d: -f1); c=$(grep -n 'tkctl create cluster' $l3 | head -1 | cut -d: -f1)
if [ -n "$b" ] && [ -n "$c" ] && [ "$b" -lt "$c" ]; then echo "PASS  L3 builds the node image from images/node before creating the cluster"; else echo "FAIL  L3 builds the node image from images/node before creating the cluster"; FAILURES=$((FAILURES+1)); fi
for w in .github/workflows/*.y*ml; do
  if awk '/^permissions:/{p=1;next} p&&/^  /{print;next} p{exit}' "$w" | grep -qx '  contents: read'; then echo "PASS  $w top-level token is read-only"; else echo "FAIL  $w top-level token is read-only"; FAILURES=$((FAILURES+1)); fi
done
assert_contains "$(cat .github/workflows/release.yml)" "scripts/wait-l3.sh" "release gate uses the tested wait script"
finish
