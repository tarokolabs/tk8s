from pathlib import Path

import pytest

from tests.fixtures import argvs, make_ctx, two_router_user
from tests.test_kubeconfig import ADMIN as KUBEADM_ADMIN
from tk8s import delete, kubeconfig, lifecycle, podman
from tk8s.host import FakeHost, HostError, Result
from tk8s.phases import Context, PhaseFailed, Runner

NODES = ("demo-control-plane-1", "demo-worker-1", "demo-worker-2")


def everything_there(ctx: Context, host: FakeHost) -> None:
    """A complete demo on the host: units, target, containers, volumes, network, routes, state."""
    c = ctx.require_cluster()
    p = ctx.paths
    host.dirs.update({ctx.state_dir, p.unit_dir / "demo"})
    host.files[p.systemd_dir / "demo.target"] = "x"
    host.files[p.systemd_dir / "demo-routes.service"] = "x"
    host.active.update({"demo.target", "demo-network.service"})
    host.podman.update(f"container:{n}" for n in (*NODES, "demo-router"))
    host.podman.update(f"volume:{v}" for n in NODES for v in podman.node_volumes(n))
    host.podman.add("network:demo")
    # done looks once (there), run looks twice (there, there), done looks again (gone, repeats)
    there = Result(0, f"{c.spec.network.pods} via 172.22.0.1\n", "")
    host.responses[("ip", "route", "show")] = [there, there, there, Result(0, "", "")]


def test_a_full_delete_removes_everything_in_order(ctx: Context, host: FakeHost) -> None:
    everything_there(ctx, host)
    Runner([delete.PHASE], record=False).run(ctx)
    phases = [line.split("]")[0][1:] for line in host.lines if line.startswith("[")]
    names = ("stop", "containers", "volumes", "units", "network", "routes", "state", "kubeconfig")
    assert phases == [f"delete/{p}" for p in names]
    a = argvs(host)
    assert ["systemctl", "disable", "--now", "demo.target"] in a
    assert ["systemctl", "stop", "demo-network.service"] in a  # the oneshot stays active otherwise
    rm = next(x for x in a if x[:2] == ["rm", "-rf"] and "/etc/containers/systemd/demo" in x)
    assert "/etc/systemd/system/demo.target" in rm
    assert "/etc/systemd/system/demo-routes.service" in rm
    assert a.index(["systemctl", "daemon-reload"]) > a.index(rm)
    assert a.index(["podman", "rm", "-f", "demo-control-plane-1"]) < a.index(
        rm
    )  # nodes known before units go
    for n in (*NODES, "demo-router"):
        assert ["podman", "rm", "-f", n] in a
    for n in NODES:
        for v in podman.node_volumes(n):
            assert ["podman", "volume", "rm", "-f", v] in a
    assert ["podman", "network", "rm", "-f", "demo"] in a
    assert ["ip", "route", "del", "10.244.0.0/21"] in a
    assert ["ip", "route", "del", "10.98.0.0/24"] in a
    assert ["rm", "-rf", str(ctx.state_dir)] in a
    sudo = {tuple(k.argv[:2]) for k in host.calls if k.sudo}
    assert ("rm", "-rf") in sudo and ("podman", "rm") in sudo and ("ip", "route") in sudo
    assert host.podman == set() and not host.active and not host.exists(ctx.state_dir)


def test_nothing_left_means_every_phase_is_already_done(ctx: Context, host: FakeHost) -> None:
    Runner([delete.PHASE], record=False).run(ctx)
    assert all(line.endswith("already done") for line in host.lines if line.startswith("["))
    assert not any(x[:2] == ["rm", "-rf"] for x in argvs(host))


def test_rerun_after_a_failed_volume_finishes_the_delete(ctx: Context, host: FakeHost) -> None:
    everything_there(ctx, host)
    host.responses[("podman", "volume", "rm", "-f", "demo-worker-1-var")] = [
        HostError(["podman", "volume", "rm"], 125, "volume is being used"),
        Result(0, "", ""),
    ]
    with pytest.raises(PhaseFailed, match=r"\[delete/volumes\] failed: .*volume is being used"):
        Runner([delete.PHASE], record=False).run(ctx)
    assert ["podman", "network", "rm", "-f", "demo"] not in argvs(host)  # stopped there
    assert (
        "volume:demo-worker-1-var" in host.podman
        and "container:demo-control-plane-1" not in host.podman
    )
    host.lines.clear()
    Runner([delete.PHASE], record=False).run(ctx)
    assert "[delete/stop] already done" in host.lines
    assert "[delete/containers] already done" in host.lines
    assert "[delete/volumes] remove the node volumes" in "\n".join(host.lines)
    assert "[delete/units] remove the Quadlet units" in "\n".join(host.lines)
    assert ["podman", "network", "rm", "-f", "demo"] in argvs(host)


