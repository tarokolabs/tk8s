#!/usr/bin/env bash
# Golden tests: the complete --dry-run output for two shapes must not change unnoticed.
# Regenerate deliberately with: UPDATE_GOLDEN=1 tests/run.sh
source "$(dirname "$0")/lib.sh"
T="$(dirname "$0")/../bin/tkctl"; G="$(dirname "$0")/golden"
TMP=$(mktemp -d); export TAROKO_HOME="$TMP"
norm() { sed -E "s#$TMP#<TMP>#g; s#/(private/)?(tmp|var/folders)/[A-Za-z0-9._/-]+/clusters#<TMP>/clusters#g"; }
out=$($T create cluster --dry-run 2>&1 | norm)
[ "${UPDATE_GOLDEN:-}" == 1 ] && echo "$out" > "$G/default.dry-run.txt"
assert_eq "$(cat "$G/default.dry-run.txt")" "$out" "default dry-run matches golden"
out=$($T create cluster ha --control-planes 3 --workers 2 --cpu 4 --memory 6G --defer-join --dry-run 2>&1 | norm)
[ "${UPDATE_GOLDEN:-}" == 1 ] && echo "$out" > "$G/ha.dry-run.txt"
assert_eq "$(cat "$G/ha.dry-run.txt")" "$out" "ha dry-run matches golden"
rm -rf "$TMP"; finish
