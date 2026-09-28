#!/usr/bin/env bash
# install.sh against stubbed curl/sudo/git/podman/swapon: structure, reruns, checksum failures, TK_VERSION.
source "$(dirname "$0")/lib.sh"
cd "$(dirname "$0")/.." || exit 1
TMP=$(mktemp -d); STUB="$TMP/bin"; mkdir -p "$STUB" "$TMP/usr/local/bin" "$TMP/opt"; export STUB STUB_LOG="$TMP/calls.log"
v() { sed -nE "s/^$1: *\"?([^\"]+)\"?.*/\1/p" versions.yaml; }
K8S=$(sed -nE 's/^  default: *"([^"]+)"/\1/p' versions.yaml); TASKV=$(v task)
# The whole script must be inside a function called on the last line, so a truncated `curl | sh` runs nothing.
assert_contains "$(tail -n 1 install.sh 2>/dev/null)" 'main "$@"' "script body runs only from main on the last line"
assert_eq "1" "$(grep -c '^main() {' install.sh 2>/dev/null)" "one main function"
printf '#!/usr/bin/env bash\n[ "$1" = -n ] && shift; echo "sudo $*" >> "$STUB_LOG"; exec "$@"\n' > "$STUB/sudo"
printf '#!/usr/bin/env bash\necho "podman version 5.4.2"\n' > "$STUB/podman"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB/swapon"
printf '#!/usr/bin/env bash\necho "$*" >> "$STUB_LOG"; exit 0\n' > "$STUB/systemctl"
printf '#!/usr/bin/env bash\necho cgroup2fs\n' > "$STUB/stat"   # GNU stat -fc %T on Linux; macOS has no -f/-c
printf '#!/usr/bin/env bash\nif [ -x /usr/bin/sha256sum ]; then exec /usr/bin/sha256sum "$@"; fi; exec /usr/bin/shasum -a 256 "$@"\n' > "$STUB/sha256sum"   # macOS ships shasum only
cat > "$STUB/git" <<'SH'
#!/usr/bin/env bash
echo "git $*" >> "$STUB_LOG"
case "$1" in
  clone) d=${@: -1}; mkdir -p "$d/bin"; printf '#!/bin/sh\necho tkctl\n' > "$d/bin/tkctl"; chmod +x "$d/bin/tkctl"; mkdir -p "$d/.git" ;;
  -C) shift; d=$1; shift; case "$1" in rev-parse) echo "abc1234" ;; describe) echo "${TK_VERSION:-main}" ;; esac ;;
esac; exit 0
SH
cat > "$STUB/curl" <<'SH'
#!/usr/bin/env bash
# Serve fake binaries and checksums; TASK_BAD/KUBECTL_BAD corrupt the checksum files.
out=""; prev=""; url=""; for a in "$@"; do case "$prev" in -o) out=$a;; esac; case "$a" in http*|file:*) url=$a;; esac; prev=$a; done
echo "curl $url" >> "$STUB_LOG"
body() { case "$url" in
  *task_linux_amd64.tar.gz) printf 'FAKE-TASK' ;;
  *task_checksums.txt) h=$(printf 'FAKE-TASK' | sha256sum | awk '{print $1}'); [ -n "${TASK_BAD:-}" ] && h=deadbeef; printf '%s  task_linux_amd64.tar.gz\n' "$h" ;;
  */kubectl) printf 'FAKE-KUBECTL' ;;
  */kubectl.sha256) h=$(printf 'FAKE-KUBECTL' | sha256sum | awk '{print $1}'); [ -n "${KUBECTL_BAD:-}" ] && h=deadbeef; printf '%s' "$h" ;;
  *versions.yaml) cat "$PWD/versions.yaml" ;;
