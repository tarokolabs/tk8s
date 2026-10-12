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
  network: {index: 3, nodes: 172.22.3.0/24, gateway: 172.22.3.254, dns: 172.22.3.253, lb: 172.22.3.100, pods: 10.244.24.0/21, services: 10.98.3.0/24, lb_range: 172.22.3.200-172.22.3.219}
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
# ---- apiserver LB: cluster DNS (SRV for every control plane) and the host-side HAProxy at .100
zone=$(echo "$out" | sed -n '/# ---- .*\/dns\/db.demo.k8s/,/# ---- /p')
assert_contains "$out" "# ---- $TMP/clusters/demo/dns/db.demo.k8s" "zone file path"
assert_contains "$zone" '$ORIGIN demo.k8s.' "zone origin"
assert_eq "2" "$(echo "$zone" | grep -c '_apiserver._tcp *IN SRV')" "one SRV per control plane, joined or not"
assert_contains "$zone" "IN SRV   0 100 6443 demo-control-plane2.demo.k8s." "SRV target is the node name"
assert_eq "3" "$(echo "$zone" | grep -cE '^demo-[a-z0-9-]+ +IN A ')" "one A per node"
assert_contains "$zone" "api                  IN A     172.22.3.100" "api name points at the LB"
assert_contains "$zone" "dns                  IN A     172.22.3.253" "dns name points at itself"
assert_contains "$zone" "( 1 10 10 60 10 )" "dry-run uses a fixed SOA serial"
corefile=$(echo "$out" | sed -n '/# ---- .*\/dns\/Corefile/,/# ---- /p')
assert_contains "$corefile" "file /etc/coredns/db.demo.k8s" "Corefile serves the zone"
assert_contains "$corefile" "reload 10s" "zone reloads on serial change"
assert_contains "$corefile" "forward . 172.22.3.254" "everything else goes to aardvark"
lbcfg=$(echo "$out" | sed -n '/# ---- .*\/lb\/haproxy.cfg/,/# ---- /p')
assert_contains "$lbcfg" "server-template cp 5 _apiserver._tcp.demo.k8s" "LB discovers control planes through SRV"
assert_contains "$lbcfg" "check-ssl verify none init-addr none resolvers dns" "health check over TLS, traffic passthrough, starts without DNS"
assert_contains "$lbcfg" "hold obsolete 120s" "resolver outage tolerated for two minutes"
assert_contains "$lbcfg" "bind *:6443" "LB listens on the container address"
assert_contains "$lbcfg" "bind 127.0.0.1:8404" "stats only on loopback"
dnsu=$(echo "$out" | sed -n '/demo-dns.container/,/# ---- /p')
assert_contains "$dnsu" "Image=$(v coredns)" "DNS unit runs the pinned CoreDNS"
assert_contains "$dnsu" "IP=172.22.3.253" "DNS at .253"
assert_contains "$dnsu" "Volume=$TMP/clusters/demo/dns:/etc/coredns:ro" "zone and Corefile mounted read-only"
assert_contains "$dnsu" "Exec=-conf /etc/coredns/Corefile" "CoreDNS reads the Corefile"
assert_contains "$dnsu" "PartOf=demo.target" "DNS unit belongs to the target"
lbu=$(echo "$out" | sed -n '/demo-lb.container/,/# ---- /p')
assert_contains "$lbu" "Image=$(v haproxy)" "LB unit runs the pinned HAProxy"
assert_contains "$lbu" "IP=172.22.3.100" "LB at .100"
assert_contains "$lbu" "DNS=172.22.3.253" "LB resolves through the cluster DNS"
assert_contains "$lbu" "Volume=$TMP/clusters/demo/lb/haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro" "LB config mounted where the image reads it"
assert_contains "$lbu" "After=demo-dns.service" "LB starts after DNS"
w_unit=$(echo "$out" | sed -n '/demo-worker1.container/,/# ---- /p')
assert_contains "$w_unit" "DNS=172.22.3.253" "worker resolves through the cluster DNS"
assert_contains "$w_unit" "AddHost=api.demo.k8s:172.22.3.100" "worker reaches the apiserver through the host-side LB (kubeadm HA model: one LB for everyone)"
if [[ "$w_unit" == *"apiserver-lb"* ]]; then echo "FAIL  worker has no node-local proxy"; FAILURES=$((FAILURES+1)); else echo "PASS  worker has no node-local proxy"; fi
assert_contains "$w_unit" "After=demo-dns.service" "worker starts after DNS"
cp2_unit=$(echo "$out" | sed -n '/demo-control-plane2.container/,/# ---- /p')
assert_contains "$cp2_unit" "AddHost=api.demo.k8s:172.22.3.100" "control plane reaches the apiserver through the host-side LB too"
if [[ "$out" == *"/nodes/"*".env"* ]]; then echo "FAIL  no per-node env files (nothing in the node is role-specific any more)"; FAILURES=$((FAILURES+1)); else echo "PASS  no per-node env files (nothing in the node is role-specific any more)"; fi
assert_contains "$out" "Wants=demo-dns.service demo-lb.service demo-control-plane.service demo-worker1.service demo-control-plane2.service demo-routes.service" "target wants DNS, LB, every node and the routes unit"
# nodes:up copies the DNS and LB images into the host store before the target starts, and waits for the LB
# to have resolved every control plane (which proves DNS, zone and LB config at once).
up=$(sed -n '/^  up:/,/^  [a-z-]*:$/p' taskfiles/nodes.yaml)
assert_contains "$up" "images:host K8S=" "nodes:up calls images:host"
if [ "$(echo "$up" | grep -n 'images:host' | head -1 | cut -d: -f1)" -lt "$(echo "$up" | grep -n 'enable --now' | head -1 | cut -d: -f1)" ]; then echo "PASS  images are in the host store before the target starts"; else echo "FAIL  images are in the host store before the target starts"; FAILURES=$((FAILURES+1)); fi
assert_contains "$up" "MAINT (resolution)" "nodes:up waits until the LB has resolved the control planes"
cp_unit=$(echo "$out" | sed -n '/demo-control-plane.container/,/demo-worker1.container/p')
if [[ "$cp_unit" == *"After=demo-control-plane.service"* ]]; then echo "FAIL  control plane must not wait for itself"; FAILURES=$((FAILURES+1)); else echo "PASS  control plane has no After on itself"; fi
rm -rf "$TMP"; finish
