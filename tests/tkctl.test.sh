#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
T="$(dirname "$0")/../bin/tkctl"
TMP=$(mktemp -d); export TAROKO_HOME="$TMP"
out=$($T create cluster --dry-run 2>&1); rc=$?
assert_eq "0" "$rc" "dry-run exits 0"
if [[ "$out" == *"unbound variable"* ]]; then echo "FAIL  dry-run has no shell errors"; FAILURES=$((FAILURES+1)); else echo "PASS  dry-run has no shell errors"; fi
assert_contains "$out" "name: tk8s" "default cluster name"
if [[ "$out" == *"task: ["* ]]; then echo "FAIL  tkctl output does not echo task commands"; FAILURES=$((FAILURES+1)); else echo "PASS  tkctl output does not echo task commands"; fi
assert_contains "$out" "name: tk8s-worker2" "default 1 control-plane + 2 workers"
assert_contains "$out" "cpu: 2" "default cpu"
assert_contains "$out" "memory: 4g" "default memory, lower-cased"
assert_contains "$out" "# ---- /etc/containers/systemd/tk8s/tk8s.network" "dry-run prints the network unit"
assert_contains "$out" "# ---- /etc/containers/systemd/tk8s/tk8s-worker2.container" "dry-run prints node units"
assert_contains "$out" "kubernetesVersion: 1.37.0" "dry-run prints the kubeadm config"
if [ -d "$TMP/clusters/tk8s" ]; then echo "FAIL  dry-run leaves no state directory"; FAILURES=$((FAILURES+1)); else echo "PASS  dry-run leaves no state directory"; fi
mkdir -p "$TMP/clusters/taken"; touch "$TMP/clusters/taken/.create-complete"; printf 'metadata: {name: taken}\nspec: {network: {index: 5}, nodes: []}\n' > "$TMP/clusters/taken/cluster.yaml"
out=$($T create cluster taken --dry-run 2>&1); rc=$?
assert_eq "1" "$rc" "existing cluster is refused"
assert_contains "$out" "already exists" "existing cluster message"
out=$($T create cluster demo --control-planes 3 --workers 4 --cpu 4 --memory 8G --k8s 1.36.4 --dry-run 2>&1)
assert_contains "$out" "name: demo-control-plane3" "control-planes flag"
assert_contains "$out" "name: demo-worker4" "workers flag"
assert_contains "$out" 'kubernetes: "1.36.4"' "k8s flag"
assert_contains "$out" "vip: 172.22." "HA gets a vip"
out=$($T create cluster demo --control-planes 3 --defer-join --dry-run 2>&1)
assert_eq "2" "$(echo "$out" | grep -c "join: false")" "--defer-join marks the extra control planes"
assert_fails "--defer-join without extra control planes is rejected" $T create cluster demo --defer-join --dry-run
out=$($T create cluster demo --gvisor --dry-run 2>&1)
assert_contains "$out" "gvisor: true" "gvisor flag"
assert_contains "$out" "datapath: veth" "gvisor implies veth when datapath unset"
assert_fails "gvisor with netkit is rejected" $T create cluster demo --gvisor --datapath netkit --dry-run
assert_fails "invalid name (uppercase)" $T create cluster Demo --dry-run
assert_fails "invalid name (dot)" $T create cluster tk8s.k8s --dry-run
assert_fails "invalid name (too long)" $T create cluster abcdefghijklmnopq --dry-run
assert_fails "invalid memory (Gi)" $T create cluster demo --memory 4Gi --dry-run
assert_fails "invalid memory (no unit)" $T create cluster demo --memory 4096 --dry-run
out=$($T create cluster demo --memory 4096M --dry-run 2>&1); assert_contains "$out" "memory: 4096m" "memory 4096M accepted"
assert_fails "control-planes 0 is rejected" $T create cluster demo --control-planes 0 --dry-run
assert_fails "control-planes 2 (even) is rejected" $T create cluster demo --control-planes 2 --dry-run
assert_fails "unknown verb" $T frobnicate cluster demo
assert_fails "unknown flag" $T create cluster demo --colour red --dry-run
out=$($T --help 2>&1); assert_contains "$out" "create cluster" "help lists create"
assert_contains "$out" "add node" "help lists add node"
cat > "$TMP/c.yaml" <<'YAML'
metadata: {name: fromfile}
spec:
  nodes: [{role: control-plane, cpu: 4, memory: 4G}, {role: worker, name: fromfile-big, cpu: 8, memory: 16G}]
YAML
out=$($T create cluster -f "$TMP/c.yaml" --dry-run 2>&1)
assert_contains "$out" "name: fromfile-big" "per-node name from -f"
assert_contains "$out" "memory: 16g" "per-node memory from -f"
assert_fails "-f combined with topology flags is rejected" $T create cluster -f "$TMP/c.yaml" --workers 3 --dry-run
out=$($T version 2>&1); assert_contains "$out" "kubernetes default 1.37.0" "version shows default kubernetes"
rm -rf "$TMP"; finish
