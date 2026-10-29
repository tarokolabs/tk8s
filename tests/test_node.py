from ipaddress import IPv4Address
from pathlib import Path

import pytest

from tests.fixtures import (
    OP,
    SHOW_HAPROXY,
    VBASH,
    argvs,
    bgp_router_answers,
    make_ctx,
    new_node_answers,
    phases_of,
    ready_host,
    two_router_user,
    vtysh_route,
)
from tk8s import lifecycle, model
from tk8s.create import units
from tk8s.host import FakeHost, Result
from tk8s.node import add as node_add
from tk8s.node import common as node_common
from tk8s.node import delete as node_delete
from tk8s.phases import Context, PhaseFailed, Runner
from tk8s.podman import node_volumes


def worker(n: int, ip: str, join: bool = True) -> model.Node:
    return model.Node(
        role="worker", name=f"demo-worker-{n}", ip=IPv4Address(ip), cpu=2, memory="4g", join=join
    )


def test_next_name_skips_past_the_highest_suffix(ctx: Context) -> None:
    c = ctx.require_cluster()  # demo-control-plane-1, demo-worker-1, demo-worker-2
    assert node_common.next_name(c, "worker") == "demo-worker-3"
    assert node_common.next_name(c, "control-plane") == "demo-control-plane-2"
    c = node_common.without_node(node_common.with_node(c, worker(3, "172.22.0.4")), "demo-worker-2")
    assert node_common.next_name(c, "worker") == "demo-worker-4"  # not by count: 2 is not reused


def test_next_ip_follows_the_highest_address(ctx: Context) -> None:
    c = ctx.require_cluster()
    assert node_common.next_ip(c) == IPv4Address("172.22.0.4")
    c = node_common.with_node(c, worker(3, "172.22.0.99"))
    assert node_common.next_ip(c) == IPv4Address("172.22.0.100")  # .100 is a node address now
    c = node_common.with_node(c, worker(4, "172.22.0.199"))
    with pytest.raises(
        lifecycle.ClusterError,
        match=r"no free node address left in 172.22.0.0/24 \(.200-.219 is the LB pool\)",
    ):
        node_common.next_ip(c)


def test_plan_add_records_the_node_as_not_joined(ctx: Context) -> None:
    """cluster.yaml says what is true: join: true is written by the record phase after kubeadm
    join and Ready, so an add interrupted in between describes the node as not joined."""
    c = ctx.require_cluster()
    assert node_common.plan_add(c, model.NodeRequest(role="worker")).join is False


def test_plan_add_inherits_the_size_of_the_first_node_of_the_role(ctx: Context) -> None:
    c = ctx.require_cluster()
    n = node_common.plan_add(c, model.NodeRequest(role="worker"))
    assert (n.name, str(n.ip), n.cpu, n.memory, n.join) == (
        "demo-worker-3",
        "172.22.0.4",
        2,
        "4g",
        False,  # join: true is the record phase's to write
    )
    req = model.NodeRequest(role="control-plane", cpu=4, memory="8g")
    n = node_common.plan_add(c, req)
    assert (n.name, n.cpu, n.memory, n.join) == ("demo-control-plane-2", 4, "8g", False)
    spec = c.spec.model_copy(update={"nodes": [c.first_control_plane]})
    only_cp = c.model_copy(update={"spec": spec})
    added = node_common.plan_add(only_cp, model.NodeRequest(role="worker"))
    assert added.cpu == c.first_control_plane.cpu


def test_plan_add_refuses_a_sixth_control_plane(ctx: Context) -> None:
    """The apiserver LB has five backend slots, the same limit create enforces."""
    c = ctx.require_cluster()
    for _ in range(2, 6):
        c = node_common.with_node(
            c, node_common.plan_add(c, model.NodeRequest(role="control-plane"))
        )
    assert sum(n.role == "control-plane" for n in c.spec.nodes) == 5
    with pytest.raises(lifecycle.ClusterError, match="at most 5"):
        node_common.plan_add(c, model.NodeRequest(role="control-plane"))
    node_common.plan_add(c, model.NodeRequest(role="worker"))  # workers are not capped


def test_without_node_refuses_the_first_control_plane_and_unknown_nodes(ctx: Context) -> None:
    c = ctx.require_cluster()
    with pytest.raises(
        lifecycle.ClusterError,
        match="the first control plane demo-control-plane-1 cannot be deleted; "
        "delete the cluster instead",
    ):
        node_common.without_node(c, "demo-control-plane-1")
    with pytest.raises(lifecycle.ClusterError, match="node demo-nope not found in cluster demo"):
        node_common.without_node(c, "demo-nope")
    c2 = node_common.without_node(c, "demo-worker-2")
    assert [n.name for n in c2.spec.nodes] == ["demo-control-plane-1", "demo-worker-1"]
    assert c2.spec.network == c.spec.network and c2.status == c.status


