from ipaddress import IPv4Address
from pathlib import Path

import pytest

from tests.fixtures import HOME, ready_host
from tk8s import lifecycle, model, node, node_delete, versions
from tk8s.delete import node_volumes
from tk8s.host import FakeHost, Result
from tk8s.paths import Paths
from tk8s.phases import Context, Options, Runner


def worker(n: int, ip: str, join: bool = True) -> model.Node:
    return model.Node(
        role="worker", name=f"demo-worker-{n}", ip=IPv4Address(ip), cpu=2, memory="4g", join=join
    )


def test_next_name_skips_past_the_highest_suffix(ctx: Context) -> None:
    c = ctx.require_cluster()  # demo-cp-1, demo-worker-1, demo-worker-2
    assert node.next_name(c, "worker") == "demo-worker-3"
    assert node.next_name(c, "control-plane") == "demo-cp-2"
    c = node.without_node(node.with_node(c, worker(3, "172.22.0.4")), "demo-worker-2")
    assert node.next_name(c, "worker") == "demo-worker-4"  # not by count: 2 is not reused


def test_next_ip_follows_the_highest_address_and_skips_the_lb(ctx: Context) -> None:
    c = ctx.require_cluster()
    assert node.next_ip(c) == IPv4Address("172.22.0.4")
    c = node.with_node(c, worker(3, "172.22.0.99"))
    assert node.next_ip(c) == IPv4Address("172.22.0.101")
    c = node.with_node(c, worker(4, "172.22.0.199"))
    with pytest.raises(
        lifecycle.ClusterError,
        match=r"no free node address left in 172.22.0.0/24 \(.200-.219 is the LB pool\)",
    ):
        node.next_ip(c)


def test_plan_add_inherits_the_size_of_the_first_node_of_the_role(ctx: Context) -> None:
    c = ctx.require_cluster()
    n = node.plan_add(c, model.NodeRequest(role="worker"))
    assert (n.name, str(n.ip), n.cpu, n.memory, n.join) == (
        "demo-worker-3",
        "172.22.0.4",
        2,
        "4g",
        True,
    )
    n = node.plan_add(c, model.NodeRequest(role="control-plane", cpu=4, memory="8g", join=False))
    assert (n.name, n.cpu, n.memory, n.join) == ("demo-cp-2", 4, "8g", False)
    spec = c.spec.model_copy(update={"nodes": [c.first_control_plane]})
    only_cp = c.model_copy(update={"spec": spec})
    assert node.plan_add(only_cp, model.NodeRequest(role="worker")).cpu == c.first_control_plane.cpu


def test_without_node_refuses_the_first_control_plane_and_unknown_nodes(ctx: Context) -> None:
    c = ctx.require_cluster()
    with pytest.raises(
        lifecycle.ClusterError,
        match="the first control plane demo-cp-1 cannot be deleted; delete the cluster instead",
    ):
        node.without_node(c, "demo-cp-1")
    with pytest.raises(lifecycle.ClusterError, match="node demo-nope not found in cluster demo"):
        node.without_node(c, "demo-nope")
    c2 = node.without_node(c, "demo-worker-2")
    assert [n.name for n in c2.spec.nodes] == ["demo-cp-1", "demo-worker-1"]
    assert c2.spec.network == c.spec.network and c2.status == c.status


def test_marked_joined_flips_one_node(ctx: Context) -> None:
    c = node.with_node(ctx.require_cluster(), worker(3, "172.22.0.4", join=False))
    c2 = node.marked_joined(c, "demo-worker-3")
    found = node.find(c2, "demo-worker-3")
    assert found is not None and found.join is True
    assert [n.join for n in c2.spec.nodes[:3]] == [True, True, True]


