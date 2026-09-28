#!/usr/bin/env bash
# Release notes come from versions.yaml and the digests CI produced; no v1 paths, both supported versions.
source "$(dirname "$0")/lib.sh"
cd "$(dirname "$0")/.." || exit 1
TMP=$(mktemp -d); mkdir -p "$TMP/digests"
v() { sed -nE "s/^$1: *\"?([^\"]+)\"?.*/\1/p" versions.yaml; }
d=$(sed -nE 's/^  default: *"([^"]+)"/\1/p' versions.yaml); p=$(sed -nE 's/^  previous: *"([^"]+)"/\1/p' versions.yaml)
printf 'ghcr.io/tarokolabs/tk8s/node:v%s  sha256:aaa\n' "$d" > "$TMP/digests/node-v$d.digest"
printf 'ghcr.io/tarokolabs/tk8s/node:v%s  sha256:bbb\n' "$p" > "$TMP/digests/node-v$p.digest"
out=$(scripts/release-notes.sh v2026.10.0 "$TMP/digests" 2>&1); rc=$?
assert_eq "0" "$rc" "notes generated"
assert_contains "$out" "TK_VERSION=v2026.10.0 sh" "install line pins the tag"
assert_contains "$out" "| \`ghcr.io/tarokolabs/tk8s/node:v$d\` | \`sha256:aaa\` |" "digest table row"
assert_contains "$out" "cilium $(v cilium)" "cilium version from versions.yaml"
assert_contains "$out" "Gateway API $(v gateway_api)" "gateway api version"
assert_contains "$out" "gVisor $(v gvisor)" "gvisor release"
assert_contains "$out" "metrics-server $(v metrics_server)" "metrics-server version"
assert_contains "$out" "local-path-provisioner $(v local_path_provisioner)" "local-path version"
assert_contains "$out" "$d、$p" "both supported kubernetes versions"
if [[ "$out" == *libexec* ]]; then echo "FAIL  no v1 paths"; FAILURES=$((FAILURES+1)); else echo "PASS  no v1 paths"; fi
rm -rf "$TMP"; finish
