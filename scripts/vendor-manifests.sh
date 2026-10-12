#!/usr/bin/env bash
# Vendor the manifests cluster:create applies from a URL, so a create needs no network beyond the
# shared image store. Rerun after bumping gateway_api or metrics_server in versions.yaml: the file
# names carry the version and tests/versions.test.sh checks they match.
set -euo pipefail
TK_HOME="$(cd "$(dirname "$(realpath "$0")")/.." && pwd)"
v() { sed -nE "s/^$1: *\"?([^\"]+)\"?.*/\1/p" "$TK_HOME/versions.yaml"; }
gw=$(v gateway_api); ms=$(v metrics_server)
[ -n "$gw" ] && [ -n "$ms" ] || { echo "versions.yaml has no gateway_api/metrics_server" >&2; exit 1; }
rm -f "$TK_HOME"/manifests/gateway-api.*.yaml "$TK_HOME"/manifests/metrics-server.*.yaml

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
