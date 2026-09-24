#!/usr/bin/env bash
# Runs lifecycle:delete against stubbed sudo/podman/systemctl/ip so the whole flow
# can be exercised without a host. Regression for: a missing volume aborted the loop.
source "$(dirname "$0")/lib.sh"
TMP=$(mktemp -d); export TK_DATA_DIR="$TMP"
STUB="$TMP/bin"; mkdir -p "$STUB" "$TMP/clusters/demo"; touch "$TMP/clusters/demo/.create-complete"
cat > "$TMP/clusters/demo/cluster.yaml" <<'YAML'
metadata: {name: demo}
spec:
  network: {index: 0, nodes: 172.22.0.0/24, pods: 10.244.0.0/21, services: 10.98.0.0/24}
  nodes:
    - {role: control-plane, name: demo-control-plane, ip: 172.22.0.1, cpu: 2, memory: 4g, join: true}
    - {role: worker, name: demo-worker1, ip: 172.22.0.2, cpu: 2, memory: 4g, join: true}
YAML
cat > "$STUB/sudo" <<'SH'
#!/usr/bin/env bash
exec "$@"
SH
cat > "$STUB/podman" <<'SH'
#!/usr/bin/env bash
# Only the -var volumes exist (cluster created before -etc/-usr-local-bin were introduced).
case "$1 $2" in
  "volume exists") case "$3" in *-var) exit 0;; *) exit 1;; esac ;;
  "container exists") exit 1 ;;
  "network exists") exit 0 ;;
  *) echo "podman $*" >> "$STUB_LOG"; exit 0 ;;
esac
SH
cat > "$STUB/systemctl" <<'SH'
#!/usr/bin/env bash
case "$1" in list-units) echo "demo.target loaded active active";; esac; exit 0
SH
cat > "$STUB/ip" <<'SH'
#!/usr/bin/env bash
case "$1 $2" in "route show") exit 0;; *) exit 0;; esac
SH
cat > "$STUB/rm" <<'SH'
#!/usr/bin/env bash
echo "rm $*" >> "$STUB_LOG"; exec /bin/rm "$@"
SH
chmod +x "$STUB"/*; export STUB_LOG="$TMP/podman.log"; : > "$STUB_LOG"
out=$(PATH="$STUB:$PATH" TK_ASSUME_YES=1 task lifecycle:delete CLUSTER=demo 2>&1); rc=$?
assert_eq "0" "$rc" "delete exits 0 when some volumes are already gone"
assert_contains "$out" "remove volume demo-control-plane-var" "first node volume removed"
assert_contains "$out" "remove volume demo-worker1-var" "second node volume removed (loop did not abort)"
assert_contains "$out" "remove network demo" "network removed after the volume loop"
assert_contains "$out" "remove state" "state directory removed"
assert_contains "$(cat "$STUB_LOG")" "/etc/systemd/system/demo-routes.service" "routes unit removed with the other units"
assert_contains "$out" "cluster demo deleted" "final message"
if [ -d "$TMP/clusters/demo" ]; then echo "FAIL  state dir gone"; FAILURES=$((FAILURES+1)); else echo "PASS  state dir gone"; fi
rm -rf "$TMP"; finish
