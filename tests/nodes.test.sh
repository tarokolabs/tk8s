#!/usr/bin/env bash
source "$(dirname "$0")/lib.sh"
cd "$(dirname "$0")/.." || exit 1
v() { sed -nE "s/^$1: *\"?([^\"]+)\"?.*/\1/p" versions.yaml; }
TMP=$(mktemp -d); export TK_DATA_DIR="$TMP"
mkdir -p "$TMP/clusters/demo"
cat > "$TMP/clusters/demo/cluster.yaml" <<'YAML'
apiVersion: taroko.io/v1alpha1
kind: Cluster
metadata: {name: demo}
spec:
  kubernetes: "1.37.0"
  network: {index: 3, nodes: 172.22.3.0/24, gateway: 172.22.3.254, lb: 172.22.3.100, pods: 10.244.24.0/21, services: 10.98.3.0/24, lb_range: 172.22.3.200-172.22.3.219}
  nodes:
    - {role: control-plane, name: demo-control-plane, ip: 172.22.3.1, cpu: 2, memory: 4g, join: true}
    - {role: worker, name: demo-worker1, ip: 172.22.3.2, cpu: 4, memory: 8g, join: true}
    - {role: control-plane, name: demo-control-plane2, ip: 172.22.3.3, cpu: 2, memory: 4g, join: false}
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
assert_contains "$out" "Volume=$TMP/images/1.37.0:/var/lib/containers/shared:ro" "shared image store of the cluster's Kubernetes version mounted read-only"
assert_contains "$out" "# ---- /etc/systemd/system/demo.target" "target path"
# Host routes to the pod and service subnets live in a unit so they come back after a reboot.
routes=$(echo "$out" | sed -n '/demo-routes.service/,$p')
assert_contains "$out" "# ---- /etc/systemd/system/demo-routes.service" "routes unit path"
assert_contains "$routes" "ExecStart=ip route replace 10.244.24.0/21 via 172.22.3.1" "pod subnet routed via the first control plane"
assert_contains "$routes" "ExecStart=ip route replace 10.98.3.0/24 via 172.22.3.1" "service subnet routed via the first control plane"
assert_contains "$routes" "ExecStop=-ip route del 10.244.24.0/21" "routes removed when the cluster stops"
assert_contains "$routes" "After=demo-control-plane.service" "routes wait for the control plane (its bridge address)"
assert_contains "$routes" "PartOf=demo.target" "routes unit belongs to the cluster target"
assert_contains "$routes" "RemainAfterExit=yes" "oneshot stays active so stop runs ExecStop"
# ---- apiserver LB: the host-side HAProxy at .100 with a static backend per control plane
lbcfg=$(echo "$out" | sed -n '/# ---- .*\/lb\/haproxy.cfg/,/# ---- /p')
assert_contains "$out" "# ---- $TMP/clusters/demo/lb/haproxy.cfg" "LB config path"
assert_eq "2" "$(echo "$lbcfg" | grep -c '^    server ')" "one backend per control plane, joined or not"
assert_contains "$lbcfg" "server demo-control-plane 172.22.3.1:6443" "backend is the control plane's address"
assert_contains "$lbcfg" "server demo-control-plane2 172.22.3.3:6443" "deferred control plane listed too (DOWN until it joins)"
assert_contains "$lbcfg" "default-server check inter 2s fall 3 rise 2 on-marked-down shutdown-sessions check-ssl verify none" "health check over TLS, traffic passthrough, sessions of a DOWN backend shut down"
if [[ "$lbcfg" == *"resolvers"* ]] || [[ "$lbcfg" == *"server-template"* ]]; then echo "FAIL  no DNS discovery in the LB config"; FAILURES=$((FAILURES+1)); else echo "PASS  no DNS discovery in the LB config"; fi
assert_contains "$lbcfg" "bind *:6443" "LB listens on the container address"
assert_contains "$lbcfg" "bind 127.0.0.1:8404" "stats only on loopback"
if [[ "$out" == *"demo-dns.container"* ]] || [[ "$out" == *"/dns/"* ]]; then echo "FAIL  no DNS container or zone (backends are static)"; FAILURES=$((FAILURES+1)); else echo "PASS  no DNS container or zone (backends are static)"; fi
lbu=$(echo "$out" | sed -n '/demo-lb.container/,/# ---- /p')
assert_contains "$lbu" "Image=$(v haproxy)" "LB unit runs the pinned HAProxy"
assert_contains "$lbu" "IP=172.22.3.100" "LB at .100"
assert_contains "$lbu" "Volume=$TMP/clusters/demo/lb/haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro" "LB config mounted where the image reads it"
assert_contains "$lbu" "StopSignal=SIGTERM" "LB stops hard on cluster stop (the image's SIGUSR1 drains for up to 10 min and podman kills it after 10 s)"
if [[ "$lbu" == *"DNS="* ]]; then echo "FAIL  LB unit needs no resolver"; FAILURES=$((FAILURES+1)); else echo "PASS  LB unit needs no resolver"; fi
w_unit=$(echo "$out" | sed -n '/demo-worker1.container/,/# ---- /p')
assert_contains "$w_unit" "AddHost=api.demo.k8s:172.22.3.100" "worker reaches the apiserver through the host-side LB (kubeadm HA model: one LB for everyone)"
if [[ "$w_unit" == *"DNS="* ]] || [[ "$w_unit" == *"After=demo-dns"* ]]; then echo "FAIL  node units no longer depend on a cluster DNS"; FAILURES=$((FAILURES+1)); else echo "PASS  node units no longer depend on a cluster DNS"; fi
cp2_unit=$(echo "$out" | sed -n '/demo-control-plane2.container/,/# ---- /p')
assert_contains "$cp2_unit" "AddHost=api.demo.k8s:172.22.3.100" "control plane reaches the apiserver through the host-side LB too"
assert_contains "$out" "Wants=demo-lb.service demo-control-plane.service demo-worker1.service demo-control-plane2.service demo-routes.service" "target wants the LB, every node and the routes unit"
# A cluster.yaml from before the apiserver LB (no lb) must be refused, not rendered with an empty address.
mkdir -p "$TMP/clusters/old"
sed -e 's/name: demo/name: old/' -e 's/demo-/old-/g' -e 's/ lb: 172.22.3.100,//' "$TMP/clusters/demo/cluster.yaml" > "$TMP/clusters/old/cluster.yaml"
out=$(task nodes:render CLUSTER=old DRY_RUN=true 2>&1); rc=$?
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "render refuses a cluster created before the apiserver LB"
assert_contains "$out" "created before the apiserver LB" "refusal explains why"
if [[ "$out" == *"AddHost="* ]]; then echo "FAIL  nothing rendered for the old cluster"; FAILURES=$((FAILURES+1)); else echo "PASS  nothing rendered for the old cluster"; fi
# nodes:up copies the LB image into the host store before the target starts, and waits for the LB to
# list every control plane as a backend (which proves the LB is up and its config good).
up=$(sed -n '/^  up:/,/^  [a-z-]*:$/p' taskfiles/nodes.yaml)
assert_contains "$up" "images:host K8S=" "nodes:up calls images:host"
if [ "$(echo "$up" | grep -n 'images:host' | head -1 | cut -d: -f1)" -lt "$(echo "$up" | grep -n 'enable --now' | head -1 | cut -d: -f1)" ]; then echo "PASS  images are in the host store before the target starts"; else echo "FAIL  images are in the host store before the target starts"; FAILURES=$((FAILURES+1)); fi
assert_contains "$up" "control-planes," "nodes:up waits until the LB lists the control planes"
if [[ "$up" == *"MAINT (resolution)"* ]]; then echo "FAIL  nodes:up no longer waits on DNS resolution"; FAILURES=$((FAILURES+1)); else echo "PASS  nodes:up no longer waits on DNS resolution"; fi
cp_unit=$(echo "$out" | sed -n '/demo-control-plane.container/,/demo-worker1.container/p')
if [[ "$cp_unit" == *"After=demo-control-plane.service"* ]]; then echo "FAIL  control plane must not wait for itself"; FAILURES=$((FAILURES+1)); else echo "PASS  control plane has no After on itself"; fi
rm -rf "$TMP"; finish