def test_marked_joined_flips_one_node(ctx: Context) -> None:
    c = node_common.with_node(ctx.require_cluster(), worker(3, "172.22.0.4", join=False))
    c2 = node_common.marked_joined(c, "demo-worker-3")
    found = node_common.find(c2, "demo-worker-3")
    assert found is not None and found.join is True
    assert [n.join for n in c2.spec.nodes[:3]] == [True, True, True]


def node_ctx(tmp_path: Path) -> tuple[Context, FakeHost]:
    """demo is Ready on the fake host (every create phase done); a node verb starts from there."""
    host = FakeHost()
    c = ready_host(host, tmp_path)
    assert c.status is not None
    c = c.model_copy(update={"status": c.status.model_copy(update={"phase": "Ready"})})
    host.files[tmp_path / "clusters" / "demo" / "cluster.yaml"] = model.dumps(c)
    ctx = make_ctx(host, tmp_path, user=None)
    ctx.cluster, ctx.name = c, "demo"
    host.podman.update(f"container:{n.name}" for n in c.spec.nodes)
    host.podman.update(f"volume:{v}" for n in c.spec.nodes for v in node_volumes(n.name))
    host.active.update(f"{n.name}.service" for n in c.spec.nodes)
    host.responses[("curl", "-sk", "--max-time", "3")] = Result(0, "ok", "")  # the LB is healthy
    return ctx, host


def test_add_worker_runs_plan_units_start_addons_join_in_order(tmp_path: Path) -> None:
    ctx, host = node_ctx(tmp_path)
    new_node_answers(host, "demo-worker-3", ready_after=1)
    ctx.request = model.NodeRequest(role="worker")
    Runner([node_add.add_phase(join=True)], record=False).run(ctx)
    assert phases_of(host) == [
        "add/plan",
        "add/images",
        "add/units",
        "add/start",
        "add/frr",
        "add/runtimeclass",
        "add/gvisor",
        "add/kubeadm",
        "add/ready",
        "add/record",
    ]
    assert ctx.node == "demo-worker-3"
    saved = model.load_text(host.files[ctx.state_dir / "cluster.yaml"])
    assert [n.name for n in saved.spec.nodes][-1] == "demo-worker-3" and saved.spec.nodes[-1].join
    assert saved.status is not None and saved.status.phase == "Ready"  # untouched
    unit = ctx.paths.unit_dir / "demo" / "demo-worker-3.container"
    assert (
        "IP=172.22.0.4" in host.files[unit]
        and "After=demo-control-plane-1.service" in host.files[unit]
    )
    assert "demo-worker-3.service" in host.files[ctx.paths.systemd_dir / "demo.target"]
    a = argvs(host)
    reload_ = a.index(list(VBASH))
    start = a.index(["systemctl", "start", "demo-worker-3.service"])
    assert a.index(["systemctl", "daemon-reload"]) < reload_ < start
    joins = [x for x in a if x[:5] == ["podman", "exec", "demo-worker-3", "bash", "-c"]]
    assert len(joins) == 1 and "--ignore-preflight-errors=SystemVerification" in joins[0][5]
    assert "--control-plane" not in joins[0][5]
    assert "[add/gvisor] already done" in host.lines  # not a --gvisor cluster
    assert "  demo-worker-3 Ready (worker)" in host.lines


def test_add_join_waits_for_the_node_object_to_register(tmp_path: Path) -> None:
    """kubeadm join returns before the kubelet has registered the Node; the phase must wait for
    `kubectl get node` instead of failing its own done() check a moment later."""
    ctx, host = node_ctx(tmp_path)
    new_node_answers(host, "demo-worker-3", ready_after=1)
    host.responses[("kubectl", "get", "node", "demo-worker-3")] = [
        Result(1, "", "not found"),  # done() before the phase runs
        Result(1, "", "not found"),  # right after kubeadm join: not registered yet
        Result(1, "", "not found"),
        Result(0, "demo-worker-3", ""),
    ]
    ctx.request = model.NodeRequest(role="worker")
    Runner([node_add.add_phase(join=True)], record=False).run(ctx)
    assert "  demo-worker-3 joined (worker)" in host.lines
    gets = [
        x
        for x in argvs(host)
        if x[:4] == ["kubectl", "get", "node", "demo-worker-3"] and len(x) == 4
    ]
    assert len(gets) >= 4


def test_add_start_is_not_done_until_the_router_lists_the_new_control_plane(
    tmp_path: Path,
) -> None:
    ctx, host = node_ctx(tmp_path)
    new_node_answers(host, "demo-control-plane-2", ready_after=1)
    ctx.request = model.NodeRequest(role="control-plane")
    node_add.plan_run(ctx)
    host.active.add("demo-control-plane-2.service")
    assert not node_add.start_done(ctx)
    two = SHOW_HAPROXY + "api                   demo-control-plane-2  DOWN       0 ms         4s\n"
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(0, two, "")
    assert node_add.start_done(ctx)