def test_without_cluster_yaml_the_units_name_the_nodes_to_remove(tmp_path: Path) -> None:
    """No cluster.yaml: the node names come from the unit directory, which must be read before
    the units phase removes it; otherwise the volumes survive and poison a same-name create."""
    host = FakeHost()
    ctx = make_ctx(host, tmp_path, user=None)
    ctx.name = "demo"
    unit_dir = ctx.paths.unit_dir / "demo"
    host.dirs.add(unit_dir)
    for f in (
        "demo.network",
        "demo-router.container",
        "demo-control-plane-1.container",
        "demo-worker-1.container",
    ):
        host.files[unit_dir / f] = "x"
    host.podman.update(
        {"container:demo-router", "container:demo-control-plane-1", "volume:demo-worker-1-var"}
    )
    assert delete.found(ctx)
    Runner([delete.PHASE], record=False).run(ctx)
    a = argvs(host)
    assert ["podman", "rm", "-f", "demo-router"] in a and [
        "podman",
        "rm",
        "-f",
        "demo-control-plane-1",
    ] in a
    assert ["podman", "volume", "rm", "-f", "demo-worker-1-var"] in a
    assert host.podman == set()
    assert "[delete/routes] already done" in host.lines  # no subnets known without the file


def test_not_found_when_nothing_is_there(tmp_path: Path) -> None:
    ctx = make_ctx(FakeHost(), tmp_path, user=None)
    ctx.name = "nothing"
    assert not delete.found(ctx)


