#!/usr/bin/env bash
# The shared image store: scripts/images.sh derives the image list; images:* fill, list and prune the
# store against stubbed sudo/podman/cilium/flock. Image presence is simulated by marker files under
# $STUB/store/, one per image, created by the podman stub when it is "pulled" with the store conf.
source "$(dirname "$0")/lib.sh"
cd "$(dirname "$0")/.." || exit 1
TMP=$(mktemp -d); export TK_DATA_DIR="$TMP" TK_HOME="$PWD"
STUB="$TMP/bin"; mkdir -p "$STUB" "$TMP/store"; export STUB STUB_LOG="$TMP/calls.log"
v() { sed -nE "s/^$1: *\"?([^\"]+)\"?.*/\1/p" versions.yaml; }
K8S=$(sed -nE 's/^  default: *"([^"]+)"/\1/p' versions.yaml)
printf '#!/usr/bin/env bash\necho "sudo $*" >> "$STUB_LOG"; exec "$@"\n' > "$STUB/sudo"
printf '#!/usr/bin/env bash\necho "flock $1" >> "$STUB_LOG"; shift; exec "$@"\n' > "$STUB/flock"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB/curl"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB/tar"
cat > "$STUB/podman" <<'SH'
#!/usr/bin/env bash
# With CONTAINERS_STORAGE_CONF set the call is against the shared store (markers under $STUB/store);
# without it, against the host store (the node image is there unless $STUB/no-node-image exists).
echo "podman${CONTAINERS_STORAGE_CONF:+ [store]} $*" >> "$STUB_LOG"
key() { echo "$1" | tr '/:@' '___'; }
case "$*" in
  "image exists "*) if [ -n "${CONTAINERS_STORAGE_CONF:-}" ]; then [ -f "$STUB/../store/$(key "$3")" ]; else [ ! -f "$STUB/no-node-image" ]; fi ;;
  "pull "*) img=${!#}
            if [ -n "${CONTAINERS_STORAGE_CONF:-}" ]; then [ ! -f "$STUB/pull-fail" ] || exit 125; touch "$STUB/../store/$(key "$img")"
            else [ ! -f "$STUB/node-unpublished" ] || exit 125; fi ;;
  "run --rm --entrypoint kubeadm "*) [ ! -f "$STUB/kubeadm-fail" ] || exit 1
            printf 'registry.k8s.io/kube-apiserver:v%s\nregistry.k8s.io/kube-controller-manager:v%s\nregistry.k8s.io/kube-scheduler:v%s\nregistry.k8s.io/kube-proxy:v%s\nregistry.k8s.io/coredns/coredns:v1.14.6\nregistry.k8s.io/pause:3.10.2\nregistry.k8s.io/etcd:3.7.0-0\n' "$K8S" "$K8S" "$K8S" "$K8S" ;;
  *) exit 0 ;;
esac
SH
cat > "$STUB/cilium" <<'SH'
#!/usr/bin/env bash
echo "cilium $*" >> "$STUB_LOG"
case "$*" in
  *"--dry-run"*) printf '      image: "quay.io/cilium/cilium:v%s@sha256:aaaa"\n      image: "quay.io/cilium/operator-generic:v%s@sha256:bbbb"\n      image: "quay.io/cilium/cilium-envoy:v1.37.6-1@sha256:cccc"\n' "$CILIUM" "$CILIUM" ;;
