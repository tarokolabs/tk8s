#!/usr/bin/env bash
# Every external component version lives in versions.yaml; nothing downloads "latest".
source "$(dirname "$0")/lib.sh"
TMP=$(mktemp -d); export TK_DATA_DIR="$TMP"
cd "$(dirname "$0")/.." || exit 1
v() { sed -nE "s/^$1: *\"?([^\"]+)\"?.*/\1/p" versions.yaml; }
hits=$(grep -nE 'releases/latest|api\.github\.com|calico/v[0-9]' Taskfile.yaml taskfiles/*.yaml || true)
assert_eq "" "$hits" "no latest/API lookups or hard-coded versions in the taskfiles"
for k in cni_plugins cilium_cli calico metrics_server local_path_provisioner gvisor; do
  if [ -n "$(v $k)" ]; then echo "PASS  versions.yaml pins $k"; else echo "FAIL  versions.yaml pins $k"; FAILURES=$((FAILURES+1)); fi
done
out=$(task --dry nodes:cni-plugins 2>&1)
assert_contains "$out" "containernetworking/plugins/releases/download/$(v cni_plugins)/cni-plugins-linux-amd64-$(v cni_plugins).tgz" "cni-plugins downloads the pinned release directly"
# cni:install is silent, so its pinned URLs are asserted in tests/cni-flow.test.sh.
# The customized local-path manifest is checked in; its file name must carry the pinned version.
lp=$(v local_path_provisioner)
if [ -f "manifests/local-path-storage.${lp#v}.yaml" ]; then echo "PASS  local-path manifest matches versions.yaml ($lp)"; else echo "FAIL  local-path manifest matches versions.yaml ($lp)"; FAILURES=$((FAILURES+1)); fi
rm -rf "$TMP"; finish
