#!/usr/bin/env bash
# addons:* against stubbed kubectl/podman/curl/sudo. Each addon is idempotent and pinned.
source "$(dirname "$0")/lib.sh"
cd "$(dirname "$0")/.." || exit 1
TMP=$(mktemp -d); export TK_DATA_DIR="$TMP"
STUB="$TMP/bin"; mkdir -p "$STUB" "$TMP/clusters/demo" "$TMP/nodefs"; export STUB STUB_LOG="$TMP/calls.log"
v() { sed -nE "s/^$1: *\"?([^\"]+)\"?.*/\1/p" versions.yaml; }
fixture() {  # gvisor
  cat > "$TMP/clusters/demo/cluster.yaml" <<YAML
metadata: {name: demo}
spec:
  kubernetes: "1.37.0"
  cni: cilium
  datapath: veth
  gvisor: $1
  network: {index: 0, nodes: 172.22.0.0/24, gateway: 172.22.0.254, pods: 10.244.0.0/21, services: 10.98.0.0/24, lb_range: 172.22.0.200-172.22.0.219}
  nodes:
    - {role: control-plane, name: demo-control-plane, ip: 172.22.0.1, cpu: 2, memory: 4g, join: true}
    - {role: worker, name: demo-worker1, ip: 172.22.0.2, cpu: 2, memory: 4g, join: true}
YAML
}
printf '#!/usr/bin/env bash\nexec "$@"\n' > "$STUB/sudo"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB/sleep"
cat > "$STUB/curl" <<'SH'
#!/usr/bin/env bash
# Log the URL; write a marker file when -o is given so the caller sees a download.
out=""; prev=""; for a in "$@"; do case "$prev" in -o) out=$a;; esac; case "$a" in http*) echo "curl $a" >> "$STUB_LOG";; esac; prev=$a; done
[ -z "$out" ] || { case "$out" in *.sha512) printf '%s  gvisor.tar.zstd\n' "$(printf 'stub-download\n' | sha512sum | awk '{print $1}')" > "$out";; *) echo "stub-download" > "$out";; esac; }
SH
cat > "$STUB/podman" <<'SH'
#!/usr/bin/env bash
# Node filesystems are simulated under $TMP/nodefs/<node>/ so `exec test -e` and `cp` are real.
echo "podman $*" >> "$STUB_LOG"
root="$STUB/../nodefs"
case "$1" in
  exec) shift; [ "$1" = -i ] && shift; node=$1; shift
        case "$*" in
          "test -e "*|"test -x "*) [ -e "$root/$node${3}" ] ;;
          "grep -q "*) grep -q -- "$3" "$root/$node$4" 2>/dev/null ;;
          *"apt-get install"*zstd*) mkdir -p "$root/$node/usr/bin"; touch "$root/$node/usr/bin/zstd" ;;
          "sh -c cat >> "*) f=$(echo "$*" | sed -E 's/^sh -c cat >> ([^ ]+).*/\1/'); mkdir -p "$(dirname "$root/$node$f")"; cat >> "$root/$node$f" ;;
          *) exit 0 ;;
        esac ;;
  cp)   src=$2; dst=$3; node=${dst%%:*}; path=${dst#*:}; mkdir -p "$(dirname "$root/$node$path")"; cp "$src" "$root/$node$path" ;;
  *)    exit 0 ;;
esac
SH
cat > "$STUB/kubectl" <<'SH'
#!/usr/bin/env bash
echo "kubectl $*" >> "$STUB_LOG"
case "$*" in
  *"get deploy -n kube-system metrics-server -o jsonpath"*) [ -f "$STUB/ms-exists" ] || exit 1; if [ -f "$STUB/ms-patched" ]; then echo '["--secure-port=10250","--kubelet-insecure-tls"]'; else echo '["--secure-port=10250"]'; fi ;;
  *"get deploy -n kube-system metrics-server"*|*"get deployment -n kube-system metrics-server"*) [ -f "$STUB/ms-exists" ] ;;
  *"get runtimeclass "*) [ -f "$STUB/rc-exists" ] ;;
  *"get deploy"*|*"get deployment"*|*"get ns "*|*"get storageclass"*) exit 1 ;;
  *) exit 0 ;;
