#!/bin/sh
# tk8s installer: prerequisites, task and kubectl (pinned in versions.yaml, checksum verified),
# a checkout under TK_INSTALL_DIR and a tkctl symlink. Safe to rerun.
#   curl -fsSL https://raw.githubusercontent.com/tarokolabs/tk8s/main/install.sh | sh
#   TK_VERSION=v2026.10.0 sh install.sh     # pin a release
set -eu

main() {
  TK_VERSION="${TK_VERSION:-main}"
  TK_REPO="${TK_REPO:-https://github.com/tarokolabs/tk8s.git}"
  TK_INSTALL_DIR="${TK_INSTALL_DIR:-/opt/taroko/tk8s}"
  TK_BIN_DIR="${TK_BIN_DIR:-/usr/local/bin}"
  RAW="https://raw.githubusercontent.com/tarokolabs/tk8s/${TK_VERSION}"

  say() { printf 'install: %s\n' "$*"; }
  fail() { printf 'install: %s\n' "$*" >&2; exit 1; }
  need() { command -v "$1" >/dev/null 2>&1 || fail "$1 not found; $2"; }

  # 1. Prerequisites, each with a named message
  need curl "install curl first"
  need git "install git first"
  need podman "podman >= 5.4 is required (https://podman.io/docs/installation)"
  pv=$(podman --version | awk '{print $3}'); maj=${pv%%.*}; rest=${pv#*.}; min=${rest%%.*}
  if [ "$maj" -lt 5 ] || { [ "$maj" -eq 5 ] && [ "$min" -lt 4 ]; }; then fail "podman $pv is too old; 5.4 or newer is required"; fi
  { [ -d /run/systemd/system ] || systemctl is-system-running >/dev/null 2>&1; } || fail "systemd is required (tk8s v2 runs nodes as Quadlet units)"
  [ "$(stat -fc %T /sys/fs/cgroup 2>/dev/null)" = cgroup2fs ] || fail "cgroup v2 is required"
  if [ -n "$(swapon --show --noheadings 2>/dev/null)" ]; then fail "swap is enabled; run: sudo swapoff -a, then comment the swap line in /etc/fstab"; fi
  sudo -n true 2>/dev/null || fail "sudo without a password prompt is required for podman and systemd"
  say "prerequisites ok: podman $pv, systemd, cgroup v2, swap off"

  # 2. task and kubectl, versions from versions.yaml of the chosen TK_VERSION
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
  curl -fsSL -o "$tmp/versions.yaml" "$RAW/versions.yaml" || fail "cannot fetch versions.yaml for $TK_VERSION"
  task_v=$(sed -nE 's/^task: *"?([^"]+)"?.*/\1/p' "$tmp/versions.yaml")
  k8s_v=$(sed -nE 's/^  default: *"?([^"]+)"?.*/\1/p' "$tmp/versions.yaml")
  [ -n "$task_v" ] && [ -n "$k8s_v" ] || fail "versions.yaml of $TK_VERSION has no task/kubernetes versions"

  # go-task prints a bare version ("3.53.1"); older builds printed "Task version: v3.53.1".
  have_task=$("$TK_BIN_DIR/task" --version 2>/dev/null | awk '{print $NF}' | sed 's/^v//' || true)
  if [ "$have_task" = "${task_v#v}" ]; then
    say "task $task_v already installed"
  else
    base="https://github.com/go-task/task/releases/download/$task_v"
    curl -fsSL -o "$tmp/task.tgz" "$base/task_linux_amd64.tar.gz"
    curl -fsSL -o "$tmp/task_checksums.txt" "$base/task_checksums.txt"
    want=$(grep ' task_linux_amd64.tar.gz$' "$tmp/task_checksums.txt" | awk '{print $1}')
    got=$(sha256sum "$tmp/task.tgz" | awk '{print $1}')
    [ -n "$want" ] && [ "$want" = "$got" ] || fail "task $task_v checksum mismatch (got $got); nothing installed"
    mkdir -p "$tmp/task"; tar -xz -C "$tmp/task" -f "$tmp/task.tgz" task
    sudo install -m 755 "$tmp/task/task" "$TK_BIN_DIR/task"
    say "task $task_v installed to $TK_BIN_DIR"
  fi

  have_kubectl=$("$TK_BIN_DIR/kubectl" version --client -o yaml 2>/dev/null | sed -nE 's/^ *gitVersion: *v?//p' || true)
  if [ "$have_kubectl" = "$k8s_v" ]; then
    say "kubectl $k8s_v already installed"
  else
    base="https://dl.k8s.io/release/v$k8s_v/bin/linux/amd64"
    curl -fsSL -o "$tmp/kubectl" "$base/kubectl"
    want=$(curl -fsSL "$base/kubectl.sha256")
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
  sudo ln -sfn "$TK_INSTALL_DIR/bin/tkctl" "$TK_BIN_DIR/tkctl"
  say "tk8s $TK_VERSION at $TK_INSTALL_DIR ($(git -C "$TK_INSTALL_DIR" rev-parse --short HEAD)); tkctl linked into $TK_BIN_DIR"

  # 4. Next step
  printf '\nNext:\n  tkctl create cluster        # 1 control plane + 2 workers, about four minutes\n  tkctl use cluster tk8s && kubectl get nodes\n'
}

main "$@"
