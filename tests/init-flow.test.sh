#!/usr/bin/env bash
# kubeadm:init against stubbed sudo/podman. Regression for: a failed read of admin.conf left an
# empty kubeconfig that satisfied the task's status check on every later resume.
source "$(dirname "$0")/lib.sh"
cd "$(dirname "$0")/.." || exit 1
TMP=$(mktemp -d); export TK_DATA_DIR="$TMP"
STUB="$TMP/bin"; mkdir -p "$STUB" "$TMP/clusters/demo"; export STUB
cat > "$TMP/clusters/demo/cluster.yaml" <<'YAML'
metadata: {name: demo}
spec:
  kubernetes: "1.37.0"
  network: {index: 0, nodes: 172.22.0.0/24, gateway: 172.22.0.254, pods: 10.244.0.0/21, services: 10.98.0.0/24}
  nodes: [{role: control-plane, name: demo-control-plane, ip: 172.22.0.1, join: true}]
YAML
printf '#!/usr/bin/env bash\nexec "$@"\n' > "$STUB/sudo"
cat > "$STUB/podman" <<'SH'
#!/usr/bin/env bash
case "$*" in
  *"test -f /etc/kubernetes/admin.conf"*) exit 0 ;;
  *"cat /etc/kubernetes/admin.conf"*) if [ -f "$STUB/cat-fail" ]; then echo "container not running" >&2; exit 125; fi; echo "apiVersion: v1"; echo "kind: Config" ;;
  *) exit 0 ;;
esac
SH
chmod +x "$STUB"/*
touch "$STUB/cat-fail"
out=$(PATH="$STUB:$PATH" task kubeadm:init CLUSTER=demo 2>&1); rc=$?
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "init fails when admin.conf cannot be read"
assert_contains "$out" "admin.conf" "failure names admin.conf"
if [ -e "$TMP/clusters/demo/kubeconfig" ]; then echo "FAIL  no kubeconfig left behind after a failed read"; FAILURES=$((FAILURES+1)); else echo "PASS  no kubeconfig left behind after a failed read"; fi
rm -f "$STUB/cat-fail"
out=$(PATH="$STUB:$PATH" task kubeadm:init CLUSTER=demo 2>&1); rc=$?
assert_eq "0" "$rc" "init succeeds when admin.conf is readable"
assert_contains "$(cat "$TMP/clusters/demo/kubeconfig")" "kind: Config" "kubeconfig holds admin.conf"
assert_eq "600" "$(stat -f %Lp "$TMP/clusters/demo/kubeconfig" 2>/dev/null || stat -c %a "$TMP/clusters/demo/kubeconfig")" "kubeconfig is 0600"
rm -rf "$TMP"; finish
