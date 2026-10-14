#!/usr/bin/env bash
# The node image is built from Debian, not from kindest/node: CRI-O from its static bundle, the
# Kubernetes binaries from dl.k8s.io, every artifact checked against its published checksum. What
# kind's entrypoint used to do at start (rshared, a unique product_uuid) is a systemd unit of ours.
source "$(dirname "$0")/lib.sh"
cd "$(dirname "$0")/.." || exit 1
f=images/node/Containerfile; c=$(cat $f)
assert_contains "$c" "FROM docker.io/library/debian:13-slim" "Debian 13 slim base"
assert_eq "" "$(grep -n 'kindest\|containerd\|/kind/\|CRIO_REPO\|opensuse' $f || true)" "nothing from kind, containerd or the OBS repo"
# Pinned artifacts with checksums
assert_contains "$c" 'https://storage.googleapis.com/cri-o/artifacts/cri-o.amd64.${CRIO}.tar.gz' "CRI-O static bundle at the pinned tag"
assert_contains "$c" 'sha256sum -c "cri-o.amd64.${CRIO}.tar.gz.sha256sum"' "CRI-O bundle checked against its .sha256sum"
assert_contains "$c" "bash ./install" "the bundle's install script is run with bash (its shebang is broken)"
assert_contains "$c" 'https://dl.k8s.io/release/v${VER}/bin/linux/amd64/${b}' "Kubernetes binaries from dl.k8s.io"
assert_contains "$c" '${b}.sha256' "each Kubernetes binary checked against its .sha256"
assert_contains "$c" "for b in kubeadm kubelet kubectl" "kubeadm, kubelet and kubectl"
# systemd in the container
assert_contains "$c" "systemd systemd-sysv dbus" "systemd and dbus (CRI-O's systemd cgroup manager talks over sd-bus)"
assert_contains "$c" "systemctl enable crio.service kubelet.service tk8s-node-init.service" "crio, kubelet and the node init unit enabled"
assert_contains "$c" "systemctl mask systemd-udevd.service" "udev masked"
assert_contains "$c" "getty.target" "getty masked"
assert_contains "$c" ": > /etc/machine-id" "machine-id emptied so each node generates its own on first boot"
assert_contains "$c" "ln -s /etc/machine-id /var/lib/dbus/machine-id" "dbus machine-id is a symlink, not the build-time copy systemd would reuse"
assert_contains "$c" "STOPSIGNAL SIGRTMIN+3" "systemd's stop signal"
assert_contains "$c" "ReadKMsg=no" "journald does not read the host's /dev/kmsg (privileged container)"
assert_contains "$c" "rm -f /root/.crio-install" "the bundle's uninstall manifest is not left in the image"
assert_eq "" "$(grep -n '^ENV DEBIAN_FRONTEND' $f || true)" "DEBIAN_FRONTEND is a build ARG, not a runtime ENV"
assert_contains "$c" 'CMD ["/sbin/init"]' "systemd is PID 1"
# Our files are in the image
for p in systemd/kubelet.service systemd/kubelet.service.d/10-kubeadm.conf systemd/tk8s-node-init.service sbin/tk8s-node-init registries.conf.d/zz-taroko.conf crio.conf.d/10-taroko.conf crictl.yaml storage.conf; do
  if [ -f "images/node/$p" ]; then echo "PASS  images/node/$p exists"; else echo "FAIL  images/node/$p exists"; FAILURES=$((FAILURES+1)); fi
