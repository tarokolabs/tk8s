from pathlib import Path

import pytest

from tk8s import delete, lifecycle
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
    host.podman.update(f"volume:{v}" for n in NODES for v in delete.node_volumes(n))
    host.podman.add("network:demo")
    # done looks once (there), run looks twice (there, there), done looks again (gone, repeats)
    there = Result(0, f"{c.spec.network.pods} via 172.22.0.1\n", "")
    host.responses[("ip", "route", "show")] = [there, there, there, Result(0, "", "")]


def argvs(host: FakeHost) -> list[list[str]]:
    return [k.argv for k in host.calls]


def test_a_full_delete_removes_everything_in_order(ctx: Context, host: FakeHost) -> None:
    everything_there(ctx, host)
    Runner([delete.PHASE], record=False).run(ctx)
    phases = [line.split("]")[0][1:] for line in host.lines if line.startswith("[")]
    names = ("stop", "containers", "volumes", "units", "network", "routes", "state")
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
        for v in delete.node_volumes(n):
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
    from tests.test_phases import make_ctx

    ctx, host = make_ctx(tmp_path)
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
    from tests.test_phases import make_ctx

    ctx, _ = make_ctx(tmp_path)
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
    from tests.test_phases import make_ctx

    ctx, host = make_ctx(tmp_path)
    ctx.name = "demo"
    unit_dir = ctx.paths.unit_dir / "demo"
    host.dirs.add(unit_dir)
    for f in ("demo.network", "demo-lb.container", "demo-control-plane-1.container"):
        host.files[unit_dir / f] = "x"
    host.podman.update({"container:demo-lb", "container:demo-control-plane-1"})
    assert delete.node_names(ctx) == ["demo-control-plane-1", "demo-lb"]
    Runner([delete.PHASE], record=False).run(ctx)
    assert ["podman", "rm", "-f", "demo-lb"] in argvs(host)
