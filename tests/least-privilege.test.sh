#!/usr/bin/env bash
# Least privilege: sudo appears only in front of the commands that need root (rootful podman, systemd,
# files under /etc and /usr/local/bin, kernel settings, host routes, ownership handover, and removing
# root-written cluster state). Everything under the user-owned TK_DATA_DIR is done as the user.
source "$(dirname "$0")/lib.sh"
cd "$(dirname "$0")/.." || exit 1
files="bin/tkctl install.sh taskfiles/*.yaml"
# Commands root may run. "sudo swapoff" / "sudo without" occur only inside messages to the user.
# shellcheck disable=SC2086
bad=$(grep -noE 'sudo [a-z-]+' $files | grep -vE 'sudo (-n|podman|systemctl|modprobe|sysctl|tee|rm|mkdir|install|ln|ip|tar|chown|swapoff|without)$' || true)
assert_eq "" "$bad" "every sudo is on the allowlist"
# shellcheck disable=SC2086
etc=$(grep -noE 'sudo (tee|mkdir -p|rm -rf|rm -f) [^ ]+' $files | grep -vE 'sudo (tee|mkdir -p|rm -rf|rm -f) (/etc/|"\$1"|\{\{$)' || true)
assert_eq "" "$etc" "sudo tee/mkdir/rm touch only /etc paths (unit files, sysctl.d, modules-load.d) or the cluster state"
# shellcheck disable=SC2086
state=$(grep -noE 'sudo rm -rf \{\{ \.[A-Z_]+ \}\}' $files | grep -vE 'STATE_DIR' || true)
assert_eq "" "$state" "the only templated sudo rm target is the cluster state dir (root-written PV and log data)"
# shellcheck disable=SC2086
tar=$(grep -noE 'sudo tar .*' $files | grep -v -- '-C /usr/local/bin' || true)
assert_eq "" "$tar" "sudo tar extracts only into /usr/local/bin"
# shellcheck disable=SC2086
modes=$(grep -nE 'chmod (-R )?(777|666|o\+w)' $files || true)
assert_eq "" "$modes" "no world-writable modes"
# shellcheck disable=SC2086
own=$(grep -nE 'sudo (mkdir|cp|ls|cat|touch|tee|chmod) [^/]*\{\{ \.(STATE_DIR|CLUSTERS_DIR|CACHE|OUT_DIR|CNI_DIR)' $files || true)
assert_eq "" "$own" "the user-owned data dir is written without sudo"
finish
