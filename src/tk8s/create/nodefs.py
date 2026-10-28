"""nodefs: the files that make a node's kubelet see the cpu and memory cluster.yaml asked for.

podman's --cpus/--memory only set the container's cgroup. The kubelet sizes the node through
cAdvisor, which never looks at the cgroup: memory is MemTotal from /proc/meminfo, the cpu count is
the number of cpu directories under /sys/devices/system/node/node*. So a node reported the whole
host, the scheduler overcommitted it and eviction never fired before the kernel's OOM killer.

Three files per node, rendered here and bind-mounted by the node's Quadlet unit:
- meminfo over /proc/meminfo: MemTotal is the node's memory (the two lines cAdvisor reads).
- sys-node over /sys/devices/system/node: one node0 whose cpuN entries are symlinks to the real
  /sys/devices/system/cpu/cpuN (topology and cache are real, only the count changes), plus the
  small files cAdvisor and lscpu expect next to them.
- kubelet-reserved.conf into the kubelet's --config-dir: kubeReserved alone. With capacity now the
  node's own size there is nothing to put in systemReserved; the kubelet turns the reservation into
  the kubepods cgroup limit and the allocatable eviction signal itself.
- kubelet-config-dir.conf, the systemd drop-in that adds --config-dir to the kubelet's command line.
"""

from pathlib import Path

from .. import model

KUBE_RESERVED_CPU_MILLI = 500
KUBE_RESERVED_MEMORY = 512 * 2**20
EVICTION_MEMORY = 500 * 2**20  # evictionHard memory.available in templates/init-config.yaml

MEMINFO_MOUNT = "/proc/meminfo"
SYS_NODE_MOUNT = "/sys/devices/system/node"
DROP_IN_MOUNT = "/etc/kubernetes/kubelet.conf.d/10-tk8s-reserved.conf"
CONFIG_DIR_MOUNT = "/etc/systemd/system/kubelet.service.d/20-tk8s-config-dir.conf"
SYSFS_CPU = Path("/sys/devices/system/cpu")

DROP_IN = (
    "apiVersion: kubelet.config.k8s.io/v1beta1\n"
    "kind: KubeletConfiguration\n"
    "kubeReserved:\n"
    f"  cpu: {KUBE_RESERVED_CPU_MILLI}m\n"
    f"  memory: {KUBE_RESERVED_MEMORY // 2**20}Mi\n"
)


# The kubelet learns --config-dir from a systemd drop-in mounted next to kubeadm's 10-kubeadm.conf,
# not from the image: an image flag would point at a directory that does not exist on a node whose
# /etc volume was filled from the image without the mount (add node on a cluster from before), and
# the floating node:v<k8s> tag makes "which image is this host running" unknowable.
CONFIG_DIR_DROP_IN = (
    "# tkctl: the kubelet also reads /etc/kubernetes/kubelet.conf.d (kubeReserved)\n"
    "[Service]\n"
    'Environment="KUBELET_CONFIG_ARGS=--config=/var/lib/kubelet/config.yaml '
    '--config-dir=/etc/kubernetes/kubelet.conf.d"\n'
)


def memory_bytes(memory: str) -> int:
    """podman's size: <n>g is n GiB, <n>m is n MiB."""
    n, unit = int(memory[:-1]), memory[-1].lower()
    return n * (2**30 if unit == "g" else 2**20)


def node_dir(state: Path, name: str) -> Path:
    return state / "nodes" / name


def _cpumap(cpus: int) -> str:
    """The kernel's cpumask text: 32-bit words from the top, hex, comma-separated."""
    mask = (1 << cpus) - 1
    words = []
    while True:
        words.append(f"{mask & 0xFFFFFFFF:x}")
        mask >>= 32
        if mask == 0:
            break
    return ",".join(reversed(words))


def files(dir: Path, node: model.Node) -> dict[Path, str]:
    """Every regular file under the node's directory, in write order."""
    kb = memory_bytes(node.memory) // 1024
    n0 = dir / "sys-node" / "node0"
    return {
        dir / "meminfo": f"MemTotal: {kb} kB\nSwapTotal: 0 kB\n",
        dir / "kubelet-reserved.conf": DROP_IN,
        dir / "kubelet-config-dir.conf": CONFIG_DIR_DROP_IN,
        n0 / "cpulist": ("0" if node.cpu == 1 else f"0-{node.cpu - 1}") + "\n",
        n0 / "cpumap": _cpumap(node.cpu) + "\n",
        n0 / "distance": "10\n",
        n0 / "meminfo": (
            f"Node 0 MemTotal: {kb} kB\nNode 0 MemFree: {kb} kB\nNode 0 MemUsed: 0 kB\n"
        ),
        n0 / "hugepages" / "hugepages-2048kB" / "nr_hugepages": "0\n",
        n0 / "hugepages" / "hugepages-1048576kB" / "nr_hugepages": "0\n",
    }


def links(dir: Path, node: model.Node) -> dict[Path, Path]:
    """node0/cpuN -> the real cpuN, one per cpu of the node."""
    n0 = dir / "sys-node" / "node0"
    return {n0 / f"cpu{i}": SYSFS_CPU / f"cpu{i}" for i in range(node.cpu)}


def mounts(dir: Path) -> str:
    """The Volume= lines of the node's unit, each on a new line (the template's $node_mounts)."""
    return (
        f"\nVolume={dir}/meminfo:{MEMINFO_MOUNT}:ro"
        f"\nVolume={dir}/sys-node:{SYS_NODE_MOUNT}:ro"
        f"\nVolume={dir}/kubelet-reserved.conf:{DROP_IN_MOUNT}:ro"
        f"\nVolume={dir}/kubelet-config-dir.conf:{CONFIG_DIR_MOUNT}:ro"
    )


# ---- what the kubelet reports, for verify

_SUFFIX = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}


def cpu_milli(q: str) -> int:
    """A Kubernetes cpu quantity in millicores: "2" -> 2000, "1500m" -> 1500."""
    return int(q[:-1]) if q.endswith("m") else int(q) * 1000


def quantity_bytes(q: str) -> int:
    """A Kubernetes memory quantity in bytes: 4Gi, 3084Mi, 32873260Ki or plain bytes."""
    for suffix, mult in _SUFFIX.items():
        if q.endswith(suffix):
            return int(q[: -len(suffix)]) * mult
    return int(q)


def expected_allocatable(node: model.Node) -> tuple[int, int]:
    """(cpu millicores, memory bytes) the kubelet should report as allocatable."""
    return (
        node.cpu * 1000 - KUBE_RESERVED_CPU_MILLI,
        memory_bytes(node.memory) - KUBE_RESERVED_MEMORY - EVICTION_MEMORY,
    )