done
assert_contains "$c" "COPY systemd/kubelet.service /etc/systemd/system/kubelet.service" "kubelet unit copied"
assert_contains "$c" "COPY systemd/kubelet.service.d/ /etc/systemd/system/kubelet.service.d/" "kubeadm drop-in copied"
assert_contains "$c" "COPY systemd/tk8s-node-init.service /etc/systemd/system/tk8s-node-init.service" "node init unit copied"
assert_contains "$c" "COPY sbin/tk8s-node-init /usr/local/sbin/tk8s-node-init" "node init script copied"
assert_contains "$c" "COPY registries.conf.d/ /etc/containers/registries.conf.d/" "registries drop-in copied"
assert_contains "$c" "COPY crio.conf.d/ /etc/crio/crio.conf.d/" "CRI-O drop-ins copied"
assert_contains "$c" "COPY storage.conf /etc/containers/storage.conf" "storage.conf copied"
assert_contains "$c" "COPY crictl.yaml /etc/crictl.yaml" "crictl.yaml copied"
# kubelet: the standard kubeadm drop-in, plus --runtime-cgroups pointing at crio.service (a kubelet
# watching a cgroup that never exists logs an error every few seconds)
d=$(cat images/node/systemd/kubelet.service.d/10-kubeadm.conf 2>/dev/null)
assert_contains "$d" "EnvironmentFile=-/etc/default/kubelet" "drop-in reads /etc/default/kubelet"
assert_contains "$d" "EnvironmentFile=-/var/lib/kubelet/kubeadm-flags.env" "drop-in reads kubeadm-flags.env"
assert_contains "$c" "KUBELET_EXTRA_ARGS=--runtime-cgroups=/system.slice/crio.service" "kubelet --runtime-cgroups follows the runtime"
assert_contains "$(cat images/node/systemd/kubelet.service 2>/dev/null)" "ExecStart=/usr/local/bin/kubelet" "kubelet unit runs the dl.k8s.io binary"
# What kind's entrypoint did
i=$(cat images/node/sbin/tk8s-node-init 2>/dev/null)
assert_contains "$i" "mount --make-rshared /" "root made rshared (kubelet mounts must propagate)"
assert_contains "$i" "/etc/tk8s/product_uuid" "product_uuid persisted under /etc (a node volume)"
assert_contains "$i" "mount --bind /etc/tk8s/product_uuid /sys/class/dmi/id/product_uuid" "product_uuid bind-mounted over the host's DMI"
u=$(cat images/node/systemd/tk8s-node-init.service 2>/dev/null)
assert_contains "$u" "Before=crio.service kubelet.service" "node init runs before crio and kubelet"
assert_contains "$u" "ExecStart=/usr/local/sbin/tk8s-node-init" "node init unit runs the script"
assert_contains "$u" "Conflicts=shutdown.target" "node init unit pairs DefaultDependencies=no with the shutdown conflict"
# Short image names resolve to docker.io, whatever the bundle's registries.conf says
assert_contains "$(cat images/node/registries.conf.d/zz-taroko.conf 2>/dev/null)" 'unqualified-search-registries = ["docker.io"]' "kubectl run --image=nginx pulls from docker.io"
# The shared store: nodes mount it where CRI-O's storage.conf lists an additional image store. The
# mount point must exist in the image (containers/storage stats it; empty is fine, missing is not).
s=images/node/storage.conf
assert_contains "$(cat $s)" '"/var/lib/containers/shared"' "storage.conf lists /var/lib/containers/shared as an additional image store"
assert_contains "$c" "mkdir -p /var/lib/containers/shared" "the mount point is created in the image"
assert_contains "$(cat taskfiles/nodes.yaml)" ":/var/lib/containers/shared:ro" "nodes mount the store at the path CRI-O looks at"
# CRI-O's built-in sandbox image and kubeadm's listed pause drift apart; the store only holds
# kubeadm's, so CRI-O is told at build time to use that one.
assert_contains "$c" "kubeadm config images list" "the image build reads the pause image from kubeadm"
assert_contains "$c" 'pause_image = ' "CRI-O's pause_image is set to kubeadm's pause in a crio.conf.d drop-in"
# The apiserver LB is a host-side container; the node image carries no proxy.
assert_eq "" "$(grep -n 'haproxy\|apiserver-lb' $f || true)" "node image has no haproxy or apiserver-lb"
# Teaching tools (no yq: not in Debian's apt)
assert_contains "$c" "nano vim iputils-ping less jq tcpdump strace" "editor and debugging tools"
assert_contains "$c" " zstd " "zstd in the image (addons:gvisor unpacks gvisor.tar.zstd inside the node)"
assert_eq "" "$(grep -nw 'yq' $f || true)" "no yq"
# The CRI-O drop-in's comments describe the bundle, not a deb
assert_eq "" "$(grep -n '套件\|kcn' images/node/crio.conf.d/10-taroko.conf || true)" "10-taroko.conf no longer talks about a package or kcn"
# README describes the image students get: base, sources, what is not in it.
r=$(cat README.md)
assert_contains "$r" "## 節點 image" "README has a node image section"
assert_contains "$r" "debian:13-slim" "README names the base"
assert_contains "$r" "dl.k8s.io" "README names the Kubernetes source"
assert_contains "$r" "static bundle" "README names the CRI-O source"
assert_eq "" "$(grep -n 'kindest' README.md || true)" "README does not mention kindest"
# Flaky mirrors (storage.googleapis.com HTTP/2 resets, dl.k8s.io) must not fail a build on the first error.
assert_eq "4" "$(grep -c 'curl -fsSL' images/node/Containerfile)" "Containerfile downloads with curl (CRI-O bundle, its checksum, K8s binaries, their checksums)"
assert_eq "0" "$(grep 'curl -fsSL' images/node/Containerfile | grep -vc -- '--retry 3 --retry-all-errors')" "every download in the Containerfile retries on any error"
finish
