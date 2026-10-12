#!/usr/bin/env bash
# Runs cni:install against stubbed kubectl/cilium/curl/sudo so the failure paths can be
# exercised without a cluster. Regression for: create reported "ready" after cilium timed out.
source "$(dirname "$0")/lib.sh"
cd "$(dirname "$0")/.." || exit 1
TMP=$(mktemp -d); export TK_DATA_DIR="$TMP"
STUB="$TMP/bin"; mkdir -p "$STUB" "$TMP/clusters/demo"; export STUB STUB_LOG="$TMP/calls.log"
v() { sed -nE "s/^$1: *\"?([^\"]+)\"?.*/\1/p" versions.yaml; }
fixture() {  # cni
  cat > "$TMP/clusters/demo/cluster.yaml" <<YAML
metadata: {name: demo}
spec:
  cni: $1
  datapath: veth
  network: {index: 0, nodes: 172.22.0.0/24, pods: 10.244.0.0/21, services: 10.98.0.0/24, lb_range: 172.22.0.200-172.22.0.219}
  nodes: [{role: control-plane, name: demo-control-plane, ip: 172.22.0.1}]
YAML
}
printf '#!/usr/bin/env bash\nexec "$@"\n' > "$STUB/sudo"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB/sleep"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB/tar"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB/ip"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB/systemctl"
printf '#!/usr/bin/env bash\necho "podman $*" >> "$STUB_LOG"; exit 0\n' > "$STUB/podman"
cat > "$STUB/curl" <<'SH'
#!/usr/bin/env bash
# Log the URL and "install" the cilium CLI the way the real download would.
for a in "$@"; do case "$a" in http*) echo "curl $a" >> "$STUB_LOG";; esac; done
printf '#!/usr/bin/env bash\ncase "$1" in status) [ ! -f "$STUB/cilium-fail" ] ;; *) echo "cilium $*" >> "$STUB_LOG" ;; esac\n' > "$STUB/cilium"; chmod +x "$STUB/cilium"
SH
cat > "$STUB/kubectl" <<'SH'
#!/usr/bin/env bash
case "$*" in
  *"get ds cilium"*) [ -f "$STUB/ds-exists" ] ;;
  *"get ds canal"*) exit 1 ;;
  *"get ciliumloadbalancerippool"*) [ -f "$STUB/pool-exists" ] ;;
  *"get secret"*) [ -f "$STUB/ds-exists" ] && echo "sh.helm.release.v1.cilium.v1 helm.sh/release.v1 1 1m"; exit 0 ;;
  *"wait gatewayclass"*) [ ! -f "$STUB/gw-fail" ] ;;
  *"get nodes"*) if [ -f "$STUB/nodes-notready" ]; then echo "demo-worker1 NotReady <none> 1m v1.37.0"; fi; echo "demo-control-plane Ready control-plane 1m v1.37.0" ;;
  *) echo "kubectl $*" >> "$STUB_LOG"; exit 0 ;;
