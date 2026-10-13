#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
TMP=$(mktemp -d); export TK_DATA_DIR="$TMP"
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
out=$(task plan:resolve CLUSTER_FILE="$TMP/in.yaml" DRY_RUN=true 2>&1)
assert_contains "$out" "index: 1" "skips index 0 used by existing cluster"
assert_contains "$out" "nodes: 172.22.1.0/24" "node subnet from index"
assert_contains "$out" "pods: 10.244.8.0/21" "pod subnet = 10.244.(8N).0/21"
assert_contains "$out" "services: 10.98.1.0/24" "service subnet from index"
assert_contains "$out" "gateway: 172.22.1.254" "gateway .254"
assert_contains "$out" "lb_range: 172.22.1.200-172.22.1.219" "LB-IPAM range"
assert_contains "$out" "lb: 172.22.1.100" "apiserver LB address (single control plane too)"
assert_contains "$out" "name: demo-control-plane" "first control plane name"
assert_contains "$out" "name: demo-worker2" "workers expanded from count"
assert_contains "$out" "ip: 172.22.1.3" "third node ip"
assert_contains "$out" 'kubernetes: "1.37.0"' "default kubernetes version filled in"
if [[ "$out" == *"runtime:"* ]]; then echo "FAIL  no runtime field (CRI-O only)"; FAILURES=$((FAILURES+1)); else echo "PASS  no runtime field (CRI-O only)"; fi
assert_contains "$out" "join: true" "join defaults to true"
# HA
sed -i.bak 's/role: control-plane, cpu: 2/role: control-plane, count: 3, cpu: 2/' "$TMP/in.yaml"
out=$(task plan:resolve CLUSTER_FILE="$TMP/in.yaml" DRY_RUN=true 2>&1)
assert_contains "$out" "lb: 172.22.1.100" "apiserver LB address (HA too)"
if [[ "$out" == *"vip:"* ]] || [[ "$out" == *"dns:"* ]]; then echo "FAIL  no vip field (kube-vip is gone)"; FAILURES=$((FAILURES+1)); else echo "PASS  no vip field (kube-vip is gone)"; fi
assert_contains "$out" "name: demo-control-plane3" "third control plane name"
# deferred join carried per node
cat > "$TMP/in3.yaml" <<'YAML'
metadata: {name: defer}
spec:
  nodes: [{role: control-plane}, {role: control-plane, count: 2, join: false}]
YAML
out=$(task plan:resolve CLUSTER_FILE="$TMP/in3.yaml" DRY_RUN=true 2>&1)
assert_eq "2" "$(echo "$out" | grep -c "join: false")" "join: false carried per node"
# resume keeps index
mkdir -p "$TMP/clusters/demo"; printf 'metadata: {name: demo}\nspec:\n  datapath: auto\n  datapath_resolved: netkit\n  network: {index: 7}\n  nodes: []\n' > "$TMP/clusters/demo/cluster.yaml"
out=$(task plan:resolve CLUSTER_FILE="$TMP/in.yaml" DRY_RUN=true 2>&1)
assert_contains "$out" "index: 7" "resume keeps the existing index"
assert_contains "$out" "datapath_resolved: netkit" "resume keeps the resolved datapath recorded by cni:install"
rm -rf "$TMP/clusters/demo"
# overlap
cat > "$TMP/in2.yaml" <<'YAML'
metadata: {name: clash}
spec:
  network: {nodes: 172.22.0.0/24}
  nodes: [{role: control-plane}]
