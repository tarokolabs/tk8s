#!/usr/bin/env bash
# Vendor the manifests cluster:create applies from a URL, so a create needs no network beyond the
# shared image store. Rerun after bumping gateway_api, metrics_server or cilium in versions.yaml: the
# file names carry the version and tests/versions.test.sh checks they match.
set -euo pipefail
TK_HOME="$(cd "$(dirname "$(realpath "$0")")/.." && pwd)"
v() { sed -nE "s/^$1: *\"?([^\"]+)\"?.*/\1/p" "$TK_HOME/versions.yaml"; }
gw=$(v gateway_api); ms=$(v metrics_server); cv=$(v cilium)
[ -n "$gw" ] && [ -n "$ms" ] && [ -n "$cv" ] || { echo "versions.yaml has no gateway_api/metrics_server/cilium" >&2; exit 1; }
rm -f "$TK_HOME"/manifests/gateway-api.*.yaml "$TK_HOME"/manifests/metrics-server.*.yaml
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

# cilium-cli embeds only the chart of the cilium release it was built against; any other --version is
# fetched from helm.cilium.io and cached per user. --chart-directory wants an unpacked directory.
out="$TK_HOME/manifests/cilium-${cv}"
url="https://helm.cilium.io/cilium-${cv}.tgz"
tmp=$(mktemp -d)
curl -fsSL "$url" | tar -xz -C "$tmp"
mv "$tmp/cilium" "$out"; rm -rf "$tmp"
echo "wrote $out/ (Helm chart $(sed -nE 's/^version: *//p' "$out/Chart.yaml") from $url)"
