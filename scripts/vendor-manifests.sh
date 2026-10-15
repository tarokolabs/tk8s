#!/usr/bin/env bash
# Vendor the manifests create applies, so a create needs no network beyond the shared image store.
# Rerun after bumping gateway_api, metrics_server, local_path_provisioner or cilium in versions.yaml:
# the file names carry the version and tests/test_manifests.py checks they match.
set -euo pipefail
TK_HOME="$(cd "$(dirname "$(realpath "$0")")/.." && pwd)"
v() { sed -nE "s/^$1: *\"?([^\"]+)\"?.*/\1/p" "$TK_HOME/versions.yaml"; }
gw=$(v gateway_api); ms=$(v metrics_server); lp=$(v local_path_provisioner); bb=$(v verify_busybox); cv=$(v cilium)
[ -n "$gw" ] && [ -n "$ms" ] && [ -n "$lp" ] && [ -n "$bb" ] && [ -n "$cv" ] \
  || { echo "versions.yaml lacks one of gateway_api, metrics_server, local_path_provisioner, verify_busybox, cilium" >&2; exit 1; }
rm -f "$TK_HOME"/manifests/gateway-api.*.yaml "$TK_HOME"/manifests/metrics-server.*.yaml \
      "$TK_HOME"/manifests/local-path-storage.*.yaml
rm -rf "$TK_HOME"/manifests/cilium-[0-9]*

out="$TK_HOME/manifests/gateway-api.${gw#v}.yaml"
base="https://raw.githubusercontent.com/kubernetes-sigs/gateway-api/${gw}/config/crd/experimental"
{
  echo "# Gateway API ${gw} CRDs, experimental channel (the set cni:install applies). Vendored by"
  echo "# scripts/vendor-manifests.sh from ${base}/ ; do not edit, rerun the script after a bump."
  for crd in gatewayclasses gateways httproutes grpcroutes referencegrants backendtlspolicies tlsroutes tcproutes udproutes listenersets; do
    echo "---"
    curl -fsSL "${base}/gateway.networking.k8s.io_${crd}.yaml"
  done
} > "$out.tmp" && mv "$out.tmp" "$out"
echo "wrote $out ($(grep -c '^kind: CustomResourceDefinition' "$out") CRDs)"

out="$TK_HOME/manifests/metrics-server.${ms#v}.yaml"
url="https://github.com/kubernetes-sigs/metrics-server/releases/download/${ms}/components.yaml"
{
  echo "# metrics-server ${ms}, upstream components.yaml. Vendored by scripts/vendor-manifests.sh from"
  echo "# ${url} ; do not edit, rerun the script after a bump."
  curl -fsSL "$url"
} > "$out.tmp" && mv "$out.tmp" "$out"
echo "wrote $out"

# local-path-provisioner is upstream's deploy manifest with three platform edits applied here, so a
# bump keeps them: the edits are checked after the download and the script fails if upstream moved.
out="$TK_HOME/manifests/local-path-storage.${lp#v}.yaml"
url="https://raw.githubusercontent.com/rancher/local-path-provisioner/${lp}/deploy/local-path-storage.yaml"
{
  echo "# local-path-provisioner ${lp}, upstream deploy/local-path-storage.yaml with three platform edits:"
  echo "#   1. StorageClass volumeBindingMode: Immediate (a PVC binds as soon as it is created, no pod needed)"
  echo "#   2. ConfigMap sharedFileSystemPath=/opt/taroko/storage (a node's /opt/taroko is the host's"
  echo "#      clusters/<name>, so PV data lands on the host side)"
  echo "#   3. the helper image pinned to versions.yaml verify_busybox"
  echo "# Vendored by scripts/vendor-manifests.sh from ${url} ; do not edit, rerun the script after a bump."
  curl -fsSL "$url" \
    | sed -e 's/^volumeBindingMode: WaitForFirstConsumer$/volumeBindingMode: Immediate/' \
          -e "s#^\( *image: \)docker.io/library/busybox\$#\1${bb}#" \
    | awk '
        /^  config.json: \|-$/ {
          print; print "    {"; print "      \"nodePathMap\": [],"
          print "      \"sharedFileSystemPath\": \"/opt/taroko/storage\""; print "    }"
          skip = 1; next
        }
        skip && /^    }$/ { skip = 0; next }
        skip { next }
        { print }'
} > "$out.tmp" && mv "$out.tmp" "$out"
grep -q '^volumeBindingMode: Immediate$' "$out" && grep -q "image: ${bb}$" "$out" \
  && grep -q '"sharedFileSystemPath": "/opt/taroko/storage"' "$out" && ! grep -q DEFAULT_PATH_FOR_NON_LISTED_NODES "$out" \
  || { echo "upstream local-path-storage.yaml changed shape; fix the edits in this script" >&2; exit 1; }
echo "wrote $out"

# cilium-cli embeds only the chart of the cilium release it was built against; any other --version is
# fetched from helm.cilium.io and cached per user. --chart-directory wants an unpacked directory.
out="$TK_HOME/manifests/cilium-${cv}"
url="https://helm.cilium.io/cilium-${cv}.tgz"
tmp=$(mktemp -d)
curl -fsSL "$url" | tar -xz -C "$tmp"
mv "$tmp/cilium" "$out"; rm -rf "$tmp"
echo "wrote $out/ (Helm chart $(sed -nE 's/^version: *//p' "$out/Chart.yaml") from $url)"