esac
SH
chmod +x "$STUB"/*
run() { : > "$STUB_LOG"; out=$(PATH="$STUB:$PATH" task "$@" CLUSTER=demo 2>&1); rc=$?; }
log() { cat "$STUB_LOG"; }
lpv=$(v local_path_provisioner)

# ---- runtimeclass: CRI-O ships crun, only the RuntimeClass object is applied, nothing enters the nodes
fixture false; run addons:runtimeclass
assert_eq "0" "$rc" "runtimeclass exits 0"
assert_contains "$(log)" "kubectl apply -f $PWD/manifests/runtimeclass.yaml" "RuntimeClass crun applied"
if grep -qE "curl |podman cp|podman exec" "$STUB_LOG"; then echo "FAIL  runtimeclass touches no node"; FAILURES=$((FAILURES+1)); else echo "PASS  runtimeclass touches no node"; fi
# ---- gvisor disabled: nothing installed, no RuntimeClass gvisor
fixture false; rm -rf "$TMP/nodefs"; mkdir -p "$TMP/nodefs"; run addons:gvisor
assert_eq "0" "$rc" "gvisor disabled exits 0"
assert_contains "$out" "not enabled" "gvisor disabled says so"
if grep -q "gvisor" "$STUB_LOG"; then echo "FAIL  gvisor disabled touches nothing"; FAILURES=$((FAILURES+1)); else echo "PASS  gvisor disabled touches nothing"; fi
# ---- gvisor on crio: tarball verified on the host, extracted in every node, handler conf written, crio restarted
fixture true; mkdir -p "$TMP/nodefs/demo-control-plane/usr/bin" "$TMP/nodefs/demo-worker1/usr/bin"; touch "$TMP/nodefs/demo-control-plane/usr/bin/zstd" "$TMP/nodefs/demo-worker1/usr/bin/zstd"; run addons:gvisor
if grep -q "apt-get" "$STUB_LOG"; then echo "FAIL  node with zstd: no apt-get"; FAILURES=$((FAILURES+1)); else echo "PASS  node with zstd: no apt-get"; fi
assert_eq "0" "$rc" "gvisor (crio) exits 0"
assert_contains "$(log)" "gvisor/releases/release/$(v gvisor)/x86_64/gvisor.tar.zstd.sha512" "gvisor checksum downloaded at the pinned release"
assert_contains "$(log)" "podman cp $TMP/cache/gvisor-$(v gvisor).tar.zstd demo-worker1:/tmp/gvisor.tar.zstd" "tarball copied into every node"
assert_contains "$(log)" "podman exec demo-worker1 tar --zstd -xf /tmp/gvisor.tar.zstd -C /usr/local/bin" "extracted into the persisted /usr/local/bin"
assert_contains "$(cat "$TMP/nodefs/demo-worker1/etc/crio/crio.conf.d/20-gvisor.conf")" 'runtime_type = "vm"' "CRI-O handler conf written into the node"
assert_contains "$(log)" "podman exec demo-worker1 systemctl restart crio" "crio restarted"
assert_contains "$(log)" "kubectl apply -f -" "RuntimeClass gvisor applied"
# ---- node image without zstd (published before the recipe change): installed before extraction
fixture true; rm -rf "$TMP/nodefs"; mkdir -p "$TMP/nodefs"; run addons:gvisor
assert_eq "0" "$rc" "gvisor on a node without zstd exits 0"
assert_contains "$(log)" "apt-get install -y -qq zstd" "zstd installed into a node whose image lacks it"
if [ "$(grep -n 'apt-get install' "$STUB_LOG" | head -1 | cut -d: -f1)" -lt "$(grep -n 'tar --zstd' "$STUB_LOG" | head -1 | cut -d: -f1)" ]; then echo "PASS  zstd installed before extraction"; else echo "FAIL  zstd installed before extraction"; FAILURES=$((FAILURES+1)); fi
run addons:gvisor
assert_eq "1" "$(grep -c 'runtime_type = "vm"' "$TMP/nodefs/demo-worker1/etc/crio/crio.conf.d/20-gvisor.conf")" "second run does not rewrite the handler conf"
if grep -q "systemctl restart crio" "$STUB_LOG"; then echo "FAIL  second run does not restart crio"; FAILURES=$((FAILURES+1)); else echo "PASS  second run does not restart crio"; fi
# ---- old image with runsc baked in: no download, conf still written
fixture true; rm -rf "$TMP/nodefs"; mkdir -p "$TMP/nodefs/demo-control-plane/usr/local/bin" "$TMP/nodefs/demo-worker1/usr/local/bin"
touch "$TMP/nodefs/demo-control-plane/usr/local/bin/runsc" "$TMP/nodefs/demo-worker1/usr/local/bin/runsc"; rm -rf "$TMP/cache"; run addons:gvisor
if grep -q "curl " "$STUB_LOG"; then echo "FAIL  runsc already present: no download"; FAILURES=$((FAILURES+1)); else echo "PASS  runsc already present: no download"; fi
assert_contains "$(cat "$TMP/nodefs/demo-worker1/etc/crio/crio.conf.d/20-gvisor.conf")" 'runtime_type = "vm"' "handler conf still written when the binary was baked in"
# ---- metrics-server: pinned manifest, kubelet-insecure-tls via a JSON patch, rollout waited
fixture false; run addons:metrics-server
assert_eq "0" "$rc" "metrics-server exits 0"
assert_contains "$(log)" "metrics-server/releases/download/$(v metrics_server)/components.yaml" "metrics-server manifest at the pinned version"
assert_contains "$(log)" '"--kubelet-insecure-tls"' "kubelet-insecure-tls added (kind nodes have self-signed kubelet certs)"
assert_contains "$(log)" "rollout status -n kube-system deploy/metrics-server" "waits for the rollout"
# Deployed but the patch never landed (interrupted create): the rerun must patch, not skip.
touch "$STUB/ms-exists"; run addons:metrics-server
assert_eq "0" "$rc" "metrics-server deployed but unpatched: exits 0"
assert_contains "$(log)" '"--kubelet-insecure-tls"' "metrics-server deployed but unpatched: rerun applies the patch"
touch "$STUB/ms-patched"; run addons:metrics-server
if grep -qE "components.yaml|patch deploy" "$STUB_LOG"; then echo "FAIL  metrics-server fully installed is skipped"; FAILURES=$((FAILURES+1)); else echo "PASS  metrics-server fully installed is skipped"; fi
rm -f "$STUB/ms-exists" "$STUB/ms-patched"
# ---- local-path: checked-in manifest for the pinned version
run addons:local-path
assert_eq "0" "$rc" "local-path exits 0"
assert_contains "$(log)" "kubectl apply -f $PWD/manifests/local-path-storage.${lpv#v}.yaml" "local-path manifest applied"
assert_contains "$(log)" "rollout status -n local-path-storage deploy/local-path-provisioner" "waits for the provisioner"
# ---- install runs all four in order and is safe to rerun
run addons:install
assert_eq "0" "$rc" "addons:install exits 0"
order=$(echo "$out" | grep -oE 'RuntimeClass crun ok|gVisor not enabled|metrics-server [^ ]+ ok|local-path [^ ]+ ok' | paste -sd, -)
assert_eq "RuntimeClass crun ok,gVisor not enabled,metrics-server $(v metrics_server) ok,local-path $(v local_path_provisioner) ok" "$order" "install runs runtimeclass, gvisor, metrics-server, local-path in order"
rm -rf "$TMP"; finish
