#!/usr/bin/env bash
# verify:cluster against stubbed kubectl/cilium/curl/sudo/systemctl. The happy path is all PASS;
# one broken component turns into exactly one FAIL and exit 1; a stopped cluster is refused by name.
source "$(dirname "$0")/lib.sh"
cd "$(dirname "$0")/.." || exit 1
TMP=$(mktemp -d); export TK_DATA_DIR="$TMP" TK_VERIFY_TIMEOUT=15  # three poll rounds; sleep is stubbed
STUB="$TMP/bin"; mkdir -p "$STUB" "$TMP/clusters/demo/storage/pvc-1234_tk-verify_pvc"; export STUB
touch "$TMP/clusters/demo/.create-complete"
cat > "$TMP/clusters/demo/cluster.yaml" <<'YAML'
metadata: {name: demo}
spec:
  kubernetes: "1.37.0"
  cni: cilium
  datapath: auto
  datapath_resolved: veth
  gvisor: false
  network: {index: 0, nodes: 172.22.0.0/24, gateway: 172.22.0.254, pods: 10.244.0.0/21, services: 10.98.0.0/24, lb_range: 172.22.0.200-172.22.0.219}
  nodes:
    - {role: control-plane, name: demo-control-plane, ip: 172.22.0.1, cpu: 2, memory: 4g, join: true}
    - {role: worker, name: demo-worker1, ip: 172.22.0.2, cpu: 2, memory: 4g, join: true}
YAML
printf '#!/usr/bin/env bash\nexec "$@"\n' > "$STUB/sudo"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB/sleep"
printf '#!/usr/bin/env bash\n[ "$1" = is-active ] && [ ! -f "$STUB/stopped" ]\n' > "$STUB/systemctl"
printf '#!/usr/bin/env bash\n[ "$1" = status ] && [ ! -f "$STUB/cilium-bad" ]\n' > "$STUB/cilium"
cat > "$STUB/curl" <<'SH'
#!/usr/bin/env bash
case "$*" in
  *"/hostname"*) echo "echo-abc" ;;
  *"X-Forwarded-For"*) echo "172.22.0.254" ;;
esac
SH
cat > "$STUB/kubectl" <<'SH'
#!/usr/bin/env bash
echo "kubectl $*" >> "$STUB/kubectl.log"
case "$*" in
  *"get nodes --no-headers"*) printf 'demo-control-plane Ready control-plane 1m v1.37.0\ndemo-worker1 Ready <none> 1m v1.37.0\n' ;;
  *"get nodes -o custom-columns"*) echo "  demo-control-plane v1.37.0 cri-o://1.37.0 6.12" ;;
  *"get pods -n kube-system --no-headers"*)
     # Right after a boot the system pods are still starting; the first answer is not Running.
     n=$(cat "$STUB/pods-calls" 2>/dev/null || echo 0); echo $((n+1)) > "$STUB/pods-calls"
     if [ "$n" -lt 2 ]; then printf 'coredns-1 0/1 ContainerCreating 0 1m\ncilium-1 0/1 PodInitializing 0 1m\n'; else printf 'coredns-1 1/1 Running 0 1m\ncilium-1 1/1 Running 0 1m\n'; fi ;;
  *"get cm cilium-config"*) echo "veth" ;;
  *"top nodes"*)
     # metrics-server answers only after its first scrape cycle; fail the first two calls.
     [ -f "$STUB/no-metrics" ] && exit 1
     n=$(cat "$STUB/top-calls" 2>/dev/null || echo 0); echo $((n+1)) > "$STUB/top-calls"; [ "$n" -ge 2 ] ;;
  *"logs -n tk-verify writer"*) echo "verify-ok" ;;
  *"get pvc -n tk-verify pvc -o jsonpath"*) echo "pvc-1234" ;;
  *"get runtimeclass crun"*) exit 0 ;;
  *"get runtimeclass gvisor"*) exit 1 ;;
  *"logs -n tk-verify rc-crun"*) [ -f "$STUB/crun-bad" ] && exit 1; echo "6.12.0" ;;
  *"get gateway -n tk-verify gw -o jsonpath"*) echo "172.22.0.200" ;;
  *) exit 0 ;;
