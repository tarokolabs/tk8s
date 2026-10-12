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
  "pull "*) img=${*##* }
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

rm -rf "$TMP"; finish
