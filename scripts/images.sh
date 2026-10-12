#!/usr/bin/env bash
# scripts/images.sh <k8s-version>: every image a cluster of that Kubernetes version needs, one per
# line, sorted and unique. Nothing is hand-written: control-plane tags come from kubeadm inside the
# node image, cilium's from `cilium install --dry-run` (no cluster needed), the rest from the
# checked-in manifests and versions.yaml. images:ensure pulls this list into the shared store.
set -euo pipefail
K8S=${1:?usage: images.sh <k8s-version>}
TK_HOME="${TK_HOME:-$(cd "$(dirname "$(realpath "$0")")/.." && pwd)}"
v() { sed -nE "s/^$1: *\"?([^\"]+)\"?.*/\1/p" "$TK_HOME/versions.yaml"; }
images() { sed -nE 's/^ *(- )?image: *"?([^" ]+)"?.*/\2/p' "$@"; }
ms=$(v metrics_server); lp=$(v local_path_provisioner)
# The cilium CLI wants a home directory for helm's cache even in --dry-run (none under a guest agent).
export HOME="${HOME:-/tmp}"
{
  sudo podman run --rm --entrypoint kubeadm "ghcr.io/tarokolabs/tk8s/node:v$K8S" config images list --kubernetes-version "v$K8S"
  # The --set flags of cni:install toggle datapath, IPAM and the Gateway API; none changes the image
  # set (agent, operator, envoy). L3 asserts that no node pulled anything, which catches drift here.
  KUBECONFIG=/nonexistent cilium install --version "$(v cilium)" --dry-run | images
  images "$TK_HOME/manifests/metrics-server.${ms#v}.yaml" \
         "$TK_HOME/manifests/local-path-storage.${lp#v}.yaml" \
         "$TK_HOME/manifests/kube-vip.yaml"
  v verify_busybox; v verify_alpine; v verify_agnhost
} | sort -u
