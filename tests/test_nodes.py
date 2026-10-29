import pytest

from tests.fixtures import (
    OP,
    SHOW_HAPROXY,
    VTYSH_SUMMARY,
    argvs,
    bgp_router_answers,
    running_cluster_answers,
    running_multi_answers,
)
from tk8s.create import nodes
from tk8s.host import FakeHost, Result
from tk8s.phases import Context, PhaseFailed, Runner


def test_cni_plugins_downloaded_once_into_the_data_tree(ctx: Context, host: FakeHost) -> None:
    assert not nodes.cni_plugins_done(ctx)
    nodes.cni_plugins_run(ctx)
    v = ctx.versions.cni_plugins
    url = (
        f"https://github.com/containernetworking/plugins/releases/download/{v}/"
        f"cni-plugins-linux-amd64-{v}.tgz"
    )
    curl = next(c for c in host.calls if c.argv[0] == "curl")
    assert url in curl.argv and "--retry" in curl.argv
    tar = next(c for c in host.calls if c.argv[0] == "tar")
    assert tar.argv[:3] == ["tar", "-xz", "-C"] and str(ctx.paths.cni_dir) in tar.argv
    assert not tar.sudo
    host.files[ctx.paths.cni_dir / "bridge"] = "elf"
    assert nodes.cni_plugins_done(ctx)


def test_cni_plugins_offline_without_plugins_fails_by_name(ctx: Context, host: FakeHost) -> None:
    ctx.options.offline = True
    with pytest.raises(PhaseFailed, match=r"CNI plugins are not installed .* TK_OFFLINE=1"):
        Runner([nodes.PHASE], only="nodes/cni-plugins").run(ctx)
    assert not any(c.argv[0] == "curl" for c in host.calls)


def test_up_starts_the_target_and_waits_for_nodes_and_router(ctx: Context, host: FakeHost) -> None:
    running_cluster_answers(host, ctx)
    host.active.discard("demo.target")
    nodes.up_run(ctx)
    a = argvs(host)
    assert ["systemctl", "enable", "--now", "--quiet", "demo.target"] in a
    assert ["systemctl", "daemon-reload"] in a and ["systemctl", "reset-failed", "demo*"] in a
    assert ["mkdir", str(ctx.state_dir / "storage")] in a
    assert ["mkdir", str(ctx.state_dir / "logs")] in a
    assert ["mkdir", str(ctx.paths.wulin_dir), "owner=1000:1000"] in a
    assert not any(c[:2] == ["podman", "load"] for c in a)  # nothing is copied for the router
    assert "  demo-router: VyOS up, 1 control plane backend(s) at 172.22.0.253" in host.lines


def test_stale_volumes_of_a_new_node_are_removed(ctx: Context, host: FakeHost) -> None:
    running_cluster_answers(host, ctx)
    host.responses[("podman", "container", "exists")] = Result(1, "", "")  # nodes are new
    host.responses[("podman", "volume", "exists", "demo-control-plane-1-var")] = Result(0, "", "")
    host.responses[("podman", "volume", "exists")] = Result(1, "", "")
    nodes.up_run(ctx)
    a = argvs(host)
    assert ["podman", "volume", "rm", "-f", "demo-control-plane-1-var"] in a
    assert ["podman", "volume", "rm", "-f", "demo-worker-1-var"] not in a  # did not exist
    assert "  removed stale volume demo-control-plane-1-var" in host.lines


def test_stale_volumes_are_kept_when_a_kubeconfig_exists(ctx: Context, host: FakeHost) -> None:
    running_cluster_answers(host, ctx)
    host.files[ctx.state_dir / "kubeconfig"] = "kind: Config\n"
    host.responses[("podman", "container", "exists")] = Result(1, "", "")
    host.responses[("podman", "volume", "exists")] = Result(0, "", "")
    nodes.up_run(ctx)
    assert not any(c.argv[:3] == ["podman", "volume", "rm"] for c in host.calls)


def test_a_node_whose_systemd_never_comes_up_fails_by_name(ctx: Context, host: FakeHost) -> None:
    running_cluster_answers(host, ctx)
    host.responses[("podman", "exec", "demo-worker-2", "systemctl", "is-system-running")] = Result(
        1, "starting\n", ""
    )
    with pytest.raises(PhaseFailed, match="demo-worker-2 did not reach a running systemd state"):
        Runner([nodes.PHASE], only="nodes/up").run(ctx)


