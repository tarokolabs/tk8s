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
# CRI-O's built-in sandbox image (pause:3.10.1 in CRI-O 1.36) and kubeadm's listed pause (3.10.2 for
# 1.36.4) drift apart; the store only holds kubeadm's, so an offline create cannot start any sandbox
# unless CRI-O is told to use kubeadm's. L3 run 38252624650 (1.36.4) failed exactly there.
assert_contains "$(cat $f)" "kubeadm config images list" "the image build reads the pause image from kubeadm"
assert_contains "$(cat $f)" 'pause_image = ' "CRI-O's pause_image is set to kubeadm's pause in a crio.conf.d drop-in"
# The apiserver LB is a host-side container (kubeadm HA model: one LB in front of the control planes);
# the node image carries no proxy of its own.
assert_eq "" "$(grep -n 'haproxy\|apiserver-lb' $f || true)" "node image has no haproxy or apiserver-lb"
if [ -e images/node/systemd ] || [ -e images/node/haproxy ]; then echo "FAIL  no node-local LB files in images/node"; FAILURES=$((FAILURES+1)); else echo "PASS  no node-local LB files in images/node"; fi
finish
