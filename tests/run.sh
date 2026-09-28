#!/usr/bin/env bash
# Runs every tests/*.test.sh; exits non-zero if any file fails.
cd "$(dirname "$0")/.." || exit 1
rc=0
for t in tests/*.test.sh; do
  [ -e "$t" ] || { echo "no tests"; break; }
  echo "=== ${t}"
  bash "${t}" || rc=1
done
exit ${rc}
