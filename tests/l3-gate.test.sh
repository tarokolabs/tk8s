#!/usr/bin/env bash
# scripts/wait-l3.sh <sha>: green if any L3 run for the commit succeeded; red only when every run is
# complete and none succeeded (a tag push and a main push each get their own run, and one may be cancelled).
source "$(dirname "$0")/lib.sh"
cd "$(dirname "$0")/.." || exit 1
TMP=$(mktemp -d); STUB="$TMP/bin"; mkdir -p "$STUB"; export STUB
cat > "$STUB/gh" <<'SH'
#!/usr/bin/env bash
# Answer with the JSON listed in $STUB/answers, one line per call.
n=$(cat "$STUB/calls" 2>/dev/null || echo 0); echo $((n+1)) > "$STUB/calls"
sed -n "$((n+1))p" "$STUB/answers"
SH
printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB/sleep"; chmod +x "$STUB"/*
run() { : > "$STUB/calls"; printf '%s\n' "$@" > "$STUB/answers"; out=$(PATH="$STUB:$PATH" WAIT_L3_ROUNDS=3 scripts/wait-l3.sh deadbeef 2>&1); rc=$?; }
run '[{"status":"completed","conclusion":"cancelled"},{"status":"completed","conclusion":"success"}]'
assert_eq "0" "$rc" "a cancelled newer run does not hide a green one"
run '[{"status":"completed","conclusion":"success"}]'
assert_eq "0" "$rc" "single green run"
run '[{"status":"completed","conclusion":"failure"}]'
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "single red run fails"
assert_contains "$out" "failure" "red run is named"
run '[{"status":"in_progress","conclusion":null},{"status":"completed","conclusion":"cancelled"}]' '[{"status":"completed","conclusion":"success"},{"status":"completed","conclusion":"cancelled"}]'
assert_eq "0" "$rc" "waits for an in-progress run and accepts it when it succeeds"
run '[]' '[]' '[]'
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "no run at all within the rounds fails"
rm -rf "$TMP"; finish
