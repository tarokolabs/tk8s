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
# The shared store: nodes mount it where CRI-O's storage.conf lists an additional image store. The
# mount point must exist in the image (containers/storage stats it; empty is fine, missing is not).
s=images/node/storage.conf
assert_contains "$(cat $s)" '"/var/lib/containers/shared"' "storage.conf lists /var/lib/containers/shared as an additional image store"
assert_contains "$(cat $f)" "mkdir -p /var/lib/containers/shared" "the mount point is created in the image"
assert_eq "" "$(grep -n '/var/lib/shared' $s $f || true)" "the v1 mount point /var/lib/shared is gone"
assert_contains "$(cat taskfiles/nodes.yaml)" ":/var/lib/containers/shared:ro" "nodes mount the store at the path CRI-O looks at"
finish