YAML
assert_fails "explicit overlapping subnet is rejected" task plan:resolve CLUSTER_FILE="$TMP/in2.yaml" DRY_RUN=true
out=$(task plan:resolve CLUSTER_FILE="$TMP/in2.yaml" DRY_RUN=true ALLOW_OVERLAP=true 2>&1)
assert_contains "$out" "nodes: 172.22.0.0/24" "overlap allowed with ALLOW_OVERLAP=true"
# -f input is validated with the same rules bin/tkctl applies to flags.
bad() {  # label spec-body expected-message
  printf 'metadata: {name: bad}\nspec:\n%b\n' "$2" > "$TMP/bad.yaml"
  out=$(task plan:resolve CLUSTER_FILE="$TMP/bad.yaml" DRY_RUN=true 2>&1); rc=$?
  assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "$1 is rejected"
  assert_contains "$out" "$3" "$1 message"
}
bad "memory 4Gi" '  nodes: [{role: control-plane}, {role: worker, memory: 4Gi}]' "4096M"
bad "non-integer cpu" '  nodes: [{role: control-plane}, {role: worker, cpu: two}]' "cpu"
bad "invalid node name" '  nodes: [{role: control-plane}, {role: worker, name: Big_Node}]' "Big_Node"
bad "name with count > 1" '  nodes: [{role: control-plane}, {role: worker, name: bad-big, count: 2}]' "count"
bad "duplicate explicit name" '  nodes: [{role: control-plane}, {role: worker, name: bad-x}, {role: worker, name: bad-x}]' "bad-x"
bad "unknown role" '  nodes: [{role: control-plane}, {role: master}]' "role"
bad "no control plane" '  nodes: [{role: worker, count: 2}]' "control-plane"
bad "worker listed first" '  nodes: [{role: worker}, {role: control-plane}]' "first"
bad "even control-plane count" '  nodes: [{role: control-plane, count: 2}]' "odd"
bad "gvisor with netkit" '  gvisor: true\n  datapath: netkit\n  nodes: [{role: control-plane}]' "netkit"
bad "runtime key from a v2 pre-release file" '  runtime: crio\n  nodes: [{role: control-plane}]' "CRI-O"
printf 'metadata: {name: gv}\nspec:\n  gvisor: true\n  nodes: [{role: control-plane}]\n' > "$TMP/gv.yaml"
out=$(task plan:resolve CLUSTER_FILE="$TMP/gv.yaml" DRY_RUN=true 2>&1)
assert_contains "$out" "datapath: veth" "gvisor without an explicit datapath resolves to veth (sandboxes have no network under netkit)"
printf 'metadata: {name: good}\nspec:\n  nodes: [{role: control-plane, memory: 4096M}, {role: worker, name: good-big, cpu: 8}]\n' > "$TMP/good.yaml"
out=$(task plan:resolve CLUSTER_FILE="$TMP/good.yaml" DRY_RUN=true 2>&1); rc=$?
assert_eq "0" "$rc" "valid -f input passes validation"
assert_contains "$out" "memory: 4096m" "explicit memory lower-cased"
# Every cluster has an apiserver LB and a DNS; a file from the kube-vip era is refused, not converted.
cat > "$TMP/old.yaml" <<'YAML'
metadata: {name: old}
spec:
  network: {nodes: 172.22.7.0/24, vip: 172.22.7.100}
  nodes: [{role: control-plane}]
YAML
out=$(task plan:resolve CLUSTER_FILE="$TMP/old.yaml" DRY_RUN=true 2>&1); rc=$?
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "spec.network.vip is refused"
assert_contains "$out" "spec.network.vip is no longer supported" "vip refusal names the field"
# -f may pin dns/lb; omitted ones are derived from the node subnet.
cat > "$TMP/pin.yaml" <<'YAML'
metadata: {name: pin}
spec:
  network: {nodes: 172.22.9.0/24, lb: 172.22.9.240}
  nodes: [{role: control-plane}]
YAML
out=$(task plan:resolve CLUSTER_FILE="$TMP/pin.yaml" DRY_RUN=true 2>&1)
assert_contains "$out" "lb: 172.22.9.240" "explicit lb address kept"
# Custom addresses live in .220-.253 (nodes take .1-.199, the LB-IPAM pool .200-.219, the gateway .254).
cat > "$TMP/clash.yaml" <<'YAML'
metadata: {name: clash}
spec:
  network: {nodes: 172.22.9.0/24, lb: 172.22.9.99}
  nodes: [{role: control-plane}]
YAML
out=$(task plan:resolve CLUSTER_FILE="$TMP/clash.yaml" DRY_RUN=true 2>&1); rc=$?
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "an lb address inside the node range is refused"
assert_contains "$out" "220" "refusal names the allowed range"
# Node allocation never hands out the LB's default address.
cat > "$TMP/big.yaml" <<'YAML'
metadata: {name: big}
spec:
  nodes: [{role: control-plane}, {role: worker, count: 100}]
YAML
out=$(task plan:resolve CLUSTER_FILE="$TMP/big.yaml" DRY_RUN=true 2>&1)
if [[ "$out" == *"ip: 172.22.1.100"* ]]; then echo "FAIL  node allocation skips the LB address .100"; FAILURES=$((FAILURES+1)); else echo "PASS  node allocation skips the LB address .100"; fi
assert_contains "$out" "ip: 172.22.1.101" "the 100th node takes .101"
# HAProxy's server-template has five slots.
cat > "$TMP/seven.yaml" <<'YAML'
metadata: {name: seven}
spec:
  nodes: [{role: control-plane, count: 7}]
YAML
out=$(task plan:resolve CLUSTER_FILE="$TMP/seven.yaml" DRY_RUN=true 2>&1); rc=$?
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "seven control planes are refused"
assert_contains "$out" "at most 5" "control plane limit named"
rm -rf "$TMP"; finish
