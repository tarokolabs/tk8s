#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
TMP=$(mktemp -d); export TAROKO_HOME="$TMP"
mkdir -p "$TMP/clusters/existing"
cat > "$TMP/clusters/existing/cluster.yaml" <<'YAML'
metadata: {name: existing}
spec:
  network: {index: 0, nodes: 172.22.0.0/24, pods: 10.244.0.0/21, services: 10.98.0.0/24}
  nodes: []
YAML
cat > "$TMP/in.yaml" <<'YAML'
apiVersion: taroko.io/v1alpha1
kind: Cluster
metadata: {name: demo}
spec:
  nodes:
    - {role: control-plane, cpu: 2, memory: 4G}
    - {role: worker, count: 2, cpu: 2, memory: 4G}
YAML
out=$(task plan:resolve CLUSTER_FILE="$TMP/in.yaml" DRY_RUN=1 2>&1)
assert_contains "$out" "index: 1" "skips index 0 used by existing cluster"
assert_contains "$out" "nodes: 172.22.1.0/24" "node subnet from index"
assert_contains "$out" "pods: 10.244.8.0/21" "pod subnet = 10.244.(8N).0/21"
assert_contains "$out" "services: 10.98.1.0/24" "service subnet from index"
assert_contains "$out" "gateway: 172.22.1.254" "gateway .254"
assert_contains "$out" "lb_range: 172.22.1.200-172.22.1.219" "LB-IPAM range"
assert_contains "$out" "name: demo-control-plane" "first control plane name"
assert_contains "$out" "name: demo-worker2" "workers expanded from count"
assert_contains "$out" "ip: 172.22.1.3" "third node ip"
assert_contains "$out" 'kubernetes: "1.37.0"' "default kubernetes version filled in"
assert_contains "$out" "runtime: crio" "default runtime"
assert_contains "$out" "join: true" "join defaults to true"
# HA
sed -i.bak 's/role: control-plane, cpu: 2/role: control-plane, count: 3, cpu: 2/' "$TMP/in.yaml"
out=$(task plan:resolve CLUSTER_FILE="$TMP/in.yaml" DRY_RUN=1 2>&1)
assert_contains "$out" "vip: 172.22.1.100" "vip when control-planes > 1"
assert_contains "$out" "name: demo-control-plane3" "third control plane name"
# deferred join carried per node
cat > "$TMP/in3.yaml" <<'YAML'
metadata: {name: defer}
spec:
  nodes: [{role: control-plane}, {role: control-plane, count: 2, join: false}]
YAML
out=$(task plan:resolve CLUSTER_FILE="$TMP/in3.yaml" DRY_RUN=1 2>&1)
assert_eq "2" "$(echo "$out" | grep -c "join: false")" "join: false carried per node"
# resume keeps index
mkdir -p "$TMP/clusters/demo"; printf 'metadata: {name: demo}\nspec:\n  network: {index: 7}\n  nodes: []\n' > "$TMP/clusters/demo/cluster.yaml"
out=$(task plan:resolve CLUSTER_FILE="$TMP/in.yaml" DRY_RUN=1 2>&1)
assert_contains "$out" "index: 7" "resume keeps the existing index"
rm -rf "$TMP/clusters/demo"
# overlap
cat > "$TMP/in2.yaml" <<'YAML'
metadata: {name: clash}
spec:
  network: {nodes: 172.22.0.0/24}
  nodes: [{role: control-plane}]
YAML
assert_fails "explicit overlapping subnet is rejected" task plan:resolve CLUSTER_FILE="$TMP/in2.yaml" DRY_RUN=1
out=$(task plan:resolve CLUSTER_FILE="$TMP/in2.yaml" DRY_RUN=1 ALLOW_OVERLAP=1 2>&1)
assert_contains "$out" "nodes: 172.22.0.0/24" "overlap allowed with ALLOW_OVERLAP=1"
rm -rf "$TMP"; finish
