#!/usr/bin/env bash
# addons:* against stubbed kubectl/podman/curl/sudo. Each addon is idempotent and pinned.
source "$(dirname "$0")/lib.sh"
cd "$(dirname "$0")/.." || exit 1
TMP=$(mktemp -d); export TK_DATA_DIR="$TMP"
STUB="$TMP/bin"; mkdir -p "$STUB" "$TMP/clusters/demo" "$TMP/nodefs"; export STUB STUB_LOG="$TMP/calls.log"
v() { sed -nE "s/^$1: *\"?([^\"]+)\"?.*/\1/p" versions.yaml; }
fixture() {  # runtime gvisor
  cat > "$TMP/clusters/demo/cluster.yaml" <<YAML
metadata: {name: demo}
spec:
  kubernetes: "1.37.0"
  runtime: $1
  cni: cilium
  datapath: veth
  gvisor: $2
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

# ---- runtimeclass, crio: crun ships with CRI-O, only the RuntimeClass object is applied
fixture crio false; run addons:runtimeclass
assert_eq "0" "$rc" "runtimeclass (crio) exits 0"
assert_contains "$(log)" "kubectl apply -f $PWD/manifests/runtimeclass.yaml" "RuntimeClass crun applied"
if grep -q "crun-" "$STUB_LOG"; then echo "FAIL  crio path does not download crun"; FAILURES=$((FAILURES+1)); else echo "PASS  crio path does not download crun"; fi
# ---- runtimeclass, containerd: crun binary into /usr/local/bin and a runtime table in config.toml, once
fixture containerd false; rm -rf "$TMP/nodefs"; mkdir -p "$TMP/nodefs"; run addons:runtimeclass
assert_eq "0" "$rc" "runtimeclass (containerd) exits 0"
assert_contains "$(log)" "containers/crun/releases/download/$(v crun)/crun-$(v crun)-linux-amd64" "crun downloaded at the pinned version"
assert_contains "$(log)" "podman cp $TMP/cache/crun-$(v crun) demo-worker1:/usr/local/bin/crun" "crun copied into every node"
assert_contains "$(cat "$TMP/nodefs/demo-worker1/etc/containerd/config.toml")" 'runtimes.crun]' "crun runtime table appended to config.toml"
assert_contains "$(cat "$TMP/nodefs/demo-worker1/etc/containerd/config.toml")" 'SystemdCgroup = true' "crun uses the systemd cgroup driver like kind's runc"
assert_contains "$(log)" "podman exec demo-worker1 systemctl restart containerd" "containerd restarted after the config change"
run addons:runtimeclass
assert_eq "1" "$(grep -c 'runtimes.crun]' "$TMP/nodefs/demo-worker1/etc/containerd/config.toml")" "second run does not append a duplicate runtime table"
if grep -q "curl " "$STUB_LOG"; then echo "FAIL  second run does not download again"; FAILURES=$((FAILURES+1)); else echo "PASS  second run does not download again"; fi
# ---- gvisor disabled: nothing installed, no RuntimeClass gvisor
fixture crio false; rm -rf "$TMP/nodefs"; mkdir -p "$TMP/nodefs"; run addons:gvisor
assert_eq "0" "$rc" "gvisor disabled exits 0"
assert_contains "$out" "not enabled" "gvisor disabled says so"
if grep -q "gvisor" "$STUB_LOG"; then echo "FAIL  gvisor disabled touches nothing"; FAILURES=$((FAILURES+1)); else echo "PASS  gvisor disabled touches nothing"; fi
# ---- gvisor on crio: tarball verified on the host, extracted in every node, handler conf written, crio restarted
fixture crio true; run addons:gvisor
assert_eq "0" "$rc" "gvisor (crio) exits 0"
assert_contains "$(log)" "gvisor/releases/release/$(v gvisor)/x86_64/gvisor.tar.zstd.sha512" "gvisor checksum downloaded at the pinned release"
assert_contains "$(log)" "podman cp $TMP/cache/gvisor-$(v gvisor).tar.zstd demo-worker1:/tmp/gvisor.tar.zstd" "tarball copied into every node"
assert_contains "$(log)" "podman exec demo-worker1 tar --zstd -xf /tmp/gvisor.tar.zstd -C /usr/local/bin" "extracted into the persisted /usr/local/bin"
assert_contains "$(cat "$TMP/nodefs/demo-worker1/etc/crio/crio.conf.d/20-gvisor.conf")" 'runtime_type = "vm"' "CRI-O handler conf written into the node"
assert_contains "$(log)" "podman exec demo-worker1 systemctl restart crio" "crio restarted"
assert_contains "$(log)" "kubectl apply -f -" "RuntimeClass gvisor applied"
# ---- gvisor on containerd: shim table appended once
fixture containerd true; rm -rf "$TMP/nodefs"; mkdir -p "$TMP/nodefs"; run addons:gvisor
assert_eq "0" "$rc" "gvisor (containerd) exits 0"
assert_contains "$(cat "$TMP/nodefs/demo-worker1/etc/containerd/config.toml")" 'runtimes.runsc]' "runsc runtime table appended"
assert_contains "$(cat "$TMP/nodefs/demo-worker1/etc/containerd/runsc.toml")" 'systemd-cgroup = "true"' "runsc told to use systemd cgroups"
assert_contains "$(log)" "podman exec demo-worker1 systemctl restart containerd" "containerd restarted for runsc"
run addons:gvisor
assert_eq "1" "$(grep -c 'runtimes.runsc]' "$TMP/nodefs/demo-worker1/etc/containerd/config.toml")" "second run does not duplicate the runsc table"
# ---- old image with runsc baked in: no download, conf still written
fixture crio true; rm -rf "$TMP/nodefs"; mkdir -p "$TMP/nodefs/demo-control-plane/usr/local/bin" "$TMP/nodefs/demo-worker1/usr/local/bin"
touch "$TMP/nodefs/demo-control-plane/usr/local/bin/runsc" "$TMP/nodefs/demo-worker1/usr/local/bin/runsc"; rm -rf "$TMP/cache"; run addons:gvisor
if grep -q "curl " "$STUB_LOG"; then echo "FAIL  runsc already present: no download"; FAILURES=$((FAILURES+1)); else echo "PASS  runsc already present: no download"; fi
assert_contains "$(cat "$TMP/nodefs/demo-worker1/etc/crio/crio.conf.d/20-gvisor.conf")" 'runtime_type = "vm"' "handler conf still written when the binary was baked in"
rm -rf "$TMP"; finish
