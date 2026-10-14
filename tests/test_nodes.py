import pytest

from tk8s.create import nodes
from tk8s.host import FakeHost, Result
from tk8s.phases import Context, PhaseFailed, Runner

STATS = (
    "# pxname,svname,qcur,qmax,scur,smax,slim,stot,bin,bout,dreq,dresp,ereq,econ,eresp,wretr,"
    "wredis,status\n"
    "control-planes,demo-control-plane-1,0,0,0,0,,0,0,0,,0,,0,0,0,0,DOWN\n"
    "control-planes,BACKEND,0,0,0,0,200,0,0,0,0,0,,0,0,0,0,DOWN\n"
)


def healthy(host: FakeHost, ctx: Context) -> None:
    host.responses[("id", "-u")] = Result(0, "1000\n", "")
    host.responses[("id", "-g")] = Result(0, "1000\n", "")
    host.responses[("podman", "exec", "demo-lb")] = Result(0, STATS, "")
    for n in ("demo-control-plane-1", "demo-worker-1", "demo-worker-2"):
        host.responses[("podman", "exec", n, "systemctl", "is-system-running")] = Result(
            0, "running\n", ""
        )
    host.active.add("demo.target")
    host.files[ctx.paths.cni_dir / "bridge"] = "elf"


def argvs(host: FakeHost) -> list[list[str]]:
    return [c.argv for c in host.calls]


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


def test_up_copies_the_lb_image_starts_the_target_and_waits(ctx: Context, host: FakeHost) -> None:
    healthy(host, ctx)
    host.active.discard("demo.target")
    host.responses[("podman", "image", "exists", ctx.versions.haproxy)] = [
        Result(1, "", ""),
        Result(0, "", ""),
    ]
    host.responses[("podman", "save")] = Result(0, "TAR", "")
    nodes.up_run(ctx)
    a = argvs(host)
    load_at = a.index(["podman", "load"])
    start_at = a.index(["systemctl", "enable", "--now", "--quiet", "demo.target"])
    assert load_at < start_at, "the LB image is in the host store before the target starts"
    assert ["systemctl", "daemon-reload"] in a and ["systemctl", "reset-failed", "demo*"] in a
    assert ["mkdir", str(ctx.state_dir / "storage")] in a
    assert ["mkdir", str(ctx.state_dir / "logs")] in a
    assert ["mkdir", str(ctx.paths.wulin_dir), "owner=1000:1000"] in a
    assert "  demo-lb: 1 control plane backend(s) at 172.22.0.100" in host.lines


def test_stale_volumes_of_a_new_node_are_removed(ctx: Context, host: FakeHost) -> None:
    healthy(host, ctx)
    host.responses[("podman", "container", "exists")] = Result(1, "", "")  # nodes are new
    host.responses[("podman", "volume", "exists", "demo-control-plane-1-var")] = Result(0, "", "")
    host.responses[("podman", "volume", "exists")] = Result(1, "", "")
    nodes.up_run(ctx)
    a = argvs(host)
    assert ["podman", "volume", "rm", "-f", "demo-control-plane-1-var"] in a
    assert ["podman", "volume", "rm", "-f", "demo-worker-1-var"] not in a  # did not exist
    assert "  removed stale volume demo-control-plane-1-var" in host.lines


def test_stale_volumes_are_kept_when_a_kubeconfig_exists(ctx: Context, host: FakeHost) -> None:
    healthy(host, ctx)
    host.files[ctx.state_dir / "kubeconfig"] = "kind: Config\n"
    host.responses[("podman", "container", "exists")] = Result(1, "", "")
    host.responses[("podman", "volume", "exists")] = Result(0, "", "")
    nodes.up_run(ctx)
    assert not any(c.argv[:3] == ["podman", "volume", "rm"] for c in host.calls)


def test_a_node_whose_systemd_never_comes_up_fails_by_name(ctx: Context, host: FakeHost) -> None:
    healthy(host, ctx)
    host.responses[("podman", "exec", "demo-worker-2", "systemctl", "is-system-running")] = Result(
        1, "starting\n", ""
    )
    with pytest.raises(PhaseFailed, match="demo-worker-2 did not reach a running systemd state"):
        Runner([nodes.PHASE], only="nodes/up").run(ctx)


def test_lb_that_never_lists_the_control_planes_fails_by_name(ctx: Context, host: FakeHost) -> None:
    healthy(host, ctx)
    host.responses[("podman", "exec", "demo-lb")] = Result(0, "# pxname,svname\n", "")
    with pytest.raises(PhaseFailed, match="demo-lb lists 0 of 1 control planes within 60 s"):
        Runner([nodes.PHASE], only="nodes/up").run(ctx)


def test_up_is_done_when_target_nodes_and_lb_are_up(ctx: Context, host: FakeHost) -> None:
    healthy(host, ctx)
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


def test_lb_backends_parses_the_stats_csv(ctx: Context, host: FakeHost) -> None:
    healthy(host, ctx)
    assert nodes.lb_backends(ctx) == 1
    stats = next(c for c in host.calls if c.argv[:3] == ["podman", "exec", "demo-lb"])
    assert stats.argv[3:5] == ["bash", "-c"] and "127.0.0.1/8404" in stats.argv[5]
