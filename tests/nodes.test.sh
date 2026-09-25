#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
TMP=$(mktemp -d); export TK_DATA_DIR="$TMP"
mkdir -p "$TMP/clusters/demo"
cat > "$TMP/clusters/demo/cluster.yaml" <<'YAML'
apiVersion: taroko.io/v1alpha1
kind: Cluster
metadata: {name: demo}
spec:
  kubernetes: "1.37.0"
  network: {index: 3, nodes: 172.22.3.0/24, gateway: 172.22.3.254, pods: 10.244.24.0/21, services: 10.98.3.0/24, lb_range: 172.22.3.200-172.22.3.219}
  nodes:
    - {role: control-plane, name: demo-control-plane, ip: 172.22.3.1, cpu: 2, memory: 4g, join: true}
    - {role: worker, name: demo-worker1, ip: 172.22.3.2, cpu: 4, memory: 8g, join: true}
YAML
out=$(task nodes:render CLUSTER=demo DRY_RUN=true 2>&1)
assert_contains "$out" "# ---- /etc/containers/systemd/demo/demo.network" "network unit path"
assert_contains "$out" "Subnet=172.22.3.0/24" "network subnet"
assert_contains "$out" "Gateway=172.22.3.254" "network gateway"
assert_contains "$out" "# ---- /etc/containers/systemd/demo/demo-worker1.container" "worker unit path"
assert_contains "$out" "Image=ghcr.io/tarokolabs/tk8s/node:v1.37.0" "single node image, tagged by K8s version"
assert_contains "$out" "IP=172.22.3.2" "worker ip"
assert_contains "$out" "PodmanArgs=--privileged --cgroupns=private --cpus 4 --memory 8g" "per-node resources"
assert_contains "$out" "After=demo-control-plane.service" "worker waits for control plane"
assert_contains "$out" "PartOf=demo.target" "units belong to the cluster target"
assert_contains "$out" "WantedBy=multi-user.target" "autostart on boot"
assert_contains "$out" "SuccessExitStatus=130 143" "kind stop exit codes are success"
assert_contains "$out" "Volume=demo-worker1-var:/var:suid,exec,dev,rbind" "per-node /var volume"
assert_contains "$out" "Volume=demo-worker1-etc:/etc" "per-node /etc volume (kubeadm certs and configs survive restarts)"
assert_contains "$out" "Volume=demo-worker1-usr-local-bin:/usr/local/bin" "per-node /usr/local/bin volume (installed runtimes survive restarts)"
assert_contains "$out" "Volume=$TMP/clusters/demo:/opt/taroko" "state dir mounted as /opt/taroko"
assert_contains "$out" "# ---- /etc/systemd/system/demo.target" "target path"
assert_contains "$out" "Wants=demo-control-plane.service demo-worker1.service demo-routes.service" "target wants every node and the routes unit"
# Host routes to the pod and service subnets live in a unit so they come back after a reboot.
routes=$(echo "$out" | sed -n '/demo-routes.service/,$p')
assert_contains "$out" "# ---- /etc/systemd/system/demo-routes.service" "routes unit path"
assert_contains "$routes" "ExecStart=ip route replace 10.244.24.0/21 via 172.22.3.1" "pod subnet routed via the first control plane"
assert_contains "$routes" "ExecStart=ip route replace 10.98.3.0/24 via 172.22.3.1" "service subnet routed via the first control plane"
assert_contains "$routes" "ExecStop=-ip route del 10.244.24.0/21" "routes removed when the cluster stops"
assert_contains "$routes" "After=demo-control-plane.service" "routes wait for the control plane (its bridge address)"
assert_contains "$routes" "PartOf=demo.target" "routes unit belongs to the cluster target"
assert_contains "$routes" "RemainAfterExit=yes" "oneshot stays active so stop runs ExecStop"
cp_unit=$(echo "$out" | sed -n '/demo-control-plane.container/,/demo-worker1.container/p')
if [[ "$cp_unit" == *"After=demo-control-plane.service"* ]]; then echo "FAIL  control plane must not wait for itself"; FAILURES=$((FAILURES+1)); else echo "PASS  control plane has no After on itself"; fi
rm -rf "$TMP"; finish
