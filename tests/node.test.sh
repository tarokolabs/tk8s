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
  kubernetes: "1.37.1"
  network:
    index: 3
    nodes: 172.22.3.0/24
    gateway: 172.22.3.254
    lb_range: 172.22.3.200-172.22.3.219
    pods: 10.244.24.0/21
    services: 10.98.3.0/24
  nodes:
    - role: control-plane
      name: demo-cp-1
      ip: 172.22.3.1
      cpu: 2
      memory: 4g
      join: true
    - role: worker
      name: demo-worker-1
      ip: 172.22.3.2
      cpu: 2
      memory: 4g
      join: true
YAML
out=$(task node:plan-add CLUSTER=demo ROLE=worker 2>&1)
assert_contains "$out" "name=demo-worker-2" "next worker name"
assert_contains "$out" "ip=172.22.3.3" "next ip"
assert_contains "$out" "cpu=2 memory=4g" "inherits resources from the first node of the same role"
# Every cluster has controlPlaneEndpoint (api.<cluster>.k8s via the LB), so a control plane can be added to any of them.
out=$(task node:plan-add CLUSTER=demo ROLE=control-plane CPU=4 MEMORY=8g 2>&1); rc=$?
assert_eq "0" "$rc" "control plane can be added to a single-cp-1 cluster"
assert_contains "$out" "name=demo-cp-2" "second control plane name"
assert_contains "$out" "cpu=4 memory=8g" "explicit resources"
task node:append CLUSTER=demo ROLE=worker NODE=demo-worker-2 IP=172.22.3.3 CPU=2 MEMORY=4g JOIN=false >/dev/null
assert_eq "3" "$(grep -c 'role:' "$TMP/clusters/demo/cluster.yaml")" "node appended to cluster.yaml"
assert_contains "$(cat "$TMP/clusters/demo/cluster.yaml")" "join: false" "join flag written"
out=$(task node:plan-add CLUSTER=demo ROLE=worker 2>&1)
assert_contains "$out" "name=demo-worker-3 ip=172.22.3.4" "plan-add sees the appended node"
task node:mark-joined CLUSTER=demo NODE=demo-worker-2 >/dev/null
assert_eq "0" "$(grep -c 'join: false' "$TMP/clusters/demo/cluster.yaml")" "mark-joined flips join to true"
task node:remove-entry CLUSTER=demo NODE=demo-worker-2 >/dev/null
assert_eq "2" "$(grep -c 'role:' "$TMP/clusters/demo/cluster.yaml")" "node entry removed"
assert_contains "$(cat "$TMP/clusters/demo/cluster.yaml")" "name: demo-worker-1" "other nodes survive removal"
assert_contains "$(cat "$TMP/clusters/demo/cluster.yaml")" "lb_range: 172.22.3.200-172.22.3.219" "network block survives removal"
# A removed node must not have its name reused while a higher-numbered node still exists.
task node:append CLUSTER=demo ROLE=worker NODE=demo-worker-2 IP=172.22.3.3 CPU=2 MEMORY=4g JOIN=true >/dev/null
task node:append CLUSTER=demo ROLE=worker NODE=demo-worker-3 IP=172.22.3.4 CPU=2 MEMORY=4g JOIN=true >/dev/null
task node:remove-entry CLUSTER=demo NODE=demo-worker-2 >/dev/null
out=$(task node:plan-add CLUSTER=demo ROLE=worker 2>&1)
assert_contains "$out" "name=demo-worker-4 ip=172.22.3.5" "plan-add names past the highest existing suffix, not by count"
task node:append CLUSTER=demo ROLE=control-plane NODE=demo-cp-3 IP=172.22.3.6 CPU=2 MEMORY=4g JOIN=true >/dev/null
out=$(task node:plan-add CLUSTER=demo ROLE=control-plane 2>&1)
assert_contains "$out" "name=demo-cp-4" "control-plane name past the highest existing suffix"
task node:remove-entry CLUSTER=demo NODE=demo-cp-3 >/dev/null; task node:remove-entry CLUSTER=demo NODE=demo-worker-3 >/dev/null
out=$(task node:remove-entry CLUSTER=demo NODE=demo-cp-1 2>&1); rc=$?
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "cannot remove the first control plane"
assert_contains "$out" "cannot be deleted" "first control plane message"
out=$(task node:remove-entry CLUSTER=demo NODE=demo-nope 2>&1); assert_contains "$out" "not found" "remove unknown node message"
# node:add must give the new node the same per-node addons create gave the others (runsc on --gvisor clusters).
STUB="$TMP/stub"; mkdir -p "$STUB"; export STUB STUB_LOG="$TMP/stub.log"; : > "$STUB_LOG"
printf '#!/usr/bin/env bash\nexec "$@"\n' > "$STUB/sudo"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB/systemctl"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB/sleep"
printf '#!/usr/bin/env bash\necho "podman $*" >> "$STUB_LOG"; case "$*" in *is-system-running*) echo running;; esac; exit 0\n' > "$STUB/podman"
REAL_TASK=$(command -v task)
cat > "$STUB/task" <<SH
#!/usr/bin/env bash
# Pure cluster.yaml edits run for real; everything that needs a host is logged.
case "\$*" in *plan-add*|*append*|*remove-entry*|*mark-joined*) exec "$REAL_TASK" "\$@" ;; *) echo "task \$*" >> "\$STUB_LOG" ;; esac
SH
chmod +x "$STUB"/*
out=$(PATH="$STUB:$PATH" "$REAL_TASK" node:add CLUSTER=demo ROLE=worker JOIN=false 2>&1); rc=$?
assert_eq "0" "$rc" "node:add (stubbed host) exits 0"
assert_contains "$(cat "$STUB_LOG")" "addons:runtimeclass CLUSTER=demo" "node:add installs crun where the runtime needs it"
# The new node mounts the shared store of the cluster's version; fill it first so the node pulls nothing.
assert_contains "$(cat "$STUB_LOG")" "images:ensure K8S=1.37.1" "node:add fills the shared image store"
# The LB's backend list is rendered from cluster.yaml; a changed list is a graceful reload (master-worker SIGUSR2).
assert_contains "$(cat "$STUB_LOG")" "podman kill --signal USR2 demo-lb" "node:add reloads the LB after re-rendering its config"
if [ "$(grep -n 'nodes:render' "$STUB_LOG" | head -1 | cut -d: -f1)" -lt "$(grep -n 'kill --signal USR2' "$STUB_LOG" | head -1 | cut -d: -f1)" ]; then echo "PASS  reload comes after the render"; else echo "FAIL  reload comes after the render"; FAILURES=$((FAILURES+1)); fi
if [ "$(grep -n 'images:ensure' "$STUB_LOG" | head -1 | cut -d: -f1)" -lt "$(grep -n 'nodes:render' "$STUB_LOG" | head -1 | cut -d: -f1)" ]; then echo "PASS  store filled before the node is rendered and started"; else echo "FAIL  store filled before the node is rendered and started"; FAILURES=$((FAILURES+1)); fi
assert_contains "$(cat "$STUB_LOG")" "addons:gvisor CLUSTER=demo" "node:add installs gVisor on --gvisor clusters"
if [ "$(grep -n 'nodes:render' "$STUB_LOG" | head -1 | cut -d: -f1)" -lt "$(grep -n 'addons:runtimeclass' "$STUB_LOG" | head -1 | cut -d: -f1)" ]; then echo "PASS  addons run after the node is up"; else echo "FAIL  addons run after the node is up"; FAILURES=$((FAILURES+1)); fi
rm -rf "$TMP"; finish
