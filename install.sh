#!/bin/sh
# tk8s installer: prerequisites, uv and kubectl (pinned in versions.yaml, checksum verified), a
# checkout under TK_INSTALL_DIR with its Python environment, and a tkctl link. Safe to rerun.
#   curl -fsSL https://raw.githubusercontent.com/tarokolabs/tk8s/main/install.sh | sh
#   TK_VERSION=v2026.10.7 sh install.sh     # pin a release
set -eu

main() {
  TK_VERSION="${TK_VERSION:-main}"
  TK_REPO="${TK_REPO:-https://github.com/tarokolabs/tk8s.git}"
  TK_INSTALL_DIR="${TK_INSTALL_DIR:-/opt/taroko/tk8s}"
  TK_BIN_DIR="${TK_BIN_DIR:-/usr/local/bin}"
  # TK_RAW: where versions.yaml is read from (CI points it at the checkout under test with file://).
  RAW="${TK_RAW:-https://raw.githubusercontent.com/tarokolabs/tk8s/${TK_VERSION}}"

  say() { printf 'install: %s\n' "$*"; }
  fail() { printf 'install: %s\n' "$*" >&2; exit 1; }
  need() { command -v "$1" >/dev/null 2>&1 || fail "$1 not found; $2"; }
  fetch() { curl -fsSL --retry 3 --retry-all-errors "$@"; }

  # 1. Prerequisites, each with a named message
  need curl "install curl first"
  need git "install git first"
  need podman "podman >= 5.4 is required (https://podman.io/docs/installation)"
  pv=$(podman --version | awk '{print $3}'); maj=${pv%%.*}; rest=${pv#*.}; min=${rest%%.*}
  if [ "$maj" -lt 5 ] || { [ "$maj" -eq 5 ] && [ "$min" -lt 4 ]; }; then fail "podman $pv is too old; 5.4 or newer is required"; fi
  { [ -d /run/systemd/system ] || systemctl is-system-running >/dev/null 2>&1; } || fail "systemd is required (tk8s runs nodes as Quadlet units)"
  [ "$(stat -fc %T /sys/fs/cgroup 2>/dev/null)" = cgroup2fs ] || fail "cgroup v2 is required"
  if [ -n "$(swapon --show --noheadings 2>/dev/null)" ]; then fail "swap is enabled; run: sudo swapoff -a, then comment the swap line in /etc/fstab"; fi
  sudo -n true 2>/dev/null || fail "sudo without a password prompt is required for podman and systemd"
  say "prerequisites ok: podman $pv, systemd, cgroup v2, swap off"

  # 2. uv and kubectl, versions from versions.yaml of the chosen TK_VERSION
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
  fetch -o "$tmp/versions.yaml" "$RAW/versions.yaml" || fail "cannot fetch versions.yaml for $TK_VERSION"
  uv_v=$(sed -nE 's/^uv: *"?([^"]+)"?.*/\1/p' "$tmp/versions.yaml")
  k8s_v=$(sed -nE 's/^  default: *"?([^"]+)"?.*/\1/p' "$tmp/versions.yaml")
  [ -n "$uv_v" ] && [ -n "$k8s_v" ] || fail "versions.yaml of $TK_VERSION has no uv/kubernetes versions"

  have_uv=$("$TK_BIN_DIR/uv" --version 2>/dev/null | awk '{print $2}' || true)
  if [ "$have_uv" = "$uv_v" ]; then
    say "uv $uv_v already installed"
  else
    base="https://github.com/astral-sh/uv/releases/download/$uv_v"
    fetch -o "$tmp/uv.tgz" "$base/uv-x86_64-unknown-linux-gnu.tar.gz"
    want=$(fetch "$base/uv-x86_64-unknown-linux-gnu.tar.gz.sha256" | awk '{print $1}')
    got=$(sha256sum "$tmp/uv.tgz" | awk '{print $1}')
    [ -n "$want" ] && [ "$want" = "$got" ] || fail "uv $uv_v checksum mismatch (got $got); nothing installed"
    mkdir -p "$tmp/uv"; tar -xz -C "$tmp/uv" --strip-components=1 -f "$tmp/uv.tgz"
    sudo install -m 755 "$tmp/uv/uv" "$TK_BIN_DIR/uv"
    say "uv $uv_v installed to $TK_BIN_DIR"
  fi

  have_kubectl=$("$TK_BIN_DIR/kubectl" version --client -o yaml 2>/dev/null | sed -nE 's/^ *gitVersion: *v?//p' || true)
  if [ "$have_kubectl" = "$k8s_v" ]; then
    say "kubectl $k8s_v already installed"
  else
    base="https://dl.k8s.io/release/v$k8s_v/bin/linux/amd64"
    fetch -o "$tmp/kubectl" "$base/kubectl"
    want=$(fetch "$base/kubectl.sha256")
    got=$(sha256sum "$tmp/kubectl" | awk '{print $1}')
    [ -n "$want" ] && [ "$want" = "$got" ] || fail "kubectl $k8s_v checksum mismatch (got $got); nothing installed"
    sudo install -m 755 "$tmp/kubectl" "$TK_BIN_DIR/kubectl"
    say "kubectl $k8s_v installed to $TK_BIN_DIR"
  fi

  # 3. The platform itself: a git checkout owned by the installing user (git refuses root-owned
  #    repositories for other users, and tkctl version reads git), updated in place on reruns
  if [ -d "$TK_INSTALL_DIR/.git" ]; then
    git -C "$TK_INSTALL_DIR" fetch --tags --quiet origin
    git -C "$TK_INSTALL_DIR" checkout --quiet "$TK_VERSION"
    if [ "$TK_VERSION" = main ]; then git -C "$TK_INSTALL_DIR" pull --quiet --ff-only origin main; fi
  else
    sudo install -d -o "$(id -u)" -g "$(id -g)" "$TK_INSTALL_DIR"
    git clone --quiet --branch "$TK_VERSION" "$TK_REPO" "$TK_INSTALL_DIR"
  fi

  # 4. Python 3.14 and the dependencies exactly as uv.lock pins them, in the checkout's .venv
  #    (uv fetches its own CPython once, under the user's ~/.local/share/uv); nothing runs as root
  "$TK_BIN_DIR/uv" sync --locked --no-dev --project "$TK_INSTALL_DIR" --quiet
  sudo ln -sfn "$TK_INSTALL_DIR/.venv/bin/tkctl" "$TK_BIN_DIR/tkctl"
  say "tk8s $TK_VERSION at $TK_INSTALL_DIR ($(git -C "$TK_INSTALL_DIR" rev-parse --short HEAD)); tkctl linked into $TK_BIN_DIR"
  "$TK_BIN_DIR/tkctl" version

  # 5. Next step
  printf '\nNext:\n  tkctl create cluster        # 1 control plane + 2 workers, about three minutes\n  tkctl use cluster tk8s && kubectl get nodes\n'
}

main "$@"
