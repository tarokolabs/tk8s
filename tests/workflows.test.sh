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

# ci.yaml: lint and unit tests are separate jobs, so a red check names what actually failed
ci_jobs=$(awk '/^jobs:/{f=1;next} f && /^  [a-z0-9_-]+:$/{sub(":","");print $1}' .github/workflows/ci.yaml | tr '\n' ' ')
assert_eq "lint test " "$ci_jobs" "ci.yaml runs lint and test as separate jobs"
assert_contains "$(awk '/^  test:/{f=1} f' .github/workflows/ci.yaml)" "tests/run.sh" "unit tests live in the test job"
if awk '/^  lint:/{f=1} /^  test:/{f=0} f' .github/workflows/ci.yaml | grep -q "tests/run.sh"; then echo "FAIL  lint job does not run the unit tests"; FAILURES=$((FAILURES+1)); else echo "PASS  lint job does not run the unit tests"; fi
