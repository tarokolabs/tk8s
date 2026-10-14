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
CRIO=$(sed -nE "s/^  \"${K8S%.*}\": *\"([^\"]+)\".*/\1/p" versions.yaml)
printf '#!/usr/bin/env bash\necho "sudo $*" >> "$STUB_LOG"; exec "$@"\n' > "$STUB/sudo"
printf '#!/usr/bin/env bash\necho "flock $1" >> "$STUB_LOG"; touch "$1"; shift; exec "$@"\n' > "$STUB/flock"  # flock creates the lock file, so does the stub
printf '#!/usr/bin/env bash\necho "curl $*" >> "$STUB_LOG"; exit 0\n' > "$STUB/curl"
printf '#!/usr/bin/env bash\nexit 0\n' > "$STUB/tar"
cat > "$STUB/podman" <<'SH'
#!/usr/bin/env bash
# With CONTAINERS_STORAGE_CONF set the call is against the shared store (markers under $STUB/store);
# without it, against the host store (the node image is there unless $STUB/no-node-image exists).
echo "podman${CONTAINERS_STORAGE_CONF:+ [store]} $*" >> "$STUB_LOG"
key() { echo "$1" | tr '/:@' '___'; }
case "$*" in
  "image exists "*) if [ -n "${CONTAINERS_STORAGE_CONF:-}" ]; then [ -f "$STUB/../store/$(key "$3")" ];
                    else case "$3" in ghcr.io/tarokolabs/tk8s/node:*) [ ! -f "$STUB/no-node-image" ] ;; *) [ -f "$STUB/host-has-images" ] ;; esac; fi ;;
  "save "*) [ -f "$STUB/../store/$(key "$2")" ] || exit 125; echo "saved $2" ;;
  "load") cat >/dev/null; [ ! -f "$STUB/load-fail" ] || exit 125 ;;
  "pull "*) img=${!#}
            if [ -n "${CONTAINERS_STORAGE_CONF:-}" ]; then [ ! -f "$STUB/pull-fail" ] || exit 125
              if [ -f "$STUB/pull-fail-once" ]; then rm "$STUB/pull-fail-once"; exit 125; fi; touch "$STUB/../store/$(key "$img")"
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
CILIUM=$(v cilium); export K8S CILIUM
run() { : > "$STUB_LOG"; out=$(PATH="$STUB:$PATH" "$@" 2>&1); rc=$?; }

# ---- scripts/images.sh: the list is derived, sorted, unique, and every source is in it
run scripts/images.sh "$K8S"
assert_eq "0" "$rc" "images.sh exits 0"
assert_eq "$(echo "$out" | sort -u)" "$out" "list is sorted and unique"
assert_eq "16" "$(echo "$out" | wc -l | tr -d ' ')" "16 images: 7 control plane, 3 cilium, metrics-server, local-path, haproxy, busybox, alpine, agnhost"
assert_contains "$out" "registry.k8s.io/kube-apiserver:v$K8S" "control plane images from kubeadm in the node image"
assert_contains "$(cat "$STUB_LOG")" "podman run --rm --entrypoint kubeadm ghcr.io/tarokolabs/tk8s/node:v$K8S config images list --kubernetes-version v$K8S" "kubeadm is run from the node image, offline"
assert_contains "$out" "quay.io/cilium/cilium:v$CILIUM@sha256:aaaa" "cilium images, digest-pinned, from the dry-run"
assert_contains "$(cat "$STUB_LOG")" "cilium install --version $CILIUM --chart-directory" "cilium dry-run at the pinned version"
assert_contains "$(cat "$STUB_LOG")" "--chart-directory $PWD/manifests/cilium-$CILIUM" "cilium dry-run reads the vendored chart (no helm.cilium.io fetch, no per-user cache)"
assert_contains "$out" "registry.k8s.io/metrics-server/metrics-server:$(v metrics_server)" "metrics-server image from the vendored manifest"
assert_contains "$out" "docker.io/rancher/local-path-provisioner:$(v local_path_provisioner)" "local-path image from the manifest"
assert_contains "$out" "$(v haproxy)" "host-side apiserver LB image"
assert_eq "1" "$(echo "$out" | grep -c "$(v verify_busybox)")" "busybox once: verify image and the local-path helper are the same pinned tag"
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
assert_contains "$(cat "$STUB_LOG")" "podman build --build-arg VER=$K8S --build-arg CRIO=$CRIO --force-rm -t ghcr.io/tarokolabs/tk8s/node:v$K8S $PWD/images/node" "node image built locally with the CRI-O tag of its Kubernetes minor"
rm -f "$STUB/no-node-image" "$STUB/node-unpublished"
run task images:node K8S="$K8S"
if grep -qE "podman (pull|build)" "$STUB_LOG"; then echo "FAIL  images:node is a no-op when the image exists"; FAILURES=$((FAILURES+1)); else echo "PASS  images:node is a no-op when the image exists"; fi
# REFRESH=1 pulls again even when the tag is present: a republished tag replaces the stale local image.
run task images:node K8S="$K8S" REFRESH=1
assert_eq "0" "$rc" "images:node REFRESH=1 exits 0"
assert_contains "$(cat "$STUB_LOG")" "podman pull ghcr.io/tarokolabs/tk8s/node:v$K8S" "REFRESH=1 pulls a present node image again"
assert_contains "$out" "ghcr.io/tarokolabs/tk8s/node:v$K8S" "REFRESH=1 reports the image"
# TK_OFFLINE=1 means never download: a missing node image is refused up front, not pulled and then built.
touch "$STUB/no-node-image"; run env TK_OFFLINE=1 task images:node K8S="$K8S"
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "images:node offline with no node image fails"
assert_contains "$out" "TK_OFFLINE" "images:node offline failure names TK_OFFLINE"
if grep -qE "podman (pull|build)" "$STUB_LOG"; then echo "FAIL  images:node offline pulls and builds nothing"; FAILURES=$((FAILURES+1)); else echo "PASS  images:node offline pulls and builds nothing"; fi
# A minor without a crio entry: an image already on the host is used as is; building is refused by name.
rm -f "$STUB/no-node-image" "$STUB/node-unpublished"
run task images:node K8S=1.35.5
assert_eq "0" "$rc" "images:node uses a pulled image of a minor without a crio entry"
touch "$STUB/no-node-image" "$STUB/node-unpublished"; run task images:node K8S=1.35.5
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "images:node cannot build a minor without a crio entry"
assert_contains "$out" "no crio entry for Kubernetes minor 1.35" "the refusal names the minor"
if grep -q "podman build" "$STUB_LOG"; then echo "FAIL  no build without a crio entry"; FAILURES=$((FAILURES+1)); else echo "PASS  no build without a crio entry"; fi
rm -f "$STUB/no-node-image" "$STUB/node-unpublished"
# scripts/build-images.sh (release) does the same lookup.
run env TK="$PWD" SUDO= scripts/build-images.sh node "$K8S"
assert_contains "$(cat "$STUB_LOG")" "--build-arg=VER=$K8S --build-arg=CRIO=$CRIO -t ghcr.io/tarokolabs/tk8s/node:v$K8S $PWD/images/node" "build-images.sh passes the CRI-O tag"
run env TK="$PWD" SUDO= scripts/build-images.sh node 1.35.5
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "build-images.sh refuses a minor without a crio entry"
assert_contains "$out" "no crio entry for Kubernetes minor 1.35" "build-images.sh names the minor"
rm -f "$STUB/no-node-image"
run env TK_OFFLINE=1 task nodes:cni-plugins
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "nodes:cni-plugins offline with no plugins fails"
assert_contains "$out" "TK_OFFLINE" "nodes:cni-plugins offline failure names TK_OFFLINE"
if grep -q "curl" "$STUB_LOG"; then echo "FAIL  nodes:cni-plugins offline downloads nothing"; FAILURES=$((FAILURES+1)); else echo "PASS  nodes:cni-plugins offline downloads nothing"; fi
# nodes:image delegates, so a cluster's create and tkctl pull images share one implementation.
mkdir -p "$TMP/clusters/demo"
cat > "$TMP/clusters/demo/cluster.yaml" <<YAML
metadata: {name: demo}
spec:
  kubernetes: "$K8S"
  network: {index: 0, nodes: 172.22.0.0/24, gateway: 172.22.0.254, pods: 10.244.0.0/21, services: 10.98.0.0/24, lb_range: 172.22.0.200-172.22.0.219}
  nodes: [{role: control-plane, name: demo-cp-1, ip: 172.22.0.1, cpu: 2, memory: 4g, join: true}]
YAML
touch "$STUB/no-node-image"; run task nodes:image CLUSTER=demo
assert_contains "$(cat "$STUB_LOG")" "podman pull ghcr.io/tarokolabs/tk8s/node:v$K8S" "nodes:image delegates to images:node"
rm -f "$STUB/no-node-image"

# ---- a root-owned IMAGES_DIR (podman created the bind-mount source for an added node before any
# ensure ran) is taken over like preflight takes over the data dir, not written into as the user.
mkdir -p "$TMP/images"; chmod 555 "$TMP/images"; printf '#!/usr/bin/env bash\necho 0\n' > "$STUB/stat"
printf '#!/usr/bin/env bash\nchmod u+w "${@: -1}"\n' > "$STUB/chown"; chmod +x "$STUB/stat" "$STUB/chown"   # root -> user handover makes the dir writable
run task images:ensure K8S="$K8S"
assert_eq "0" "$rc" "ensure with a root-owned images dir exits 0"
assert_contains "$(cat "$STUB_LOG")" "sudo chown $(id -u):$(id -g) $TMP/images" "root-owned images dir is taken over"
if [ -f "$TMP/images/$K8S.conf" ]; then echo "PASS  conf written after the takeover"; else echo "FAIL  conf written after the takeover"; FAILURES=$((FAILURES+1)); fi
# tkctl pull images is the first thing run on a fresh host (before any create, so before preflight):
# it must build the same user-owned data tree preflight builds, or nodes:cni-plugins fails right after.
assert_contains "$(cat "$STUB_LOG")" "sudo install -d -o $(id -u) -g $(id -g) $TMP/cni" "ensure creates the data tree preflight would (cni)"
if [ -d "$TMP/clusters" ] && [ -w "$TMP/cni" ] && [ -w "$TMP/images" ]; then echo "PASS  data tree exists and is writable after a bare ensure"; else echo "FAIL  data tree exists and is writable after a bare ensure"; FAILURES=$((FAILURES+1)); fi
rm -f "$STUB/stat" "$STUB/chown"; chmod 755 "$TMP/images"; rm -rf "$TMP/images/$K8S" "$TMP/images/$K8S.conf" "$TMP/images/$K8S.lock" "$TMP/store"/*

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
# ---- a pull that fails once (registry CDN hiccup) is retried; one that keeps failing stops with a resumable message
rm "$TMP/store/docker.io_library_alpine_3.22"; touch "$STUB/pull-fail-once"; run task images:ensure K8S="$K8S"
assert_eq "0" "$rc" "a pull that fails once succeeds on the retry"
assert_eq "2" "$(grep -c 'podman \[store\] pull -q docker.io/library/alpine' "$STUB_LOG")" "the failed pull is tried again"
assert_contains "$out" "retrying" "the retry is reported"
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
assert_contains "$(cat "$STUB_LOG")" "flock $TMP/images/$K8S.lock" "prune deletes under the same per-version lock ensure pulls under"
if [ -e "$TMP/images/$K8S" ] || [ -e "$conf" ]; then echo "FAIL  prune removes graphroot and conf"; FAILURES=$((FAILURES+1)); else echo "PASS  prune removes graphroot and conf"; fi
if [ -e "$TMP/images/$K8S.lock" ]; then echo "PASS  prune keeps the lock file (a waiting ensure must not end up on another inode)"; else echo "FAIL  prune keeps the lock file (a waiting ensure must not end up on another inode)"; FAILURES=$((FAILURES+1)); fi
run env TK_ASSUME_YES=1 task images:prune K8S=1.30.0
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "prune of a version without a store fails"
assert_contains "$out" "no image store" "prune of a missing store says so"

# ---- images:host: the LB container runs from the host's own store; copy from the shared store
rm -rf "$TMP/store"/*; run task images:ensure K8S="$K8S"
run task images:host K8S="$K8S"
assert_eq "0" "$rc" "images:host exits 0"
assert_eq "1" "$(grep -c 'podman \[store\] save ' "$STUB_LOG")" "haproxy is read from the shared store"
assert_contains "$(cat "$STUB_LOG")" "sudo env CONTAINERS_STORAGE_CONF=$conf podman save $(v haproxy)" "save goes through the store's storage.conf"
assert_eq "1" "$(grep -c '^sudo podman load' "$STUB_LOG")" "and loaded into the host store"
touch "$STUB/host-has-images"; run task images:host K8S="$K8S"
if grep -q 'podman load' "$STUB_LOG"; then echo "FAIL  images:host is a no-op when the host store has both"; FAILURES=$((FAILURES+1)); else echo "PASS  images:host is a no-op when the host store has both"; fi
rm -f "$STUB/host-has-images"; rm -f "$TMP/store/$(echo "$(v haproxy)" | tr '/:@' '___')"; run task images:host K8S="$K8S"
assert_eq "1" "$([ $rc -ne 0 ] && echo 1)" "a shared store without haproxy fails images:host"
assert_contains "$out" "tkctl pull images --k8s $K8S" "and tells the user how to fill it"

# ---- cluster:create fills the store after the plan is resolved and before the nodes start
e=$(grep -n 'images:ensure' Taskfile.yaml | head -1 | cut -d: -f1); p=$(grep -n 'plan:resolve CLUSTER_FILE' Taskfile.yaml | tail -1 | cut -d: -f1); u=$(grep -n 'nodes:up CLUSTER' Taskfile.yaml | head -1 | cut -d: -f1)
if [ -n "$e" ] && [ "$p" -lt "$e" ] && [ "$e" -lt "$u" ]; then echo "PASS  cluster:create runs images:ensure between plan:resolve and nodes:up"; else echo "FAIL  cluster:create runs images:ensure between plan:resolve and nodes:up"; FAILURES=$((FAILURES+1)); fi

rm -rf "$TMP"; finish
