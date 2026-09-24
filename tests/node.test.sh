#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
TMP=$(mktemp -d); export TK_DATA_DIR="$TMP"
mkdir -p "$TMP/clusters/demo"; touch "$TMP/clusters/demo/.create-complete"
cat > "$TMP/clusters/demo/cluster.yaml" <<'YAML'
apiVersion: taroko.io/v1alpha1
kind: Cluster
metadata:
  name: demo
spec:
  kubernetes: "1.37.0"
  runtime: crio
  network:
    index: 3
    nodes: 172.22.3.0/24
    gateway: 172.22.3.254
    lb_range: 172.22.3.200-172.22.3.219
    pods: 10.244.24.0/21
    services: 10.98.3.0/24
  nodes:
    - role: control-plane
      name: demo-control-plane
      ip: 172.22.3.1
      cpu: 2
      memory: 4g
      join: true
    - role: worker
      name: demo-worker1
      ip: 172.22.3.2
      cpu: 2
      memory: 4g
      join: true
YAML
out=$(task node:plan-add CLUSTER=demo ROLE=worker 2>&1)
assert_contains "$out" "name=demo-worker2" "next worker name"
assert_contains "$out" "ip=172.22.3.3" "next ip"
assert_contains "$out" "cpu=2 memory=4g" "inherits resources from the first node of the same role"
out=$(task node:plan-add CLUSTER=demo ROLE=control-plane CPU=4 MEMORY=8g 2>&1); rc=$?
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "control plane cannot be added to a cluster without a vip"
assert_contains "$out" "controlPlaneEndpoint" "no-vip message explains why"
sed -i.bak 's#    services: 10.98.3.0/24#    services: 10.98.3.0/24\n    vip: 172.22.3.100#' "$TMP/clusters/demo/cluster.yaml"
out=$(task node:plan-add CLUSTER=demo ROLE=control-plane CPU=4 MEMORY=8g 2>&1)
assert_contains "$out" "name=demo-control-plane2" "second control plane name (cluster with vip)"
assert_contains "$out" "cpu=4 memory=8g" "explicit resources"
task node:append CLUSTER=demo ROLE=worker NODE=demo-worker2 IP=172.22.3.3 CPU=2 MEMORY=4g JOIN=false >/dev/null
assert_eq "3" "$(grep -c 'role:' "$TMP/clusters/demo/cluster.yaml")" "node appended to cluster.yaml"
assert_contains "$(cat "$TMP/clusters/demo/cluster.yaml")" "join: false" "join flag written"
out=$(task node:plan-add CLUSTER=demo ROLE=worker 2>&1)
assert_contains "$out" "name=demo-worker3 ip=172.22.3.4" "plan-add sees the appended node"
task node:mark-joined CLUSTER=demo NODE=demo-worker2 >/dev/null
assert_eq "0" "$(grep -c 'join: false' "$TMP/clusters/demo/cluster.yaml")" "mark-joined flips join to true"
task node:remove-entry CLUSTER=demo NODE=demo-worker2 >/dev/null
assert_eq "2" "$(grep -c 'role:' "$TMP/clusters/demo/cluster.yaml")" "node entry removed"
assert_contains "$(cat "$TMP/clusters/demo/cluster.yaml")" "name: demo-worker1" "other nodes survive removal"
assert_contains "$(cat "$TMP/clusters/demo/cluster.yaml")" "lb_range: 172.22.3.200-172.22.3.219" "network block survives removal"
# A removed node must not have its name reused while a higher-numbered node still exists.
task node:append CLUSTER=demo ROLE=worker NODE=demo-worker2 IP=172.22.3.3 CPU=2 MEMORY=4g JOIN=true >/dev/null
task node:append CLUSTER=demo ROLE=worker NODE=demo-worker3 IP=172.22.3.4 CPU=2 MEMORY=4g JOIN=true >/dev/null
task node:remove-entry CLUSTER=demo NODE=demo-worker2 >/dev/null
out=$(task node:plan-add CLUSTER=demo ROLE=worker 2>&1)
assert_contains "$out" "name=demo-worker4 ip=172.22.3.5" "plan-add names past the highest existing suffix, not by count"
task node:append CLUSTER=demo ROLE=control-plane NODE=demo-control-plane3 IP=172.22.3.6 CPU=2 MEMORY=4g JOIN=true >/dev/null
out=$(task node:plan-add CLUSTER=demo ROLE=control-plane 2>&1)
assert_contains "$out" "name=demo-control-plane4" "control-plane name past the highest existing suffix"
task node:remove-entry CLUSTER=demo NODE=demo-control-plane3 >/dev/null; task node:remove-entry CLUSTER=demo NODE=demo-worker3 >/dev/null
out=$(task node:remove-entry CLUSTER=demo NODE=demo-control-plane 2>&1); rc=$?
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "cannot remove the first control plane"
assert_contains "$out" "cannot be deleted" "first control plane message"
out=$(task node:remove-entry CLUSTER=demo NODE=demo-nope 2>&1); assert_contains "$out" "not found" "remove unknown node message"
rm -rf "$TMP"; finish
