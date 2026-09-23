#!/usr/bin/env bash
# Minimal assertion helpers for tkctl tests. No external dependencies.
FAILURES=0
assert_eq() {  # expected actual message
  if [ "$1" == "$2" ]; then echo "PASS  $3"; else echo "FAIL  $3"; echo "      expected: $1"; echo "      actual:   $2"; FAILURES=$((FAILURES+1)); fi
}
assert_contains() {  # haystack needle message
  if [[ "$1" == *"$2"* ]]; then echo "PASS  $3"; else echo "FAIL  $3"; echo "      missing: $2"; echo "      in:      ${1:0:300}"; FAILURES=$((FAILURES+1)); fi
}
assert_fails() {  # message cmd...
  local msg=$1; shift
  if "$@" >/dev/null 2>&1; then echo "FAIL  $msg (expected non-zero exit)"; FAILURES=$((FAILURES+1)); else echo "PASS  $msg"; fi
}
finish() { echo; echo "failures: ${FAILURES}"; [ "${FAILURES}" == 0 ]; }