esac
SH
chmod +x "$STUB"/*
run() { rm -f "$STUB/pods-calls" "$STUB/top-calls"; out=$(PATH="$STUB:$PATH" task verify:cluster CLUSTER=demo 2>&1); rc=$?; }

run
assert_eq "0" "$rc" "all healthy: exit 0"
pin=$(sed -nE 's/^verify_alpine: *"?([^"]+)"?.*/\1/p' versions.yaml)
if [ -n "$pin" ] && grep -q -- "--image=$pin" "$STUB/kubectl.log"; then echo "PASS  test pods use the image pinned in versions.yaml"; else echo "FAIL  test pods use the image pinned in versions.yaml ($pin)"; FAILURES=$((FAILURES+1)); fi
assert_contains "$out" "PASS  nodes: 2/2 Ready" "nodes section"
assert_contains "$out" "PASS  kube-system pods all Running" "system pods (waited for pods still starting after a boot)"
assert_contains "$out" "PASS  cilium status" "cilium status"
assert_contains "$out" "PASS  kubectl top nodes" "metrics-server (retried until the first scrape landed)"
assert_contains "$out" "PASS  PVC bound and pod wrote data" "local-path write"
assert_contains "$out" "PASS  PV data on host" "local-path data on host"
assert_contains "$out" "PASS  RuntimeClass crun (kernel 6.12.0)" "runtimeclass crun"
assert_contains "$out" "SKIP  RuntimeClass gvisor — not enabled (create the cluster with --gvisor)" "gvisor skipped when not enabled"
assert_contains "$out" "PASS  Gateway programmed (172.22.0.200)" "gateway address"
assert_contains "$out" "PASS  HTTPRoute reachable from host (backend echo-abc)" "httproute"
assert_contains "$out" "PASS  X-Forwarded-For carries the client IP (172.22.0.254)" "xff"
assert_contains "$out" "PASS  TCPRoute reachable from host (backend echo-abc)" "tcproute"
assert_contains "$out" "FAIL 0" "summary line has no failures"
if [[ "$out" == *"FAIL  "* ]]; then echo "FAIL  no FAIL lines on a healthy cluster"; FAILURES=$((FAILURES+1)); else echo "PASS  no FAIL lines on a healthy cluster"; fi
touch "$STUB/no-metrics"; run
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "one broken component: exit 1"
assert_contains "$out" "FAIL  kubectl top nodes — metrics-server not ready" "the broken component is named"
assert_contains "$out" "FAIL 1" "summary counts one failure"
rm -f "$STUB/no-metrics"
# A failing command inside a check must produce a FAIL line, not abort the script (go-task runs with errexit).
touch "$STUB/crun-bad"; run
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "failing crun pod: exit 1"
assert_contains "$out" "FAIL  RuntimeClass crun — pod did not succeed" "failing crun pod is reported"
assert_contains "$out" "[gateway-api]" "checks after a failure still run"
assert_contains "$out" "FAIL 1" "summary counts exactly the crun failure"
rm -f "$STUB/crun-bad"
touch "$STUB/stopped"; run
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "stopped cluster: refused"
assert_contains "$out" "cluster demo is not running" "stopped cluster message"
rm -f "$STUB/stopped"; rm -f "$TMP/clusters/demo/.create-complete"; run
assert_contains "$out" "not fully created" "half-created cluster message"
if [[ "$out" == *"[nodes]"* ]]; then echo "FAIL  refused verify runs no checks"; FAILURES=$((FAILURES+1)); else echo "PASS  refused verify runs no checks"; fi
rm -rf "$TMP"; finish