def test_confirm_aborts_on_anything_but_y(ctx: Context, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    with pytest.raises(lifecycle.ClusterError, match=r"^aborted$"):
        delete.confirm_delete(ctx)

    def eof(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)  # stdin closed (a script): never a yes
    with pytest.raises(lifecycle.ClusterError, match=r"^aborted$"):
        delete.confirm_delete(ctx)
    monkeypatch.setattr("builtins.input", lambda prompt: "Y")
    delete.confirm_delete(ctx)
    ctx.options.assume_yes = True
    monkeypatch.setattr("builtins.input", eof)
    delete.confirm_delete(ctx)  # never asks


def test_fallback_removes_a_v2026_10_lb_container_like_a_node(tmp_path: Path) -> None:
    """A cluster from before the router (cluster.yaml with lb:, unit demo-lb.container) cannot be
    parsed; the unit-directory fallback lists the lb unit like a node so its container goes too."""
    host = FakeHost()
    ctx = make_ctx(host, tmp_path, user=None)
    ctx.name = "demo"
    unit_dir = ctx.paths.unit_dir / "demo"
    host.dirs.add(unit_dir)
    for f in ("demo.network", "demo-lb.container", "demo-control-plane-1.container"):
        host.files[unit_dir / f] = "x"
    host.podman.update({"container:demo-lb", "container:demo-control-plane-1"})
    assert delete.node_names(ctx) == ["demo-control-plane-1", "demo-lb"]
    Runner([delete.PHASE], record=False).run(ctx)
    assert ["podman", "rm", "-f", "demo-lb"] in argvs(host)


def test_delete_containers_names_both_routers_of_a_two_router_cluster(tmp_path: Path) -> None:
    host = FakeHost()
    ctx = make_ctx(host, tmp_path, user=two_router_user())
    ctx.name = "demo"
    names = delete.containers(ctx)
    assert "demo-router-1" in names and "demo-router-2" in names and "demo-router" not in names


def test_node_names_from_the_unit_dir_skips_every_router_unit(tmp_path: Path) -> None:
    """Without a readable cluster.yaml the unit directory says what exists: router units are not
    nodes (no volumes to remove), whether one or two of them."""
    host = FakeHost()
    ctx = make_ctx(host, tmp_path, user=None)
    ctx.name = "demo"
    unit_dir = ctx.paths.unit_dir / "demo"
    host.dirs.add(unit_dir)
    for f in (
        "demo.network",
        "demo-router-1.container",
        "demo-router-2.container",
        "demo-worker-1.container",
    ):
        host.files[unit_dir / f] = "x"
    assert delete.node_names(ctx) == ["demo-worker-1"]
    assert sorted(delete.containers(ctx)) == ["demo-router-1", "demo-router-2", "demo-worker-1"]


def test_delete_drops_the_cluster_from_kube_config(
    ctx: Context, host: FakeHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    demo = kubeconfig.rename(KUBEADM_ADMIN, "demo", "https://172.22.0.253:6443")
    other = kubeconfig.rename(KUBEADM_ADMIN, "other", "https://172.22.1.253:6443")
    config = home / ".kube" / "config"
    host.files[config] = kubeconfig.merge(kubeconfig.merge(None, other, "other"), demo, "demo")
    assert not delete.kubeconfig_done(ctx)
    delete.kubeconfig_run(ctx)
    doc = kubeconfig.load(host.files[config])
    assert [c["name"] for c in doc["contexts"]] == ["other"] and doc["current-context"] == ""
    assert delete.kubeconfig_done(ctx)
    assert "kubeconfig" in [p.name for p in delete.PHASE.children]


def test_delete_leaves_a_foreign_cluster_with_the_same_name_alone_and_keeps_a_backup(
    ctx: Context, host: FakeHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    foreign = (
        "apiVersion: v1\nkind: Config\nclusters:\n- cluster: {server: https://corp}\n  name: demo\n"
        "contexts:\n- context: {cluster: demo, user: demo-admin}\n  name: demo\n"
        "current-context: demo\nusers:\n- name: demo-admin\n  user: {token: t}\n"
    )
    config = home / ".kube" / "config"
    host.files[config] = foreign
    assert delete.kubeconfig_done(ctx)
    delete.kubeconfig_run(ctx)
    assert host.files[config] == foreign
    ours = kubeconfig.rename(KUBEADM_ADMIN, "demo", "https://172.22.0.253:6443")
    host.files[config] = kubeconfig.merge(None, ours, "demo")
    delete.kubeconfig_run(ctx)
    assert config not in host.files
    assert host.files[home / ".kube" / "config.bak"] == kubeconfig.merge(None, ours, "demo")


# ---- several hosts


def multi_everything_there(ctx: Context, hosts: dict[str, FakeHost]) -> None:
    """The MULTI_YAML cluster complete on its three hosts."""
    c, p = ctx.require_cluster(), ctx.paths
    there = Result(0, f"{c.spec.network.pods} via 192.168.1.70\n", "")
    for name, h in hosts.items():
        h.dirs.update({ctx.state_dir, p.unit_dir / "demo"})
        h.files[p.systemd_dir / "demo.target"] = "x"
        h.files[p.systemd_dir / "demo-routes.service"] = "x"
        h.active.update({"demo.target", "demo-network.service"})
        mine = [n.name for n in c.spec.nodes if n.host == name]
        h.podman.update(f"container:{n}" for n in mine)
        h.podman.update(f"container:{r.name}" for r in c.routers if r.host == name)
        h.podman.update(f"volume:{v}" for n in mine for v in podman.node_volumes(n))
        h.podman.add("network:demo")
        h.responses[("ip", "route", "show")] = [there, there, there, Result(0, "", "")]


def test_delete_multi_host_removes_everything_on_every_host(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    multi_everything_there(ctx, hosts)
    Runner([delete.PHASE], record=False).run(ctx)
    for name, h in hosts.items():
        a = argvs(h)
        assert ["systemctl", "disable", "--now", "demo.target"] in a, name
        assert ["podman", "network", "rm", "-f", "demo"] in a, name
        assert ["ip", "link", "del", "tk8s-demo"] in a, name
        assert ["ip", "route", "del", "10.244.0.0/21"] in a, name
        assert ["rm", "-rf", str(ctx.state_dir)] in a, name
        assert h.podman == set() and not h.active and not h.exists(ctx.state_dir), name
    assert ["podman", "rm", "-f", "demo-control-plane-3"] in argvs(hosts["c"])
    assert ["podman", "rm", "-f", "demo-control-plane-3"] not in argvs(hosts["a"])
    assert ["podman", "volume", "rm", "-f", "demo-worker-2-var"] in argvs(hosts["b"])
    assert ["podman", "rm", "-f", "demo-router-2"] in argvs(hosts["b"])
    assert "  host b:" in hosts["a"].lines


def test_delete_multi_host_resumes_per_host(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    multi_everything_there(ctx, hosts)
    b = hosts["b"]  # already clean
    b.podman.clear()
    b.active.clear()
    b.dirs.clear()
    b.files.clear()
    b.responses[("ip", "route", "show")] = Result(0, "", "")
    Runner([delete.PHASE], record=False).run(ctx)
    changes = (
        ["podman", "rm"],
        ["podman", "volume", "rm"],
        ["rm", "-rf"],
        ["systemctl", "disable"],
    )
    assert not any(x[: len(k)] == k for x in argvs(b) for k in changes)
    assert ["podman", "rm", "-f", "demo-control-plane-3"] in argvs(hosts["c"])


def test_delete_multi_host_without_cluster_yaml_cleans_this_host_only(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    multi_everything_there(ctx, hosts)
    ctx.cluster = None
    ctx.hosts = {}
    ctx.name = "demo"
    unit_dir = ctx.paths.unit_dir / "demo"
    hosts["a"].files[unit_dir / "demo.network"] = "Driver=macvlan\n"
    for u in ("demo-router-1", "demo-control-plane-1", "demo-worker-1"):
        hosts["a"].files[unit_dir / f"{u}.container"] = "x"
    assert delete.found(ctx)
    Runner([delete.PHASE], record=False).run(ctx)
    assert ["podman", "rm", "-f", "demo-control-plane-1"] in argvs(hosts["a"])
    assert not hosts["b"].calls and not hosts["c"].calls
    assert any("cleaning this host only" in ln for ln in hosts["a"].lines)


def test_single_host_delete_sequence_is_unchanged(ctx: Context, host: FakeHost) -> None:
    everything_there(ctx, host)
    Runner([delete.PHASE], record=False).run(ctx)
    assert not any(x[:2] == ["ip", "link"] for x in argvs(host))
    assert not any(ln.startswith("  host ") for ln in host.lines)
