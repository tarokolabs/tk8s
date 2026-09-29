#!/usr/bin/env bash
# The node image is kindest/node with containerd swapped for CRI-O. Everything kind wired to
# containerd must follow: the kubelet's --runtime-cgroups points at containerd.service in
# /etc/default/kubelet, and a kubelet that watches a cgroup that never exists logs an error
# every few seconds.
source "$(dirname "$0")/lib.sh"
cd "$(dirname "$0")/.." || exit 1
f=images/node/Containerfile
assert_contains "$(cat $f)" "systemctl disable containerd.service" "containerd unit disabled"
assert_contains "$(cat $f)" "systemctl enable crio.service" "crio unit enabled"
assert_contains "$(cat $f)" "/system.slice/crio.service" "kubelet --runtime-cgroups follows the runtime"
assert_contains "$(cat $f)" "/etc/default/kubelet" "the rewrite targets kind's KUBELET_EXTRA_ARGS file"
finish