def test_add_control_plane_uploads_a_fresh_certificate_key(tmp_path: Path) -> None:
    ctx, host = node_ctx(tmp_path)
    new_node_answers(host, "demo-control-plane-2", ready_after=1)  # ready runs once, and untaints
    host.responses[
        ("podman", "exec", "demo-control-plane-1", "kubeadm", "init", "phase", "upload-certs")
    ] = Result(0, "[upload-certs] Using certificate key:\nfeedfacefeedface\n", "")
    ctx.request = model.NodeRequest(role="control-plane", cpu=4)
    two = SHOW_HAPROXY + "api                   demo-control-plane-2  DOWN       0 ms         4s\n"
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(0, two, "")
    Runner([node_add.add_phase(join=True)], record=False).run(ctx)
    join = next(
        x for x in argvs(host) if x[:5] == ["podman", "exec", "demo-control-plane-2", "bash", "-c"]
    )
    key = "--control-plane --certificate-key feedfacefeedface"
    assert f"{key} --apiserver-advertise-address 172.22.0.4" in join[5]
    assert host.files[ctx.state_dir / ".certificate-key"] == "feedfacefeedface\n"
    assert any(x[:3] == ["kubectl", "taint", "node"] for x in argvs(host))
    assert (
        "            server demo-control-plane-2 {"
        in host.files[ctx.state_dir / "router" / "tkctl.boot"]
    )


def test_add_no_join_stops_after_the_addons(tmp_path: Path) -> None:
    ctx, host = node_ctx(tmp_path)
    new_node_answers(host, "demo-worker-3", ready_after=0)
    ctx.request = model.NodeRequest(role="worker")
    Runner([node_add.add_phase(join=False)], record=False).run(ctx)
    assert phases_of(host)[-1] == "add/gvisor"
    saved = model.load_text(host.files[ctx.state_dir / "cluster.yaml"])
    assert saved.spec.nodes[-1].join is False
    assert not any(x[:5] == ["podman", "exec", "demo-worker-3", "bash", "-c"] for x in argvs(host))


def test_join_node_finishes_an_interrupted_add(tmp_path: Path) -> None:
    ctx, host = node_ctx(tmp_path)
    # recorded in cluster.yaml and started, never joined
    ctx.cluster = node_common.with_node(ctx.require_cluster(), worker(3, "172.22.0.4"))
    host.active.add("demo-worker-3.service")
    new_node_answers(host, "demo-worker-3", ready_after=2)
    ctx.node = "demo-worker-3"
    Runner([node_add.JOIN_PHASE], record=False).run(ctx)
    assert phases_of(host)[-3:] == ["join/kubeadm", "join/ready", "join/record"]
    assert "[join/start] already done" in host.lines  # the node was running
    assert "[join/record] already done" in host.lines  # join: true was the plan all along
    polls = [x for x in argvs(host) if x[:5] == ["kubectl", "get", "node", "demo-worker-3", "-o"]]
    assert len(polls) == 4  # done (False), the loop (False, True), done again (True)


def test_join_node_finishes_an_add_interrupted_before_the_unit_existed(tmp_path: Path) -> None:
    """plan wrote the entry, then the add died: join node renders the unit, starts the node,
    gives it the addons and joins it. Rerunning add would allocate a new node instead."""
    ctx, host = node_ctx(tmp_path)
    ctx.cluster = node_common.with_node(ctx.require_cluster(), worker(3, "172.22.0.4"))
    new_node_answers(host, "demo-worker-3", ready_after=1)
    ctx.node = "demo-worker-3"
    Runner([node_add.JOIN_PHASE], record=False).run(ctx)
    assert phases_of(host) == [
        "join/images",
        "join/units",
        "join/start",
        "join/frr",
        "join/runtimeclass",
        "join/gvisor",
        "join/kubeadm",
        "join/ready",
        "join/record",
    ]
    assert "IP=172.22.0.4" in host.files[ctx.paths.unit_dir / "demo" / "demo-worker-3.container"]
    assert ["systemctl", "start", "demo-worker-3.service"] in argvs(host)
    assert "  demo-worker-3 Ready (worker)" in host.lines


