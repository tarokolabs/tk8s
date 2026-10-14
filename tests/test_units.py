from pathlib import Path

from tk8s.create import units
from tk8s.host import FakeHost
from tk8s.phases import Context, Runner


def test_render_produces_every_file_in_write_order(ctx: Context) -> None:
    files = units.render(ctx)
    names = [p.name for p in files]
    assert names == [
        "demo.network",
        "haproxy.cfg",
        "demo-lb.container",
        "demo-cp-1.container",
        "demo-worker-1.container",
        "demo-worker-2.container",
        "demo.target",
        "demo-routes.service",
    ]
    paths = list(files)
    assert paths[1] == ctx.state_dir / "lb" / "haproxy.cfg"
    assert paths[0].parent == Path("/etc/containers/systemd/demo")
    assert paths[-1].parent == Path("/etc/systemd/system")


def test_control_plane_has_no_after_and_workers_wait_for_it(ctx: Context) -> None:
    files = units.render(ctx)
    cp = files[Path("/etc/containers/systemd/demo/demo-cp-1.container")]
    wk = files[Path("/etc/containers/systemd/demo/demo-worker-1.container")]
    assert "PartOf=demo.target\n\n[Container]" in cp and "After=" not in cp
    assert "PartOf=demo.target\nAfter=demo-cp-1.service\n" in wk


def test_haproxy_lists_one_server_per_control_plane(ctx: Context) -> None:
    cfg = units.render(ctx)[ctx.state_dir / "lb" / "haproxy.cfg"]
    assert cfg.endswith("    server demo-cp-1 172.22.0.1:6443\n")
    assert "server-template" not in cfg and "resolvers" not in cfg


def test_target_wants_lb_every_node_and_routes(ctx: Context) -> None:
    t = units.render(ctx)[Path("/etc/systemd/system/demo.target")]
    assert (
        "Wants=demo-lb.service demo-cp-1.service demo-worker-1.service demo-worker-2.service "
        "demo-routes.service"
    ) in t


def test_run_writes_etc_with_sudo_and_state_dir_without(ctx: Context, host: FakeHost) -> None:
    Runner([units.PHASE]).run(ctx)
    etc = [c for c in host.calls if c.argv[:2] == ["install", "-m"]]
    assert len(etc) == 7 and all(c.sudo for c in etc)  # 5 units + target + routes
    assert ctx.state_dir / "lb" / "haproxy.cfg" in host.files
    assert host.lines[-1] != "[units] already done"


def test_done_means_every_file_has_this_content(ctx: Context, host: FakeHost) -> None:
    assert not units.done(ctx)
    for p, content in units.render(ctx).items():
        host.files[p] = content
    assert units.done(ctx)
    host.files[Path("/etc/systemd/system/demo.target")] = "stale"
    assert not units.done(ctx)