esac
SH
chmod +x "$STUB"/*
run() { : > "$STUB_LOG"; rm -f "$STUB/cilium"; out=$(PATH="$STUB:$PATH" task cni:install CLUSTER=demo 2>&1); rc=$?; }

fixture cilium; touch "$STUB/cilium-fail"; run
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "cilium never ready: cni:install fails"
assert_contains "$out" "cilium" "cilium never ready: message names cilium"
rm -f "$STUB/cilium-fail"; touch "$STUB/gw-fail"; run
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "GatewayClass not accepted: cni:install fails"
assert_contains "$out" "GatewayClass" "GatewayClass not accepted: message names it"
rm -f "$STUB/gw-fail"; touch "$STUB/nodes-notready"; run
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "nodes never Ready: cni:install fails"
assert_contains "$out" "Ready" "nodes never Ready: message says so"
rm -f "$STUB/nodes-notready"; run
assert_eq "0" "$rc" "healthy path exits 0"
assert_contains "$out" "cilium $(v cilium) ok (install)" "healthy path reports cilium"
assert_contains "$(cat "$STUB_LOG")" "--set bpf.lbExternalClusterIP=true" "ClusterIPs are reachable from the host and routed machines"
assert_contains "$(cat "$STUB_LOG")" "cilium-cli/releases/download/$(v cilium_cli)/cilium-linux-amd64.tar.gz" "cilium CLI downloaded at the pinned version"
gw=$(v gateway_api)
assert_contains "$(cat "$STUB_LOG")" "kubectl apply --server-side -f $PWD/manifests/gateway-api.${gw#v}.yaml" "Gateway API CRDs applied from the vendored file"
if grep -q "raw.githubusercontent.com" "$STUB_LOG"; then echo "FAIL  cni:install fetches nothing from raw.githubusercontent.com"; FAILURES=$((FAILURES+1)); else echo "PASS  cni:install fetches nothing from raw.githubusercontent.com"; fi
# The CLI install is its own task so images:status can run it before any node exists.
rm -f "$STUB/cilium"; : > "$STUB_LOG"
out=$(TK_OFFLINE=1 PATH="$STUB:$PATH" task cni:cli 2>&1); rc=$?
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "cni:cli with TK_OFFLINE=1 and no CLI fails"
assert_contains "$out" "TK_OFFLINE" "cni:cli offline failure names TK_OFFLINE"
if grep -q "curl" "$STUB_LOG"; then echo "FAIL  cni:cli offline downloads nothing"; FAILURES=$((FAILURES+1)); else echo "PASS  cni:cli offline downloads nothing"; fi
out=$(PATH="$STUB:$PATH" task cni:cli 2>&1); rc=$?
assert_eq "0" "$rc" "cni:cli installs the CLI"
assert_contains "$(cat "$STUB_LOG")" "cilium-cli/releases/download/$(v cilium_cli)/cilium-linux-amd64.tar.gz" "cni:cli downloads the pinned release"
: > "$STUB_LOG"; out=$(PATH="$STUB:$PATH" task cni:cli 2>&1)
if grep -q "curl" "$STUB_LOG"; then echo "FAIL  cni:cli is a no-op when the CLI is present"; FAILURES=$((FAILURES+1)); else echo "PASS  cni:cli is a no-op when the CLI is present"; fi
assert_contains "$(cat "$TMP/clusters/demo/cluster.yaml")" "datapath_resolved: veth" "datapath_resolved recorded"
run  # second run on the same file must not duplicate the key
assert_eq "1" "$(grep -c datapath_resolved "$TMP/clusters/demo/cluster.yaml")" "datapath_resolved written once"
fixture canal; run
assert_eq "0" "$rc" "canal path exits 0"
assert_contains "$(cat "$STUB_LOG")" "projectcalico/calico/$(v calico)/manifests/canal.yaml" "canal manifest at the pinned calico version"
fixture cilium
# Regression: a first attempt that timed out waiting for cilium leaves the DaemonSet behind but
# never applied the LB-IPAM pool; the rerun must run the task again, not report it up to date.
touch "$STUB/ds-exists"; rm -f "$STUB/pool-exists"; run
assert_eq "0" "$rc" "rerun after a cilium timeout exits 0"
assert_contains "$out" "LB-IPAM pool" "rerun after a cilium timeout applies the LB-IPAM pool"
assert_contains "$out" "cilium $(v cilium) ok (upgrade)" "rerun upgrades the existing cilium release"
touch "$STUB/pool-exists"; run
assert_eq "0" "$rc" "complete install is up to date"
if [[ "$out" == *"LB-IPAM pool"* ]]; then echo "FAIL  complete install is not redone"; FAILURES=$((FAILURES+1)); else echo "PASS  complete install is not redone"; fi

rm -rf "$TMP"; finish