def test_delete_node_drains_stops_removes_and_rerenders(tmp_path: Path) -> None:
    ctx, host = node_ctx(tmp_path)
    host.responses = {
        ("kubectl", "get", "node", "demo-worker-2"): [
            Result(0, "demo-worker-2", ""),
            Result(1, "", "not found"),
        ],
        **host.responses,
    }
    ctx.node = "demo-worker-2"
    Runner([node_delete.PHASE], record=False).run(ctx)
    names = ("drain", "stop", "container", "volumes", "units", "entry")
    assert phases_of(host) == [f"delete/{p}" for p in names]
    a = argvs(host)
    drain_argv = [
        "kubectl",
        "drain",
        "demo-worker-2",
        "--ignore-daemonsets",
        "--delete-emptydir-data",
        "--timeout=120s",
    ]
    drain = a.index(drain_argv)
    reset = a.index(["podman", "exec", "demo-worker-2", "kubeadm", "reset", "-f"])
    gone = a.index(["kubectl", "delete", "node", "demo-worker-2"])
    stop = a.index(["systemctl", "stop", "demo-worker-2.service"])
    assert drain < reset < gone < stop < a.index(["podman", "rm", "-f", "demo-worker-2"])
    assert not any(x[3:] == [OP, "show", "haproxy"] for x in a[:gone])  # no apiserver on a worker
    for v in node_volumes("demo-worker-2"):
        assert ["podman", "volume", "rm", "-f", v] in a
    saved = model.load_text(host.files[ctx.state_dir / "cluster.yaml"])
    assert [n.name for n in saved.spec.nodes] == ["demo-control-plane-1", "demo-worker-1"]
    unit = ctx.paths.unit_dir / "demo" / "demo-worker-2.container"
    assert ["rm", "-f", str(unit)] in a and unit not in host.files
    assert "demo-worker-2.service" not in host.files[ctx.paths.systemd_dir / "demo.target"]
    assert a.index(list(VBASH)) > a.index(["rm", "-f", str(unit)])
    kept = ("demo-control-plane-1", "demo-worker-1")
    assert host.podman == {f"container:{n}" for n in kept} | {
        f"volume:{v}" for n in kept for v in node_volumes(n)
    }


def test_delete_node_resumes_after_the_unit_file_went(tmp_path: Path) -> None:
    """Interrupted right after rm of the unit file: the entry is still in cluster.yaml, so the
    verb finds the node; units re-renders without it and entry removes it last."""
    ctx, host = node_ctx(tmp_path)
    host.responses = {
        ("kubectl", "get", "node", "demo-worker-2"): Result(
            1, "", 'nodes "demo-worker-2" not found'
        ),
        **host.responses,
    }
    host.active.discard("demo-worker-2.service")
    host.podman -= {
        "container:demo-worker-2",
        *(f"volume:{v}" for v in node_volumes("demo-worker-2")),
    }
    del host.files[ctx.paths.unit_dir / "demo" / "demo-worker-2.container"]
    ctx.node = "demo-worker-2"
    Runner([node_delete.PHASE], record=False).run(ctx)
    done = [line for line in host.lines if line.endswith("already done")]
    assert len(done) == 4  # drain, stop, container, volumes
    assert "demo-worker-2.service" not in host.files[ctx.paths.systemd_dir / "demo.target"]
    saved = model.load_text(host.files[ctx.state_dir / "cluster.yaml"])
    assert [n.name for n in saved.spec.nodes] == ["demo-control-plane-1", "demo-worker-1"]


def test_drain_failure_is_reported_not_fatal(tmp_path: Path) -> None:
    ctx, host = node_ctx(tmp_path)
    host.responses = {
        ("kubectl", "get", "node", "demo-worker-2"): [
            Result(0, "demo-worker-2", ""),
            Result(1, "", "NotFound"),
        ],
        ("kubectl", "drain"): Result(
            1, "", "error: cannot evict pod x: violates PodDisruptionBudget"
        ),
        **host.responses,
    }
    ctx.node = "demo-worker-2"
    Runner([node_delete.PHASE], record=False).run(ctx)
    assert (
        "  drain of demo-worker-2 did not finish: error: cannot evict pod x: violates "
        "PodDisruptionBudget; removing it anyway"
    ) in host.lines
    assert ["kubectl", "delete", "node", "demo-worker-2"] in argvs(host)


def test_confirm_delete_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, host = node_ctx(tmp_path)
    ctx.node = "demo-worker-2"
    prompts: list[str] = []

    def answer(prompt: str) -> str:
        prompts.append(prompt)
        return "n"

    monkeypatch.setattr("builtins.input", answer)
    with pytest.raises(lifecycle.ClusterError, match=r"^aborted$"):
        node_delete.confirm_delete(ctx)
    assert prompts == ["Delete node demo-worker-2 from cluster demo? [y/N] "]
    assert host.lines == []