esac; }
if [ -n "$out" ]; then body > "$out"; else body; fi
SH
cat > "$STUB/tar" <<'SH'
#!/usr/bin/env bash
# `tar -xz -C DIR -f ARCHIVE task` from a fake archive: drop a fake task binary in DIR.
prev=""; for a in "$@"; do case "$prev" in -C) d=$a;; esac; prev=$a; done; printf '#!/bin/sh\necho "%s"\n' "${TASKV#v}" > "$d/task"; chmod +x "$d/task"   # go-task prints a bare version
SH
chmod +x "$STUB"/*; export TASKV
run() { : > "$STUB_LOG"; out=$(PATH="$STUB:/usr/bin:/bin" TK_INSTALL_DIR="$TMP/opt/tk8s" TK_BIN_DIR="$TMP/usr/local/bin" "$@" sh install.sh 2>&1); rc=$?; }

run env
assert_eq "0" "$rc" "fresh install exits 0"
assert_contains "$(cat "$STUB_LOG")" "go-task/task/releases/download/$TASKV/task_linux_amd64.tar.gz" "task downloaded at the pinned version"
assert_contains "$(cat "$STUB_LOG")" "dl.k8s.io/release/v$K8S/bin/linux/amd64/kubectl" "kubectl downloaded at the default kubernetes version"
assert_contains "$(cat "$STUB_LOG")" "git clone" "repo cloned"
assert_contains "$(cat "$STUB_LOG")" "--branch main" "default TK_VERSION is main"
if [ -x "$TMP/usr/local/bin/kubectl" ] && [ -x "$TMP/usr/local/bin/task" ]; then echo "PASS  binaries installed into TK_BIN_DIR"; else echo "FAIL  binaries installed into TK_BIN_DIR"; FAILURES=$((FAILURES+1)); fi
assert_eq "$TMP/opt/tk8s/bin/tkctl" "$(readlink "$TMP/usr/local/bin/tkctl")" "tkctl symlinked into TK_BIN_DIR"
assert_contains "$out" "tkctl create cluster" "prints the next step"
# The checkout belongs to the installing user: git refuses root-owned repos for other users, and tkctl version needs git.
assert_contains "$(cat "$STUB_LOG")" "sudo install -d -o $(id -u) -g $(id -g) $TMP/opt/tk8s" "checkout directory created for the installing user"
if grep -q "sudo git" "$STUB_LOG"; then echo "FAIL  git runs as the user, not root"; FAILURES=$((FAILURES+1)); else echo "PASS  git runs as the user, not root"; fi
# rerun: existing checkout is updated, not re-cloned; same task version is not downloaded again; symlink refreshed
run env TK_VERSION=v2026.10.0
assert_eq "0" "$rc" "rerun exits 0"
if grep -q "git clone" "$STUB_LOG"; then echo "FAIL  rerun does not clone again"; FAILURES=$((FAILURES+1)); else echo "PASS  rerun does not clone again"; fi
assert_contains "$(cat "$STUB_LOG")" "git -C $TMP/opt/tk8s fetch" "rerun fetches the existing checkout"
assert_contains "$(cat "$STUB_LOG")" "checkout --quiet v2026.10.0" "TK_VERSION selects the tag"
if grep -q "task_linux_amd64.tar.gz" "$STUB_LOG"; then echo "FAIL  rerun skips an up-to-date task binary"; FAILURES=$((FAILURES+1)); else echo "PASS  rerun skips an up-to-date task binary"; fi
# checksum failures leave nothing behind
rm -f "$TMP/usr/local/bin/task" "$TMP/usr/local/bin/kubectl"
run env TASK_BAD=1
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "bad task checksum: exit non-zero"
assert_contains "$out" "checksum" "bad task checksum: named error"
if [ -e "$TMP/usr/local/bin/task" ]; then echo "FAIL  bad task checksum: no binary left"; FAILURES=$((FAILURES+1)); else echo "PASS  bad task checksum: no binary left"; fi
run env KUBECTL_BAD=1
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "bad kubectl checksum: exit non-zero"
if [ -e "$TMP/usr/local/bin/kubectl" ]; then echo "FAIL  bad kubectl checksum: no binary left"; FAILURES=$((FAILURES+1)); else echo "PASS  bad kubectl checksum: no binary left"; fi
# TK_RAW points versions.yaml at a local checkout (CI installs from the commit under test)
run env TK_RAW="file://$PWD"
assert_eq "0" "$rc" "TK_RAW install exits 0"
assert_contains "$(cat "$STUB_LOG")" "curl file://$PWD/versions.yaml" "versions.yaml fetched from TK_RAW"
# prerequisites
printf '#!/usr/bin/env bash\necho "podman version 4.9.3"\n' > "$STUB/podman"; run env
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "old podman: refused"
assert_contains "$out" "podman 4.9.3" "old podman: message names the version"
printf '#!/usr/bin/env bash\necho "podman version 5.4.2"\n' > "$STUB/podman"
printf '#!/usr/bin/env bash\necho "/dev/sda2 partition 2G 0B -2"\n' > "$STUB/swapon"; run env
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "swap on: refused"
assert_contains "$out" "swapoff" "swap on: message says how to turn it off"
rm -rf "$TMP"; finish
