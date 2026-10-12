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
# Workers run their own apiserver load balancer; it has to be in the image (apt at runtime is lost on restart).
assert_contains "$(cat $f)" "haproxy gettext-base" "Containerfile installs haproxy and envsubst"
assert_contains "$(cat $f)" "systemctl disable haproxy" "the package's own unit stays off"
assert_contains "$(cat $f)" "systemctl enable apiserver-lb.service" "the node-local LB unit is enabled (its condition decides per role)"
u=images/node/systemd/apiserver-lb.service
assert_contains "$(cat $u)" "ConditionPathExists=/etc/tk8s/apiserver-lb.env" "only workers (which get the env file mounted) run it"
assert_contains "$(cat $u)" "EnvironmentFile=/etc/tk8s/apiserver-lb.env" "cluster domain comes from the env file"
assert_contains "$(cat $u)" "envsubst < /etc/haproxy/apiserver-lb.cfg.tmpl > /run/haproxy/apiserver-lb.cfg" "config rendered at start"
assert_contains "$(cat $u)" "Type=notify" "haproxy -Ws notifies systemd"
t=images/node/haproxy/apiserver-lb.cfg.tmpl
assert_contains "$(cat $t)" 'server-template cp 5 _apiserver._tcp.${TK_CLUSTER_DOMAIN}' "SRV name from the cluster domain"
assert_contains "$(cat $t)" "bind 127.0.0.1:6443" "node-local LB listens on loopback only"
assert_contains "$(cat $t)" "bind 127.0.0.1:8404" "stats on loopback"
assert_contains "$(cat images/node/systemd/kubelet.service.d/20-apiserver-lb.conf)" "Wants=apiserver-lb.service" "kubelet pulls the LB in"
assert_contains "$(cat images/node/systemd/kubelet.service.d/20-apiserver-lb.conf)" "After=apiserver-lb.service" "and starts after it"
finish
