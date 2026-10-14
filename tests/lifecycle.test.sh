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
  cni: cilium
  datapath: auto
  datapath_resolved: netkit
  gvisor: false
  network:
    index: 3
    nodes: 172.22.3.0/24
    gateway: 172.22.3.254
    lb_range: 172.22.3.200-172.22.3.219
    pods: 10.244.24.0/21
    services: 10.98.3.0/24
    lb: 172.22.3.100
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
out=$(task lifecycle:list 2>&1)
assert_contains "$out" "demo" "list shows cluster"
assert_contains "$out" "1+1" "list shows control-planes+workers"
assert_contains "$out" "172.22.3.0/24" "list shows subnet"
assert_contains "$out" "netkit" "list shows resolved datapath"
out=$(task lifecycle:describe CLUSTER=demo 2>&1)
assert_contains "$out" "demo-worker-1" "describe lists nodes"
assert_contains "$out" "172.22.3.2" "describe lists node ip"
assert_contains "$out" "/etc/containers/systemd/demo" "describe shows unit dir"
assert_contains "$out" "datapath: netkit" "describe shows resolved datapath"
assert_contains "$out" "joined" "describe shows join state"
assert_contains "$out" "Addons:" "describe has an Addons section"
assert_contains "$out" "unavailable (cluster is not running)" "addon versions are skipped on a stopped cluster instead of timing out"
assert_contains "$out" "Access:" "describe has an Access section"
assert_contains "$out" "lb 172.22.3.100" "describe shows the apiserver LB address"
if [[ "$out" == *"dns 172"* ]]; then echo "FAIL  describe shows no cluster DNS (there is none)"; FAILURES=$((FAILURES+1)); else echo "PASS  describe shows no cluster DNS (there is none)"; fi
assert_contains "$out" "apiserver:  https://172.22.3.100:6443" "describe shows the endpoint kubectl uses"
assert_contains "$out" "ip route add 172.22.3.0/24 via " "describe prints the route another machine needs to reach LB IPs and ClusterIPs"
# Running cluster: addon versions come from kubectl with the cluster's kubeconfig; kubectl errors must not abort describe.
STUB="$TMP/stub"; mkdir -p "$STUB"
printf '#!/usr/bin/env bash\n[ "$1" = is-active ]\n' > "$STUB/systemctl"
cat > "$STUB/kubectl" <<'SH'
#!/usr/bin/env bash
echo "kubeconfig=$KUBECONFIG" >> "$STUB_LOG"
case "$*" in
  *"get deploy metrics-server"*) [ -f "$STUB/ms" ] && echo "registry.k8s.io/metrics-server/metrics-server:v0.9.0" || exit 1 ;;
  *) exit 1 ;;
esac
SH
chmod +x "$STUB"/*; export STUB STUB_LOG="$TMP/stub.log"; : > "$STUB_LOG"
out=$(KUBECONFIG=/nonexistent/other-cluster PATH="$STUB:$PATH" task lifecycle:describe CLUSTER=demo 2>&1); rc=$?
assert_eq "0" "$rc" "describe on a running cluster exits 0 even when kubectl fails"
assert_contains "$out" "runtimeclass    none" "kubectl failure shows as none, not an abort"
assert_contains "$out" "Paths:" "describe continues past the Addons section"
assert_contains "$(cat "$STUB_LOG")" "kubeconfig=$TMP/clusters/demo/kubeconfig" "kubectl uses the cluster kubeconfig, not the caller's KUBECONFIG"
touch "$STUB/ms"; out=$(PATH="$STUB:$PATH" task lifecycle:describe CLUSTER=demo 2>&1)
assert_contains "$out" "metrics-server  v0.9.0" "installed addon shows its version"
out=$(task lifecycle:describe CLUSTER=demo OUTPUT=yaml 2>&1)
assert_contains "$out" "apiVersion" "describe -o yaml prints cluster.yaml"
assert_fails "describe unknown cluster fails" task lifecycle:describe CLUSTER=nope
assert_fails "stop unknown cluster fails" task lifecycle:stop CLUSTER=nope
# systemctl only waits for the jobs of the units it was given; the nodes and the LB stop through
# PartOf= afterwards, so stop names every unit of the cluster and returns once all of them are down.
printf '#!/usr/bin/env bash\necho "systemctl $*" >> "$STUB_LOG"; [ "$1" != is-active ]\n' > "$STUB/systemctl"
printf '#!/usr/bin/env bash\nexec "$@"\n' > "$STUB/sudo"; chmod +x "$STUB/systemctl" "$STUB/sudo"
: > "$STUB_LOG"; out=$(PATH="$STUB:$PATH" task lifecycle:stop CLUSTER=demo 2>&1); rc=$?
assert_eq "0" "$rc" "stop exits 0"
stopline=$(grep '^systemctl stop' "$STUB_LOG")
assert_contains "$stopline" "demo.target" "stop stops the target"
assert_contains "$stopline" "demo-lb.service" "stop names the LB unit"
assert_contains "$stopline" "demo-cp-1.service" "stop names the control plane unit"
assert_contains "$stopline" "demo-worker-1.service" "stop names the worker unit"
assert_contains "$out" "cluster demo stopped" "stop reports"
out=$(TK_ASSUME_YES=1 task lifecycle:delete CLUSTER=nope 2>&1); rc=$?
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "delete unknown cluster exits non-zero"
assert_contains "$out" "cluster nope not found" "delete unknown cluster names the cluster"
out=$(task lifecycle:delete CLUSTER=demo 2>&1 </dev/null); rc=$?
assert_contains "$out" "aborted" "delete without confirmation aborts"
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "aborted delete exits non-zero"
if [ -f "$TMP/clusters/demo/cluster.yaml" ]; then echo "PASS  aborted delete keeps the state directory"; else echo "FAIL  aborted delete keeps the state directory"; FAILURES=$((FAILURES+1)); fi
rm -rf "$TMP/clusters/demo"
out=$(task lifecycle:list 2>&1); assert_contains "$out" "no clusters" "list with no clusters"
rm -rf "$TMP"; finish