def test_delete_control_plane_waits_until_the_router_stops_routing_to_it(tmp_path: Path) -> None:
    """kubeadm reset takes the apiserver on that node down; the router keeps routing to it for a
    few seconds (haproxy's check, the health timer's withdrawal), and a kubectl delete node in
    that window can land on the dead one."""
    ctx, host = node_ctx(tmp_path)
    ctx.cluster = node_common.with_node(
        ctx.require_cluster(),
        model.Node(
            role="control-plane",
            name="demo-control-plane-2",
            ip=IPv4Address("172.22.0.4"),
            cpu=2,
            memory="4g",
        ),
    )
    host.responses = {
        ("kubectl", "get", "node", "demo-control-plane-2"): [
            Result(0, "demo-control-plane-2", ""),
            Result(1, "", "NotFound"),
        ],
        **host.responses,
    }
    host.responses[("curl", "-sk", "--max-time", "3")] = [  # the fixture's key, now a sequence
        Result(0, "", ""),
        Result(56, "", "EOF"),
        Result(0, "ok", ""),
    ]
    row = "api                   demo-control-plane-2  %s       0 ms         4s\n"
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = [
        Result(0, SHOW_HAPROXY + row % "UP", ""),
        Result(0, SHOW_HAPROXY + row % "UP", ""),
        Result(0, SHOW_HAPROXY + row % "DOWN", ""),
        Result(0, SHOW_HAPROXY, ""),  # delete/units: the reload took it out
    ]
    ctx.node = "demo-control-plane-2"
    Runner([node_delete.PHASE], record=False).run(ctx)
    a = argvs(host)
    reset = a.index(["podman", "exec", "demo-control-plane-2", "kubeadm", "reset", "-f"])
    gone = a.index(["kubectl", "delete", "node", "demo-control-plane-2"])
    shows = [i for i, x in enumerate(a) if x[3:] == [OP, "show", "haproxy"] and i < gone]
    polls = [i for i, x in enumerate(a) if x[:4] == ["curl", "-sk", "--max-time", "3"]]
    assert len(shows) == 3 and reset < shows[0] and shows[-1] < polls[0]
    assert len(polls) == 3 and polls[-1] < gone


def test_delete_control_plane_gives_up_when_the_router_keeps_routing_to_it(tmp_path: Path) -> None:
    ctx, host = node_ctx(tmp_path)
    ctx.cluster = node_common.with_node(
        ctx.require_cluster(),
        model.Node(
            role="control-plane",
            name="demo-control-plane-2",
            ip=IPv4Address("172.22.0.4"),
            cpu=2,
            memory="4g",
        ),
    )
    row = "api                   demo-control-plane-2  UP         0 ms         4s\n"
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(
        0, SHOW_HAPROXY + row, ""
    )
    ctx.node = "demo-control-plane-2"
    with pytest.raises(
        PhaseFailed,
        match=r"demo-router still sends apiserver connections to demo-control-plane-2 after "
        r"30 s \(podman exec demo-router /opt/vyatta/bin/vyatta-op-cmd-wrapper show haproxy\)",
    ):
        Runner([node_delete.PHASE], record=False).run(ctx)
    assert ["kubectl", "delete", "node", "demo-control-plane-2"] not in argvs(host)
    assert sum(1 for x in argvs(host) if x[3:] == [OP, "show", "haproxy"]) == 16  # 30 s / 2 s


def test_delete_units_is_not_done_while_the_router_still_lists_the_control_plane(
    tmp_path: Path,
) -> None:
    """A rerun after a failed reload must reload again, not leave VyOS with a stale backend."""
    ctx, host = node_ctx(tmp_path)
    ctx.cluster = node_common.with_node(
        ctx.require_cluster(),
        model.Node(
            role="control-plane",
            name="demo-control-plane-2",
            ip=IPv4Address("172.22.0.4"),
            cpu=2,
            memory="4g",
        ),
    )
    ctx.node = "demo-control-plane-2"
    units.run(ctx, node_delete.trimmed(ctx))  # the target and tkctl.boot without it, unit gone
    two = SHOW_HAPROXY + "api                   demo-control-plane-2  DOWN       0 ms         4s\n"
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(0, two, "")
    assert not node_delete.units_done(ctx)
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(
        0, SHOW_HAPROXY, ""
    )
    assert node_delete.units_done(ctx)


def test_delete_node_refuses_to_guess_when_the_apiserver_is_unreachable(tmp_path: Path) -> None:
    """A failed kubectl is not NotFound: a stopped or broken cluster must not lose a node's
    volumes (and a control plane's etcd membership) without kubeadm reset."""
    ctx, host = node_ctx(tmp_path)
    host.responses = {
        ("kubectl", "get", "node", "demo-worker-2"): Result(
            1, "", "The connection to the server 172.22.0.100:6443 was refused"
        ),
        **host.responses,
    }
    ctx.node = "demo-worker-2"
    with pytest.raises(lifecycle.ClusterError, match="cannot reach the apiserver of cluster demo"):
        Runner([node_delete.PHASE], record=False).run(ctx)
    assert not any(
        x[:2] == ["podman", "rm"] or x[:3] == ["podman", "volume", "rm"] for x in argvs(host)
    )


# ---- two routers