def node_ctx(tmp_path: Path) -> tuple[Context, FakeHost]:
    """demo is Ready on the fake host (every create phase done); a node verb starts from there."""
    host = FakeHost()
    c = ready_host(host, tmp_path)
    assert c.status is not None
    c = c.model_copy(update={"status": c.status.model_copy(update={"phase": "Ready"})})
    host.files[tmp_path / "clusters" / "demo" / "cluster.yaml"] = model.dumps(c)
    ctx = Context(
        host=host,
        paths=Paths(data_dir=tmp_path, wulin_dir=tmp_path / "wulin", home=HOME),
        versions=versions.load(HOME / "versions.yaml"),
        options=Options(poll=0, poll_slow=0, retry_delay=0),
        say=host.lines.append,
    )
    ctx.cluster, ctx.name = c, "demo"
    host.podman.update(f"container:{n.name}" for n in c.spec.nodes)
    host.podman.update(f"volume:{v}" for n in c.spec.nodes for v in node_volumes(n.name))
    host.active.update(f"{n.name}.service" for n in c.spec.nodes)
    return ctx, host


def new_node_answers(host: FakeHost, name: str, *, ready_after: int) -> None:
    """The new node boots, is not a Node object until kubeadm join ran, then turns Ready.
    Specific prefixes go first: FakeHost takes the first matching prefix."""
    host.responses = {
        ("kubectl", "get", "node", name, "-o"): [
            *([Result(0, "False", "")] * ready_after),
            Result(0, "True", ""),
        ],
        ("kubectl", "get", "node", name): [Result(1, "", "not found"), Result(0, name, "")],
        ("podman", "exec", name, "systemctl", "is-system-running"): Result(0, "running\n", ""),
        **host.responses,
    }


def phases_of(host: FakeHost) -> list[str]:
    return [line.split("]")[0][1:] for line in host.lines if line.startswith("[")]


def argvs(host: FakeHost) -> list[list[str]]:
    return [k.argv for k in host.calls]


