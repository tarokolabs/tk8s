"""The files a sized node mounts over /proc/meminfo, /sys/devices/system/node and the kubelet
drop-in directory, rendered from its cpu and memory alone."""

from ipaddress import IPv4Address
from pathlib import Path

from tk8s import model
from tk8s.create import nodefs

D = Path("/opt/taroko/clusters/demo/nodes/demo-worker-1")


def node(cpu: int, memory: str) -> model.Node:
    return model.Node(
        role="worker", name="demo-worker-1", ip=IPv4Address("172.22.0.2"), cpu=cpu, memory=memory
    )


def test_memory_bytes_follows_podman_binary_units() -> None:
    assert nodefs.memory_bytes("4g") == 4 * 2**30
    assert nodefs.memory_bytes("4096m") == 4 * 2**30
    assert nodefs.memory_bytes("6G") == 6 * 2**30


def test_files_are_the_meminfo_the_drop_in_and_the_fake_numa_node() -> None:
    f = nodefs.files(D, node(2, "4g"))
    assert [str(p.relative_to(D)) for p in f] == [
        "meminfo",
        "kubelet-reserved.conf",
        "kubelet-config-dir.conf",
        "sys-node/node0/cpulist",
        "sys-node/node0/cpumap",
        "sys-node/node0/distance",
        "sys-node/node0/meminfo",
        "sys-node/node0/hugepages/hugepages-2048kB/nr_hugepages",
        "sys-node/node0/hugepages/hugepages-1048576kB/nr_hugepages",
    ]
    assert f[D / "meminfo"] == "MemTotal: 4194304 kB\nSwapTotal: 0 kB\n"
    assert f[D / "kubelet-reserved.conf"] == (
        "apiVersion: kubelet.config.k8s.io/v1beta1\nkind: KubeletConfiguration\n"
        "kubeReserved:\n  cpu: 500m\n  memory: 512Mi\n"
    )
    assert f[D / "kubelet-config-dir.conf"] == (
        "# tkctl: the kubelet also reads /etc/kubernetes/kubelet.conf.d (kubeReserved)\n"
        "[Service]\n"
        'Environment="KUBELET_CONFIG_ARGS=--config=/var/lib/kubelet/config.yaml '
        '--config-dir=/etc/kubernetes/kubelet.conf.d"\n'
    )
    assert f[D / "sys-node/node0/cpulist"] == "0-1\n"
    assert f[D / "sys-node/node0/cpumap"] == "3\n"
    assert f[D / "sys-node/node0/distance"] == "10\n"
    assert f[D / "sys-node/node0/meminfo"] == (
        "Node 0 MemTotal: 4194304 kB\nNode 0 MemFree: 4194304 kB\nNode 0 MemUsed: 0 kB\n"
    )
    assert f[D / "sys-node/node0/hugepages/hugepages-2048kB/nr_hugepages"] == "0\n"


def test_m_sized_memory_renders_the_same_kb() -> None:
    assert nodefs.files(D, node(2, "4096m"))[D / "meminfo"].startswith("MemTotal: 4194304 kB\n")


def test_one_cpu_and_more_than_32_cpus_render_kernel_style_lists_and_masks() -> None:
    one = nodefs.files(D, node(1, "2g"))
    assert one[D / "sys-node/node0/cpulist"] == "0\n"
    assert one[D / "sys-node/node0/cpumap"] == "1\n"
    many = nodefs.files(D, node(33, "64g"))
    assert many[D / "sys-node/node0/cpulist"] == "0-32\n"
    assert many[D / "sys-node/node0/cpumap"] == "1,ffffffff\n"


def test_links_point_every_cpu_at_the_real_sysfs_cpu() -> None:
    assert nodefs.links(D, node(2, "4g")) == {
        D / "sys-node/node0/cpu0": Path("/sys/devices/system/cpu/cpu0"),
        D / "sys-node/node0/cpu1": Path("/sys/devices/system/cpu/cpu1"),
    }


def test_mounts_are_four_volume_lines_each_on_its_own_line() -> None:
    assert nodefs.mounts(D) == (
        f"\nVolume={D}/meminfo:/proc/meminfo:ro"
        f"\nVolume={D}/sys-node:/sys/devices/system/node:ro"
        f"\nVolume={D}/kubelet-reserved.conf"
        ":/etc/kubernetes/kubelet.conf.d/10-tk8s-reserved.conf:ro"
        f"\nVolume={D}/kubelet-config-dir.conf"
        ":/etc/systemd/system/kubelet.service.d/20-tk8s-config-dir.conf:ro"
    )


def test_quantities_and_expected_allocatable() -> None:
    assert nodefs.cpu_milli("2") == 2000 and nodefs.cpu_milli("1500m") == 1500
    assert nodefs.quantity_bytes("4Gi") == 4 * 2**30
    assert nodefs.quantity_bytes("3084Mi") == 3084 * 2**20
    assert nodefs.quantity_bytes("32873260Ki") == 32873260 * 1024
    assert nodefs.quantity_bytes("123") == 123
    assert nodefs.expected_allocatable(node(2, "4g")) == (1500, 3084 * 2**20)
    assert nodefs.expected_allocatable(node(4, "6g")) == (3500, 5132 * 2**20)