def two_router_disk(tmp_path: Path) -> tuple[Context, FakeHost]:
    """demo with two routers (3 control planes + 1 worker, bgp) is Ready on the fake host; both
    routers booted and route the VIP over the three control planes."""
    host = FakeHost()
    c = ready_host(host, tmp_path, user=two_router_user())
    assert c.status is not None
    c = c.model_copy(update={"status": c.status.model_copy(update={"phase": "Ready"})})
    host.files[tmp_path / "clusters" / "demo" / "cluster.yaml"] = model.dumps(c)
    ctx = make_ctx(host, tmp_path, user=None)
    ctx.cluster, ctx.name = c, "demo"
    host.podman.update(f"container:{n.name}" for n in c.spec.nodes)
    host.active.update(f"{n.name}.service" for n in c.spec.nodes)
    host.responses[("curl", "-sk", "--max-time", "3")] = Result(0, "ok", "")
    for r in ("demo-router-1", "demo-router-2"):
        bgp_router_answers(host, router=r)
        host.responses[("podman", "exec", "-i", r)] = Result(0, "", "")
    return ctx, host


def test_add_start_reloads_only_the_running_routers_and_says_why(tmp_path: Path) -> None:
    ctx, host = two_router_disk(tmp_path)
    ctx.request = model.NodeRequest(role="worker")
    node_add.plan_run(ctx)
    new_node_answers(host, "demo-worker-2", ready_after=0)
    host.podman.add("stopped:demo-router-2")
    Runner([node_add.START], record=False).run(ctx)
    assert (
        "  demo-router-2 is not running; it loads the new tkctl.boot when it starts" in host.lines
    )
    vbash = [a for a in argvs(host) if a[:3] == ["podman", "exec", "-i"] and "vbash" in " ".join(a)]
    assert [a[3] for a in vbash] == ["demo-router-1"]


def test_add_start_fails_at_once_when_no_router_is_running(tmp_path: Path) -> None:
    ctx, host = two_router_disk(tmp_path)
    ctx.request = model.NodeRequest(role="worker")
    node_add.plan_run(ctx)
    host.podman.update({"stopped:demo-router-1", "stopped:demo-router-2"})
    with pytest.raises(PhaseFailed, match=r"^no router of cluster demo is running$"):
        Runner([node_add.START], record=False).run(ctx)
    assert not any("vyos-config-status" in " ".join(a) for a in argvs(host))  # no 60 s wait


def test_delete_control_plane_waits_for_every_running_router_to_forget_it(tmp_path: Path) -> None:
    """router-1 drops the next hop at once, router-2 only on its third answer: kubectl delete node
    runs after router-2's third route query."""
    ctx, host = two_router_disk(tmp_path)
    host.responses = {
        ("kubectl", "get", "node", "demo-control-plane-2"): [
            Result(0, "demo-control-plane-2", ""),
            Result(1, "", "NotFound"),
        ],
        **host.responses,
    }
    bgp_router_answers(host, router="demo-router-2", hops=3)
    three = host.responses[vtysh_route("demo-router-2")]
    bgp_router_answers(host, router="demo-router-2", hops=1)
    one = host.responses[vtysh_route("demo-router-2")]
    bgp_router_answers(host, router="demo-router-1", hops=1)
    host.responses[vtysh_route("demo-router-2")] = [three, three, one]
    ctx.node = "demo-control-plane-2"
    Runner([node_delete.DRAIN], record=False).run(ctx)
    a = argvs(host)
    gone = a.index(["kubectl", "delete", "node", "demo-control-plane-2"])
    queries = [
        i
        for i, x in enumerate(a)
        if x[:3] == ["podman", "exec", "demo-router-2"] and "show ip route" in " ".join(x)
    ]
    assert len([i for i in queries if i < gone]) == 3


def test_delete_control_plane_names_the_routers_that_keep_routing_to_it(tmp_path: Path) -> None:
    ctx, host = two_router_disk(tmp_path)
    host.responses = {
        ("kubectl", "get", "node", "demo-control-plane-2"): Result(0, "demo-control-plane-2", ""),
        **host.responses,
    }
    ctx.node = "demo-control-plane-2"
    with pytest.raises(
        PhaseFailed,
        match=r"demo-router-1, demo-router-2 still send apiserver connections to "
        r"demo-control-plane-2 after 30 s \(podman exec demo-router-1 vtysh -c "
        r"'show ip route 172\.23\.0\.1'\)",
    ):
        Runner([node_delete.DRAIN], record=False).run(ctx)
    assert ["kubectl", "delete", "node", "demo-control-plane-2"] not in argvs(host)


def test_delete_node_removes_its_node_directory(tmp_path: Path) -> None:
    ctx, host = node_ctx(tmp_path)
    ctx.node = "demo-worker-2"
    d = ctx.state_dir / "nodes" / "demo-worker-2"
    host.dirs.add(d)
    assert not node_delete.units_done(ctx)
    node_delete.units_run(ctx)
    assert ["rm", "-rf", str(d)] in argvs(host)
    assert not host.exists(d)