def test_add_worker_runs_plan_units_start_addons_join_in_order(tmp_path: Path) -> None:
    ctx, host = node_ctx(tmp_path)
    new_node_answers(host, "demo-worker-3", ready_after=1)
    ctx.request = model.NodeRequest(role="worker")
    Runner([node.add_phase(join=True)], record=False).run(ctx)
    assert phases_of(host) == [
        "add/plan",
        "add/images",
        "add/units",
        "add/start",
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
    assert "IP=172.22.0.4" in host.files[unit] and "After=demo-cp-1.service" in host.files[unit]
    assert "demo-worker-3.service" in host.files[ctx.paths.systemd_dir / "demo.target"]
    a = argvs(host)
    reload_ = a.index(["podman", "kill", "--signal", "USR2", "demo-lb"])
    start = a.index(["systemctl", "start", "demo-worker-3.service"])
    assert a.index(["systemctl", "daemon-reload"]) < reload_ < start
    joins = [x for x in a if x[:5] == ["podman", "exec", "demo-worker-3", "bash", "-c"]]
    assert len(joins) == 1 and "--ignore-preflight-errors=SystemVerification" in joins[0][5]
    assert "--control-plane" not in joins[0][5]
    assert "[add/gvisor] already done" in host.lines  # not a --gvisor cluster
    assert "  demo-worker-3 Ready (worker)" in host.lines


def test_add_control_plane_uploads_a_fresh_certificate_key(tmp_path: Path) -> None:
    ctx, host = node_ctx(tmp_path)
    new_node_answers(host, "demo-cp-2", ready_after=1)  # ready runs once, and untaints
    host.responses[("podman", "exec", "demo-cp-1", "kubeadm", "init", "phase", "upload-certs")] = (
        Result(0, "[upload-certs] Using certificate key:\nfeedfacefeedface\n", "")
    )
    ctx.request = model.NodeRequest(role="control-plane", cpu=4)
    Runner([node.add_phase(join=True)], record=False).run(ctx)
    join = next(x for x in argvs(host) if x[:5] == ["podman", "exec", "demo-cp-2", "bash", "-c"])
    key = "--control-plane --certificate-key feedfacefeedface"
    assert f"{key} --apiserver-advertise-address 172.22.0.4" in join[5]
    assert host.files[ctx.state_dir / ".certificate-key"] == "feedfacefeedface\n"
    assert any(x[:3] == ["kubectl", "taint", "node"] for x in argvs(host))
    assert "server demo-cp-2 172.22.0.4:6443" in host.files[ctx.state_dir / "lb" / "haproxy.cfg"]


def test_add_no_join_stops_after_the_addons(tmp_path: Path) -> None:
    ctx, host = node_ctx(tmp_path)
    new_node_answers(host, "demo-worker-3", ready_after=0)
    ctx.request = model.NodeRequest(role="worker", join=False)
    Runner([node.add_phase(join=False)], record=False).run(ctx)
    assert phases_of(host)[-1] == "add/gvisor"
    saved = model.load_text(host.files[ctx.state_dir / "cluster.yaml"])
    assert saved.spec.nodes[-1].join is False
    assert not any(x[:5] == ["podman", "exec", "demo-worker-3", "bash", "-c"] for x in argvs(host))


def test_join_node_finishes_an_interrupted_add(tmp_path: Path) -> None:
    ctx, host = node_ctx(tmp_path)
    ctx.cluster = node.with_node(ctx.require_cluster(), worker(3, "172.22.0.4"))  # never joined
    host.active.add("demo-worker-3.service")
    new_node_answers(host, "demo-worker-3", ready_after=2)
    ctx.node = "demo-worker-3"
    Runner([node.JOIN_PHASE], record=False).run(ctx)
    assert phases_of(host) == ["join/kubeadm", "join/ready", "join/record"]
    assert "[join/record] already done" in host.lines  # join: true was the plan all along
    polls = [x for x in argvs(host) if x[:5] == ["kubectl", "get", "node", "demo-worker-3", "-o"]]
    assert len(polls) == 4  # done (False), the loop (False, True), done again (True)


def test_join_node_refuses_a_stopped_node(tmp_path: Path) -> None:
    ctx, host = node_ctx(tmp_path)
    ctx.cluster = node.with_node(ctx.require_cluster(), worker(3, "172.22.0.4", join=False))
    new_node_answers(host, "demo-worker-3", ready_after=0)
    ctx.node = "demo-worker-3"
    with pytest.raises(
        lifecycle.ClusterError,
        match=r"node demo-worker-3 is not running \(systemctl status demo-worker-3\)",
    ):
        Runner([node.JOIN_PHASE], record=False).run(ctx)


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
    names = ("drain", "stop", "container", "volumes", "entry", "units")
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
    for v in node_volumes("demo-worker-2"):
        assert ["podman", "volume", "rm", "-f", v] in a
    saved = model.load_text(host.files[ctx.state_dir / "cluster.yaml"])
    assert [n.name for n in saved.spec.nodes] == ["demo-cp-1", "demo-worker-1"]
    unit = ctx.paths.unit_dir / "demo" / "demo-worker-2.container"
    assert ["rm", "-f", str(unit)] in a and unit not in host.files
    assert "demo-worker-2.service" not in host.files[ctx.paths.systemd_dir / "demo.target"]
    assert a.index(["podman", "kill", "--signal", "USR2", "demo-lb"]) > a.index(
        ["rm", "-f", str(unit)]
    )
    kept = ("demo-cp-1", "demo-worker-1")
    assert host.podman == {f"container:{n}" for n in kept} | {
        f"volume:{v}" for n in kept for v in node_volumes(n)
    }


def test_delete_node_resumes_after_the_entry_is_gone(tmp_path: Path) -> None:
    ctx, host = node_ctx(tmp_path)
    ctx.cluster = node.without_node(ctx.require_cluster(), "demo-worker-2")  # removed last time
    host.responses = {
        ("kubectl", "get", "node", "demo-worker-2"): Result(1, "", "not found"),
        **host.responses,
    }
    host.active.discard("demo-worker-2.service")
    host.podman -= {
        "container:demo-worker-2",
        *(f"volume:{v}" for v in node_volumes("demo-worker-2")),
    }
    ctx.node = "demo-worker-2"
    Runner([node_delete.PHASE], record=False).run(ctx)
    done = [line for line in host.lines if line.endswith("already done")]
    assert len(done) == 5 and "[delete/units] drop the node's unit" in "\n".join(host.lines)
    assert (ctx.paths.unit_dir / "demo" / "demo-worker-2.container") not in host.files


def test_drain_failure_is_reported_not_fatal(tmp_path: Path) -> None:
    ctx, host = node_ctx(tmp_path)
    host.responses = {
        ("kubectl", "get", "node", "demo-worker-2"): [
            Result(0, "demo-worker-2", ""),
            Result(1, "", ""),
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
