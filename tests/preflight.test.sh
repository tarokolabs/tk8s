#!/usr/bin/env bash
# preflight:check against stubbed host tools. Regression for: pods on other nodes unreachable when the host
# runs Docker (its FORWARD DROP policy applies to bridged frames once br_netfilter is loaded).
source "$(dirname "$0")/lib.sh"
cd "$(dirname "$0")/.." || exit 1
TMP=$(mktemp -d); export TK_DATA_DIR="$TMP"
STUB="$TMP/bin"; mkdir -p "$STUB" "$TMP/etc/sysctl.d" "$TMP/etc/modules-load.d"; export STUB STUB_LOG="$TMP/calls.log"
printf '#!/usr/bin/env bash\n[ "$1" = -n ] && shift; echo "sudo $*" >> "$STUB_LOG"; exec "$@"\n' > "$STUB/sudo"
printf '#!/usr/bin/env bash\necho "podman version 5.4.2"\n' > "$STUB/podman"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB/swapon"
printf '#!/usr/bin/env bash\ncase "$*" in *%%u*) cat "$STUB/owner";; *%%U*) echo someone;; *) echo cgroup2fs;; esac\n' > "$STUB/stat"   # -fc %T cgroup, -c %u owner
echo 0 > "$STUB/owner"
printf '#!/usr/bin/env bash\necho "br_netfilter 32768 0"\n' > "$STUB/lsmod"
printf '#!/usr/bin/env bash\necho "sysctl $*" >> "$STUB_LOG"; exit 0\n' > "$STUB/sysctl"
printf '#!/usr/bin/env bash\necho "modprobe $*" >> "$STUB_LOG"; exit 0\n' > "$STUB/modprobe"
cat > "$STUB/tee" <<'SH'
#!/usr/bin/env bash
# Redirect writes under /etc into the temp tree so the test can read them back.
f=${@: -1}; case "$f" in /etc/*) f="$TMP_ETC${f#/etc}";; esac; mkdir -p "$(dirname "$f")"; cat > "$f"
SH
chmod +x "$STUB"/*; export TMP_ETC="$TMP/etc"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB/systemctl"   # systemd answers (macOS has no /run/systemd/system)
chmod +x "$STUB/systemctl"
out=$(PATH="$STUB:$PATH" task preflight:check 2>&1); rc=$?
assert_eq "0" "$rc" "preflight passes on a good host"
assert_contains "$(cat "$STUB_LOG")" "sysctl -qw net.bridge.bridge-nf-call-iptables=0 net.bridge.bridge-nf-call-ip6tables=0" "bridged frames on the host stay out of the host firewall (Docker's FORWARD DROP)"
assert_contains "$(cat "$TMP/etc/sysctl.d/90-tk8s.conf" 2>/dev/null)" "net.bridge.bridge-nf-call-iptables = 0" "setting persisted for reboots"
assert_contains "$(cat "$TMP/etc/modules-load.d/tk8s.conf" 2>/dev/null)" "br_netfilter" "br_netfilter loaded at boot so the sysctl applies"
assert_contains "$(cat "$STUB_LOG")" "sudo install -d -o $(id -u) -g $(id -g) $TMP/clusters" "data tree created for the user (clusters)"
assert_contains "$(cat "$STUB_LOG")" "sudo install -d -o $(id -u) -g $(id -g) $TMP/cni" "data tree created for the user (cni)"
if [ -d "$TMP/clusters" ] && [ -w "$TMP/cache" ]; then echo "PASS  data tree exists and is writable"; else echo "FAIL  data tree exists and is writable"; FAILURES=$((FAILURES+1)); fi
assert_contains "$out" "preflight ok" "summary line"
# Ownership handover only from root (an earlier root-run tkctl); another user's directory is refused, not taken.
mkdir -p "$TMP/clusters"; chmod 555 "$TMP/clusters"; echo 0 > "$STUB/owner"; : > "$STUB_LOG"
out=$(PATH="$STUB:$PATH" task preflight:check 2>&1); rc=$?
assert_eq "0" "$rc" "root-owned dir: taken over"
assert_contains "$(cat "$STUB_LOG")" "sudo chown $(id -u):$(id -g) $TMP/clusters" "root-owned dir is chowned to the user"
echo 999 > "$STUB/owner"; : > "$STUB_LOG"
out=$(PATH="$STUB:$PATH" task preflight:check 2>&1); rc=$?
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "another user's dir: refused"
assert_contains "$out" "owned by someone" "refusal names the owner"
if grep -q "sudo chown" "$STUB_LOG"; then echo "FAIL  another user's dir is not chowned"; FAILURES=$((FAILURES+1)); else echo "PASS  another user's dir is not chowned"; fi
chmod 755 "$TMP/clusters"
rm -rf "$TMP"; finish