def test_add_node_on_a_sized_cluster_refuses_memory_under_2g_before_saving(
    tmp_path: Path,
) -> None:
    ctx, host = node_ctx(tmp_path)
    before = host.files[ctx.state_dir / "cluster.yaml"]
    ctx.request = model.NodeRequest(role="worker", memory="1g")
    with pytest.raises(PhaseFailed, match=r'node memory "1g" is below the minimum 2G'):
        node_add.plan_run(ctx)
    assert host.files[ctx.state_dir / "cluster.yaml"] == before


def test_add_node_on_a_sized_cluster_refuses_a_node_bigger_than_the_host(
    tmp_path: Path,
) -> None:
    ctx, host = node_ctx(tmp_path)
    host.responses[("nproc",)] = Result(0, "8\n", "")
    host.files[Path("/proc/meminfo")] = "MemTotal:       32873260 kB\n"
    before = host.files[ctx.state_dir / "cluster.yaml"]
    ctx.request = model.NodeRequest(role="worker", cpu=9)
    with pytest.raises(PhaseFailed, match="node demo-worker-3 asks for 9 cpu, the host has 8"):
        node_add.plan_run(ctx)
    assert host.files[ctx.state_dir / "cluster.yaml"] == before


def test_add_node_on_an_old_cluster_keeps_todays_behaviour(tmp_path: Path) -> None:
    ctx, host = node_ctx(tmp_path)
    c = ctx.require_cluster()
    ctx.cluster = c.model_copy(update={"status": model.Status(phase="Ready", tk8s="old")})
    ctx.request = model.NodeRequest(role="worker", cpu=9, memory="1g")
    node_add.plan_run(ctx)
    saved = model.load_text(host.files[ctx.state_dir / "cluster.yaml"])
    assert saved.spec.nodes[-1].cpu == 9 and saved.spec.nodes[-1].memory == "1g"


def test_add_node_on_an_old_cluster_leaves_every_existing_unit_byte_identical(
    tmp_path: Path,
) -> None:
    ctx, host = node_ctx(tmp_path)
    c = ctx.require_cluster()
    ctx.cluster = c.model_copy(update={"status": model.Status(phase="Ready", tk8s="old")})
    units.run(ctx)  # what the old tkctl left on the host
    unit_dir = ctx.paths.unit_dir / "demo"
    before = {p: t for p, t in host.files.items() if p.parent == unit_dir}
    ctx.request = model.NodeRequest(role="worker")
    node_add.plan_run(ctx)
    units.run(ctx)
    for p, t in before.items():
        assert host.files[p] == t, p
    new = host.files[unit_dir / "demo-worker-3.container"]
    assert "/proc/meminfo" not in new and "kubelet.conf.d" not in new
    assert not any("/nodes/demo-worker-3/" in str(p) for p in host.files)


# ---- several hosts

from tests.fixtures import running_multi_answers  # noqa: E402