esac
SH
chmod +x "$STUB"/*
export K8S CILIUM="$(v cilium)"
run() { : > "$STUB_LOG"; out=$(PATH="$STUB:$PATH" "$@" 2>&1); rc=$?; }

# ---- scripts/images.sh: the list is derived, sorted, unique, and every source is in it
run scripts/images.sh "$K8S"
assert_eq "0" "$rc" "images.sh exits 0"
assert_eq "$(echo "$out" | sort -u)" "$out" "list is sorted and unique"
assert_eq "16" "$(echo "$out" | wc -l | tr -d ' ')" "16 images: 7 control plane, 3 cilium, metrics-server, local-path, kube-vip, busybox, alpine, agnhost"
assert_contains "$out" "registry.k8s.io/kube-apiserver:v$K8S" "control plane images from kubeadm in the node image"
assert_contains "$(cat "$STUB_LOG")" "podman run --rm --entrypoint kubeadm ghcr.io/tarokolabs/tk8s/node:v$K8S config images list --kubernetes-version v$K8S" "kubeadm is run from the node image, offline"
assert_contains "$out" "quay.io/cilium/cilium:v$CILIUM@sha256:aaaa" "cilium images, digest-pinned, from the dry-run"
assert_contains "$(cat "$STUB_LOG")" "cilium install --version $CILIUM --dry-run" "cilium dry-run at the pinned version"
assert_contains "$out" "registry.k8s.io/metrics-server/metrics-server:$(v metrics_server)" "metrics-server image from the vendored manifest"
assert_contains "$out" "docker.io/rancher/local-path-provisioner:$(v local_path_provisioner)" "local-path image from the manifest"
assert_contains "$out" "ghcr.io/kube-vip/kube-vip:$(v kube_vip)" "kube-vip image (HA control planes)"
assert_eq "1" "$(echo "$out" | grep -c "$(v verify_busybox)")" "busybox once: verify image, local-path helper and kube-vip init are the same pinned tag"
assert_contains "$out" "$(v verify_alpine)" "verify alpine"
assert_contains "$out" "$(v verify_agnhost)" "verify agnhost"
touch "$STUB/kubeadm-fail"; run scripts/images.sh "$K8S"
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "a failing source fails the script instead of shortening the list"
rm -f "$STUB/kubeadm-fail"
assert_fails "images.sh without a version fails" scripts/images.sh

# ---- images:node: pull, or build when the tag is not published
touch "$STUB/no-node-image"; run task images:node K8S="$K8S"
assert_eq "0" "$rc" "images:node exits 0"
assert_contains "$(cat "$STUB_LOG")" "podman pull ghcr.io/tarokolabs/tk8s/node:v$K8S" "node image pulled when absent"
touch "$STUB/node-unpublished"; run task images:node K8S="$K8S"
assert_contains "$(cat "$STUB_LOG")" "podman build --build-arg VER=$K8S --force-rm -t ghcr.io/tarokolabs/tk8s/node:v$K8S $PWD/images/node" "node image built locally when the tag is not published"
rm -f "$STUB/no-node-image" "$STUB/node-unpublished"
run task images:node K8S="$K8S"
if grep -qE "podman (pull|build)" "$STUB_LOG"; then echo "FAIL  images:node is a no-op when the image exists"; FAILURES=$((FAILURES+1)); else echo "PASS  images:node is a no-op when the image exists"; fi
# nodes:image delegates, so a cluster's create and tkctl pull images share one implementation.
mkdir -p "$TMP/clusters/demo"
cat > "$TMP/clusters/demo/cluster.yaml" <<YAML
metadata: {name: demo}
spec:
  kubernetes: "$K8S"
  network: {index: 0, nodes: 172.22.0.0/24, gateway: 172.22.0.254, pods: 10.244.0.0/21, services: 10.98.0.0/24, lb_range: 172.22.0.200-172.22.0.219}
  nodes: [{role: control-plane, name: demo-control-plane, ip: 172.22.0.1, cpu: 2, memory: 4g, join: true}]
YAML
touch "$STUB/no-node-image"; run task nodes:image CLUSTER=demo
assert_contains "$(cat "$STUB_LOG")" "podman pull ghcr.io/tarokolabs/tk8s/node:v$K8S" "nodes:image delegates to images:node"
rm -f "$STUB/no-node-image"

# ---- images:list before anything is pulled: header, every image missing, no download of the CLI
run task images:list K8S="$K8S"
assert_eq "0" "$rc" "images:list exits 0"
assert_eq "STATUS   IMAGE" "$(echo "$out" | head -1)" "list header"
assert_eq "16" "$(echo "$out" | grep -c '^missing  ')" "everything missing before the first ensure"
if grep -q "curl" "$STUB_LOG"; then echo "FAIL  list does not download the cilium CLI when present"; FAILURES=$((FAILURES+1)); else echo "PASS  list does not download the cilium CLI when present"; fi

# ---- images:ensure: conf written, graphroot created as root, lock taken, every image pulled once
run task images:ensure K8S="$K8S"
assert_eq "0" "$rc" "ensure exits 0"
conf="$TMP/images/$K8S.conf"
assert_eq "$(printf '# Generated by tkctl: the shared image store for Kubernetes %s. Used only through\n# CONTAINERS_STORAGE_CONF by images:ensure; nodes mount the graphroot read-only.\n[storage]\ndriver = "overlay"\ngraphroot = "%s/images/%s"\nruncroot = "/run/taroko-images/%s"\n\n[storage.options.overlay]\nmountopt = "nodev,metacopy=on"' "$K8S" "$TMP" "$K8S" "$K8S" | sed 's/runcroot/runroot/')" "$(cat "$conf")" "storage.conf content (spec 4.1)"
if [ -d "$TMP/images/$K8S" ]; then echo "PASS  graphroot directory created"; else echo "FAIL  graphroot directory created"; FAILURES=$((FAILURES+1)); fi
assert_contains "$(cat "$STUB_LOG")" "sudo mkdir -p $TMP/images/$K8S" "graphroot is created as root (rootful podman writes it)"
assert_contains "$(cat "$STUB_LOG")" "flock $TMP/images/$K8S.lock" "pull runs under the per-version lock"
assert_eq "16" "$(grep -c "podman \[store\] pull -q " "$STUB_LOG")" "every image pulled into the store"
assert_contains "$(cat "$STUB_LOG")" "sudo env CONTAINERS_STORAGE_CONF=$conf podman pull -q registry.k8s.io/kube-apiserver:v$K8S" "pull goes through the store's storage.conf"
assert_contains "$out" "images $K8S: 0 present, 16 pulled" "ensure summary"
assert_eq "16" "$(ls "$TMP/store" | wc -l | tr -d ' ')" "16 markers in the simulated store"
# ---- rerun: nothing pulled
run task images:ensure K8S="$K8S"
assert_eq "0" "$rc" "second ensure exits 0"
if grep -q "pull -q" "$STUB_LOG"; then echo "FAIL  second ensure pulls nothing"; FAILURES=$((FAILURES+1)); else echo "PASS  second ensure pulls nothing"; fi
assert_contains "$out" "images $K8S: 16 present, nothing to pull" "second ensure says so"
run task images:list K8S="$K8S"
assert_eq "16" "$(echo "$out" | grep -c '^present  ')" "list shows everything present"
# ---- one image gone (interrupted pull, hand-deleted store): only that one is pulled again
rm "$TMP/store/registry.k8s.io_pause_3.10.2"; run task images:ensure K8S="$K8S"
assert_eq "1" "$(grep -c "podman \[store\] pull -q " "$STUB_LOG")" "only the missing image is pulled"
assert_contains "$out" "images $K8S: 15 present, 1 pulled" "partial ensure summary"
# ---- the whole store gone but the conf left behind: everything is pulled again
rm -rf "$TMP/store"/* "$TMP/images/$K8S"; run task images:ensure K8S="$K8S"
assert_eq "16" "$(grep -c "podman \[store\] pull -q " "$STUB_LOG")" "a hand-deleted store is refilled although its conf survived"
if [ -d "$TMP/images/$K8S" ]; then echo "PASS  graphroot recreated"; else echo "FAIL  graphroot recreated"; FAILURES=$((FAILURES+1)); fi
# ---- TK_OFFLINE=1: a missing image is an error, nothing is pulled
rm "$TMP/store/docker.io_library_alpine_3.22"; run env TK_OFFLINE=1 task images:ensure K8S="$K8S"
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "offline with a missing image fails"
assert_contains "$out" "TK_OFFLINE=1" "offline failure names TK_OFFLINE"
assert_contains "$out" "docker.io/library/alpine:3.22" "offline failure lists the missing image"
if grep -q "pull -q" "$STUB_LOG"; then echo "FAIL  offline pulls nothing"; FAILURES=$((FAILURES+1)); else echo "PASS  offline pulls nothing"; fi
run env TK_OFFLINE=1 task images:ensure K8S="$K8S" >/dev/null; touch "$STUB/../store/docker.io_library_alpine_3.22"
run env TK_OFFLINE=1 task images:ensure K8S="$K8S"
assert_eq "0" "$rc" "offline with everything present exits 0"
# ---- a failing pull stops with a resumable message
rm "$TMP/store/docker.io_library_alpine_3.22"; touch "$STUB/pull-fail"; run task images:ensure K8S="$K8S"
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "a failed pull fails ensure"
assert_contains "$out" "rerun to resume" "failed pull tells the user to rerun"
rm -f "$STUB/pull-fail"
# ---- an empty list (images.sh broke) must not look like "nothing to pull"
touch "$STUB/kubeadm-fail"; run task images:ensure K8S="$K8S"
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "a broken image list fails ensure"
rm -f "$STUB/kubeadm-fail"

# ---- images:prune: refused while a cluster of that version exists, even a stopped one
run env TK_ASSUME_YES=1 task images:prune K8S="$K8S"
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "prune refused while demo uses the version"
assert_contains "$out" "demo" "prune names the cluster"
if [ -d "$TMP/images/$K8S" ]; then echo "PASS  refused prune leaves the store"; else echo "FAIL  refused prune leaves the store"; FAILURES=$((FAILURES+1)); fi
rm -rf "$TMP/clusters/demo"
out=$(PATH="$STUB:$PATH" task images:prune K8S="$K8S" 2>&1 </dev/null); rc=$?
assert_contains "$out" "aborted" "prune without confirmation aborts"
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "aborted prune exits non-zero"
run env TK_ASSUME_YES=1 task images:prune K8S="$K8S"
assert_eq "0" "$rc" "prune exits 0"
assert_contains "$(cat "$STUB_LOG")" "sudo rm -rf $TMP/images/$K8S" "graphroot removed as root"
if [ -e "$TMP/images/$K8S" ] || [ -e "$conf" ] || [ -e "$TMP/images/$K8S.lock" ]; then echo "FAIL  prune removes graphroot, conf and lock"; FAILURES=$((FAILURES+1)); else echo "PASS  prune removes graphroot, conf and lock"; fi
run env TK_ASSUME_YES=1 task images:prune K8S=1.30.0
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "prune of a version without a store fails"
assert_contains "$out" "no image store" "prune of a missing store says so"

rm -rf "$TMP"; finish