def test_router_that_never_lists_the_control_planes_fails_by_name(
    ctx: Context, host: FakeHost
) -> None:
    running_cluster_answers(host, ctx)
    ctx.options.poll = 0
    host.responses[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(
        0, SHOW_HAPROXY.split("api                   demo-control-plane-1")[0], ""
    )
    with pytest.raises(
        PhaseFailed, match=r"demo-router lists nothing as backends, cluster\.yaml has demo-control"
    ):
        Runner([nodes.PHASE], only="nodes/up").run(ctx)


def test_up_is_done_when_target_nodes_and_router_are_up(ctx: Context, host: FakeHost) -> None:
    running_cluster_answers(host, ctx)
    assert nodes.up_done(ctx)
    host.active.discard("demo.target")
    assert not nodes.up_done(ctx)
    host.active.add("demo.target")
    host.responses[("podman", "exec", "demo-control-plane-1", "systemctl", "is-system-running")] = (
        Result(1, "degraded\n", "")
    )
    assert nodes.up_done(ctx)  # degraded counts, like before
    host.responses[("podman", "exec", "demo-control-plane-1", "systemctl", "is-system-running")] = (
        Result(1, "starting\n", "")
    )
    assert not nodes.up_done(ctx)


def test_up_in_bgp_mode_is_done_once_the_router_lists_every_peer(
    bgp_ctx: Context, host: FakeHost
) -> None:
    bgp_router_answers(host, established=0)
    for n in (
        "demo-control-plane-1",
        "demo-control-plane-2",
        "demo-control-plane-3",
        "demo-worker-1",
    ):
        host.responses[("podman", "exec", n, "systemctl", "is-system-running")] = Result(
            0, "running\n", ""
        )
    host.active.add("demo.target")
    assert nodes.up_done(bgp_ctx)
    host.responses[VTYSH_SUMMARY] = Result(0, "{}", "")
    assert not nodes.up_done(bgp_ctx)


# ---- several hosts


def test_multi_host_cni_plugins_land_on_every_host(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    hosts["a"].files[ctx.paths.cni_dir / "bridge"] = "elf"
    assert not nodes.cni_plugins_done(ctx)  # b and c have none yet
    nodes.cni_plugins_run(ctx)
    for name in ("b", "c"):
        assert any(c.argv[0] == "curl" for c in hosts[name].calls), name
        hosts[name].files[ctx.paths.cni_dir / "bridge"] = "elf"
    assert not any(c.argv[0] == "curl" for c in hosts["a"].calls)  # a already had them
    assert nodes.cni_plugins_done(ctx)


def test_multi_host_up_starts_every_hosts_target_and_waits(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    running_multi_answers(hosts, ctx)
    for h in hosts.values():
        h.active.discard("demo.target")
    assert not nodes.up_done(ctx)
    nodes.up_run(ctx)
    for name, h in hosts.items():
        a = argvs(h)
        assert ["systemctl", "enable", "--now", "--quiet", "demo.target"] in a, name
        assert ["systemctl", "daemon-reload"] in a, name
        assert ["mkdir", str(ctx.state_dir / "storage")] in a, name
        assert ["mkdir", str(ctx.state_dir / "logs")] in a, name
    assert ["podman", "exec", "demo-control-plane-3", "systemctl", "is-system-running"] in argvs(
        hosts["c"]
    )
    assert "  demo-router-2: VyOS up, 3 control plane peer(s) at 192.168.1.66" in hosts["a"].lines
    assert nodes.up_done(ctx)
    hosts["b"].active.discard("demo.target")
    assert not nodes.up_done(ctx)


def test_multi_host_stale_volumes_are_removed_on_the_nodes_host(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    running_multi_answers(hosts, ctx)
    for h in hosts.values():
        h.responses[("podman", "container", "exists")] = Result(1, "", "")
        h.responses[("podman", "volume", "exists")] = Result(1, "", "")
    hosts["b"].responses = {  # the specific prefix before the general one
        ("podman", "volume", "exists", "demo-worker-2-var"): Result(0, "", ""),
        **hosts["b"].responses,
    }
    nodes.up_run(ctx)
    assert ["podman", "volume", "rm", "-f", "demo-worker-2-var"] in argvs(hosts["b"])
    assert not any(c.argv[:3] == ["podman", "volume", "rm"] for c in hosts["a"].calls)


def test_multi_host_up_starts_the_target_of_an_operator_that_runs_no_node(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    op = FakeHost()
    ctx.host = op
    running_multi_answers(hosts, ctx)
    op.responses[("id", "-u")] = Result(0, "1000\n", "")
    op.responses[("id", "-g")] = Result(0, "1000\n", "")
    for h in (*hosts.values(), op):
        h.active.discard("demo.target")
    nodes.up_run(ctx)
    a = argvs(op)
    assert ["systemctl", "daemon-reload"] in a
    assert ["systemctl", "enable", "--now", "--quiet", "demo.target"] in a
    assert not any(x[:3] == ["podman", "volume", "exists"] for x in a)  # no node here
    assert nodes.up_done(ctx)
    op.active.discard("demo.target")
    assert not nodes.up_done(ctx)