def multi_node_ctx(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> tuple[Context, dict[str, FakeHost]]:
    """The MULTI_YAML cluster Ready and running on its three hosts; a node verb starts there."""
    ctx, hosts = multi_ctx
    c = ctx.require_cluster()
    assert c.status is not None
    c = c.model_copy(update={"status": c.status.model_copy(update={"phase": "Ready"})})
    ctx.cluster = c
    ctx.save_cluster(c)
    units.run(ctx)
    running_multi_answers(hosts, ctx)
    for n in c.spec.nodes:
        h = hosts[n.host or "a"]
        h.podman.add(f"container:{n.name}")
        h.podman.update(f"volume:{v}" for v in node_volumes(n.name))
        h.active.add(f"{n.name}.service")
    hosts["a"].responses[("curl", "-sk", "--max-time", "3")] = Result(0, "ok", "")
    hosts["a"].responses[("kubectl", "get", "--raw", "/readyz")] = Result(0, "ok", "")
    hosts["a"].responses[("podman", "run", "--rm", "--entrypoint", "kubeadm")] = Result(
        0, f"registry.k8s.io/kube-apiserver:v{c.spec.kubernetes}\n", ""
    )
    hosts["a"].responses[("cilium", "install")] = Result(
        0, '      image: "quay.io/cilium/cilium:v1@sha256:a"\n', ""
    )
    return ctx, hosts


def test_add_node_defaults_to_the_least_loaded_host_and_the_next_free_address(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, _ = multi_node_ctx(multi_ctx)
    c = ctx.require_cluster()  # a: 2 nodes, b: 2, c: 1
    n = node_common.plan_add(c, model.NodeRequest(role="worker"))
    assert (n.name, n.host, str(n.ip)) == ("demo-worker-3", "c", "192.168.1.75")
    n = node_common.plan_add(c, model.NodeRequest(role="worker", host="a"))
    assert n.host == "a"
    full = node_common.with_node(c, n)
    with pytest.raises(
        lifecycle.ClusterError,
        match=r"no free node address left in 192.168.1.64-192.168.1.95 \(the last 20 are the "
        r"LoadBalancer pool\)",
    ):
        node_common.next_ip(full)


def test_add_node_host_unknown_or_on_single_host_is_refused(
    multi_ctx: tuple[Context, dict[str, FakeHost]], tmp_path: Path
) -> None:
    ctx, _ = multi_node_ctx(multi_ctx)
    with pytest.raises(
        lifecycle.ClusterError, match=r"host zz is not in cluster demo \(hosts: a, b, c\)"
    ):
        node_common.plan_add(ctx.require_cluster(), model.NodeRequest(role="worker", host="zz"))
    single, _ = node_ctx(tmp_path)
    with pytest.raises(lifecycle.ClusterError, match="--host only applies to a multi-host cluster"):
        node_common.plan_add(single.require_cluster(), model.NodeRequest(role="worker", host="a"))


def test_add_node_with_host_runs_units_and_start_there(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_node_ctx(multi_ctx)
    new_node_answers(hosts["a"], "demo-worker-3", ready_after=1)
    hosts["c"].responses[("podman", "exec", "demo-worker-3", "systemctl", "is-system-running")] = (
        Result(0, "running\n", "")
    )
    hosts["c"].responses[("nproc",)] = Result(0, "8\n", "")
    hosts["c"].files[Path("/proc/meminfo")] = "MemTotal:       32873260 kB\n"
    ctx.request = model.NodeRequest(role="worker", host="c")
    Runner([node_add.add_phase(join=False)], record=False).run(ctx)
    assert ctx.node == "demo-worker-3"
    unit = ctx.paths.unit_dir / "demo" / "demo-worker-3.container"
    assert unit in hosts["c"].files and unit not in hosts["a"].files
    assert "IP=192.168.1.75" in hosts["c"].files[unit]
    assert "--mac-address 02:54:4b:a8:01:4b" in hosts["c"].files[unit]
    assert "demo-worker-3.service" in hosts["c"].files[ctx.paths.systemd_dir / "demo.target"]
    a = argvs(hosts["c"])
    assert a.index(["systemctl", "daemon-reload"]) < a.index(
        ["systemctl", "start", "demo-worker-3.service"]
    )
    assert ["systemctl", "start", "demo-worker-3.service"] not in argvs(hosts["a"])
    for h in hosts.values():
        saved = model.load_text(h.files[ctx.state_dir / "cluster.yaml"])
        assert saved.spec.nodes[-1].host == "c"
    assert "  demo-worker-3: worker at 192.168.1.75 on c, 2 cpu, 4g" in hosts["a"].lines


def test_add_node_refuses_a_node_bigger_than_its_host(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_node_ctx(multi_ctx)
    hosts["c"].responses[("nproc",)] = Result(0, "2\n", "")
    hosts["c"].files[Path("/proc/meminfo")] = "MemTotal:       32873260 kB\n"
    before = hosts["a"].files[ctx.state_dir / "cluster.yaml"]
    ctx.request = model.NodeRequest(role="worker", host="c", cpu=4)
    with pytest.raises(PhaseFailed, match="node demo-worker-3 asks for 4 cpu, the host has 2"):
        node_add.plan_run(ctx)
    assert hosts["a"].files[ctx.state_dir / "cluster.yaml"] == before


def test_delete_node_removes_container_volumes_unit_and_dir_on_its_host(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_node_ctx(multi_ctx)
    hosts["a"].responses = {
        ("kubectl", "get", "node", "demo-worker-2"): [
            Result(0, "demo-worker-2", ""),
            Result(1, "", "not found"),
        ],
        **hosts["a"].responses,
    }
    d = ctx.state_dir / "nodes" / "demo-worker-2"
    ctx.node = "demo-worker-2"
    Runner([node_delete.PHASE], record=False).run(ctx)
    b = argvs(hosts["b"])
    assert ["systemctl", "stop", "demo-worker-2.service"] in b
    assert ["podman", "rm", "-f", "demo-worker-2"] in b
    assert ["podman", "volume", "rm", "-f", "demo-worker-2-var"] in b
    unit = ctx.paths.unit_dir / "demo" / "demo-worker-2.container"
    assert ["rm", "-f", str(unit)] in b and ["rm", "-rf", str(d)] in b
    assert ["systemctl", "daemon-reload"] in b
    assert not any(
        x[:2] in (["podman", "rm"], ["rm", "-f"], ["rm", "-rf"]) for x in argvs(hosts["a"])
    )
    assert "demo-worker-2.service" not in hosts["b"].files[ctx.paths.systemd_dir / "demo.target"]
    assert "container:demo-worker-2" not in hosts["b"].podman
    for h in hosts.values():
        saved = model.load_text(h.files[ctx.state_dir / "cluster.yaml"])
        assert "demo-worker-2" not in [n.name for n in saved.spec.nodes]
