#!/usr/bin/env bash
# Every external component version lives in versions.yaml; nothing downloads "latest".
source "$(dirname "$0")/lib.sh"
TMP=$(mktemp -d); export TK_DATA_DIR="$TMP"
cd "$(dirname "$0")/.." || exit 1
v() { sed -nE "s/^$1: *\"?([^\"]+)\"?.*/\1/p" versions.yaml; }
hits=$(grep -nE 'releases/latest|api\.github\.com|calico/v[0-9]|agnhost:[0-9]|library/(busybox|alpine)' Taskfile.yaml taskfiles/*.yaml install.sh scripts/release-notes.sh .github/workflows/*.y*ml 2>/dev/null || true)
assert_eq "" "$hits" "no latest/API lookups or hard-coded versions in the taskfiles"
for k in cni_plugins cilium_cli calico metrics_server local_path_provisioner gvisor haproxy verify_busybox verify_alpine verify_agnhost; do
  if [ -n "$(v $k)" ]; then echo "PASS  versions.yaml pins $k"; else echo "FAIL  versions.yaml pins $k"; FAILURES=$((FAILURES+1)); fi
done
out=$(task --dry nodes:cni-plugins 2>&1)
assert_contains "$out" "containernetworking/plugins/releases/download/$(v cni_plugins)/cni-plugins-linux-amd64-$(v cni_plugins).tgz" "cni-plugins downloads the pinned release directly"
# cni:install is silent, so its pinned URLs are asserted in tests/cni-flow.test.sh.
# The customized local-path manifest is checked in; its file name must carry the pinned version.
lp=$(v local_path_provisioner)
if [ -f "manifests/local-path-storage.${lp#v}.yaml" ]; then echo "PASS  local-path manifest matches versions.yaml ($lp)"; else echo "FAIL  local-path manifest matches versions.yaml ($lp)"; FAILURES=$((FAILURES+1)); fi
# Everything cluster:create applies is checked in, so a create needs no network beyond the image
# store. The vendored files carry the pinned version in their name and content.
gw=$(v gateway_api); f="manifests/gateway-api.${gw#v}.yaml"
if [ -f "$f" ] && grep -q "gateway.networking.k8s.io/bundle-version: $gw" "$f"; then echo "PASS  Gateway API CRDs vendored at $gw"; else echo "FAIL  Gateway API CRDs vendored at $gw"; FAILURES=$((FAILURES+1)); fi
assert_eq "10" "$(grep -c '^kind: CustomResourceDefinition' "$f" 2>/dev/null)" "vendored Gateway API bundle holds the 10 experimental CRDs cni:install applies"
ms=$(v metrics_server); f="manifests/metrics-server.${ms#v}.yaml"
if [ -f "$f" ] && grep -q "image: registry.k8s.io/metrics-server/metrics-server:$ms" "$f"; then echo "PASS  metrics-server manifest vendored at $ms"; else echo "FAIL  metrics-server manifest vendored at $ms"; FAILURES=$((FAILURES+1)); fi
assert_contains "$(cat scripts/vendor-manifests.sh)" "config/crd/experimental" "vendor script fetches the experimental channel cni:install used to fetch"
# cilium-cli embeds only the chart of the cilium release it was built against; any other --version
# is fetched from helm.cilium.io and cached per user. The chart is vendored so cni:install never fetches.
cv=$(v cilium); f="manifests/cilium-$cv/Chart.yaml"
if [ -f "$f" ] && grep -qx "version: $cv" "$f"; then echo "PASS  cilium Helm chart vendored at $cv"; else echo "FAIL  cilium Helm chart vendored at $cv"; FAILURES=$((FAILURES+1)); fi
assert_contains "$(cat scripts/vendor-manifests.sh)" "helm.cilium.io/cilium-" "vendor script fetches the cilium chart cni:install passes with --chart-directory"
# Helper pods run the pinned verify image; a floating busybox tag would be the one pull left in a create.
bb=$(v verify_busybox)
assert_eq "" "$(grep -nE 'image: *(docker.io/library/)?busybox *$' manifests/*.yaml || true)" "no untagged busybox in the manifests"
assert_contains "$(cat "manifests/local-path-storage.${lp#v}.yaml")" "image: $bb" "local-path helper pod runs the pinned busybox"
# The node image is built from pinned artifacts: Kubernetes from dl.k8s.io and CRI-O from its static
# bundle. Every supported Kubernetes minor needs a CRI-O tag in the crio table, or images:node cannot build.
assert_eq "1.37.1" "$(sed -nE 's/^  default: *"([^"]+)"/\1/p' versions.yaml)" "kubernetes default is 1.37.1"
assert_eq "1.36.5" "$(sed -nE 's/^  previous: *"([^"]+)"/\1/p' versions.yaml)" "kubernetes previous is 1.36.5"
crio_of() { sed -nE "s/^  \"$1\": *\"([^\"]+)\".*/\1/p" versions.yaml; }
assert_eq "v1.37.2" "$(crio_of 1.37)" "crio table pins 1.37"
assert_eq "v1.36.7" "$(crio_of 1.36)" "crio table pins 1.36"
for k in default previous; do
  m=$(sed -nE "s/^  $k: *\"([0-9]+\.[0-9]+)\.[0-9]+\"/\1/p" versions.yaml)
  if [ -n "$(crio_of "$m")" ]; then echo "PASS  crio table covers kubernetes.$k ($m)"; else echo "FAIL  crio table covers kubernetes.$k ($m)"; FAILURES=$((FAILURES+1)); fi
done
rm -rf "$TMP"; finish
