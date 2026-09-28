#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
TMP=$(mktemp -d); export TK_DATA_DIR="$TMP"
mkdir -p "$TMP/clusters/demo"
cat > "$TMP/clusters/demo/cluster.yaml" <<'YAML'
metadata: {name: demo}
spec:
  kubernetes: "1.37.0"
  network: {index: 3, nodes: 172.22.3.0/24, gateway: 172.22.3.254, pods: 10.244.24.0/21, services: 10.98.3.0/24, vip: 172.22.3.100}
  nodes:
    - {role: control-plane, name: demo-control-plane, ip: 172.22.3.1, join: true}
    - {role: control-plane, name: demo-control-plane2, ip: 172.22.3.2, join: true}
    - {role: control-plane, name: demo-control-plane3, ip: 172.22.3.3, join: false}
    - {role: worker, name: demo-worker1, ip: 172.22.3.4, join: true}
YAML
out=$(task kubeadm:render CLUSTER=demo DRY_RUN=true 2>&1)
assert_contains "$out" "kubernetesVersion: 1.37.0" "k8s version"
assert_contains "$out" "advertiseAddress: 172.22.3.1" "first control plane ip"
assert_contains "$out" "criSocket: unix:///var/run/crio/crio.sock" "CRI-O socket"
assert_contains "$out" 'controlPlaneEndpoint: "172.22.3.100:6443"' "vip endpoint when HA"
assert_contains "$out" "podSubnet: 10.244.24.0/21" "pod subnet"
assert_contains "$out" "serviceSubnet: 10.98.3.0/24" "service subnet"
assert_contains "$out" "dnsDomain: demo.k8s" "dns domain from name"
assert_contains "$out" "name: demo-control-plane" "node registration name"
sed -i.bak 's/, vip: 172.22.3.100//' "$TMP/clusters/demo/cluster.yaml"
out=$(task kubeadm:render CLUSTER=demo DRY_RUN=true 2>&1)
if [[ "$out" == *controlPlaneEndpoint* ]]; then echo "FAIL  no controlPlaneEndpoint without vip"; FAILURES=$((FAILURES+1)); else echo "PASS  no controlPlaneEndpoint without vip"; fi
# join plan: which nodes join and how (pure computation, no cluster needed); restore the vip first
mv "$TMP/clusters/demo/cluster.yaml.bak" "$TMP/clusters/demo/cluster.yaml"
out=$(task kubeadm:join-plan CLUSTER=demo 2>&1)
assert_contains "$out" "demo-control-plane2 control-plane join kube-vip" "extra control plane joins and gets kube-vip (cluster has a vip)"
assert_contains "$out" "demo-control-plane3 control-plane skip" "join: false is skipped"
assert_contains "$out" "demo-worker1 worker join" "worker joins"
if [[ "$out" == *"demo-control-plane "* ]]; then echo "FAIL  first control plane is not in the join plan"; FAILURES=$((FAILURES+1)); else echo "PASS  first control plane is not in the join plan"; fi
rm -rf "$TMP"; finish
