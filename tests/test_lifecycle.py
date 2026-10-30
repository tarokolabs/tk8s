from pathlib import Path

import pytest

from tests.fixtures import HOME, OP, SHOW_HAPROXY, bgp_router_answers, vrrp_answers
from tests.test_kubeconfig import ADMIN as KUBEADM_ADMIN
from tk8s import kubeconfig, lifecycle, model, versions
from tk8s.create import units
from tk8s.create.plan import resolve
from tk8s.host import FakeHost, Result
from tk8s.paths import Paths
from tk8s.phases import Context, Options


def on_disk(ctx: Context, host: FakeHost) -> model.Cluster:
    """demo's cluster.yaml on disk (list and describe read the data dir directly) and in the fake
    host (writes go through it)."""
    c = ctx.require_cluster()
    state = ctx.paths.state_dir(c.name)
    state.mkdir(parents=True)
    text = model.dumps(c)
    (state / "cluster.yaml").write_text(text)
    host.files[state / "cluster.yaml"] = text
    return model.load(state / "cluster.yaml")


def test_load_cluster_names_the_missing_cluster(ctx: Context) -> None:
    with pytest.raises(
        lifecycle.ClusterError, match=r"cluster nope not found under .* \(tkctl get clusters\)"
    ):
        lifecycle.load_cluster(ctx, "nope")


def test_a_cluster_without_status_is_creating_not_ready(ctx: Context, host: FakeHost) -> None:
    """A cluster.yaml from before status existed: no marker file makes it Ready any more."""
    c = ctx.require_cluster().model_copy(update={"status": None})
    state = ctx.paths.state_dir(c.name)
    state.mkdir(parents=True)
    (state / "cluster.yaml").write_text(model.dumps(c))
    (state / ".create-complete").write_text("")
    loaded = lifecycle.load_cluster(ctx, "demo")
    assert loaded.status is None and lifecycle.state_of(ctx, loaded) == "Creating"
    assert ctx.cluster is loaded and ctx.name == "demo"


def test_save_cluster_writes_the_file_and_keeps_ctx_in_step(ctx: Context, host: FakeHost) -> None:
    on_disk(ctx, host)
    c = lifecycle.load_cluster(ctx, "demo")
    lifecycle.save_cluster(ctx, c.model_copy(update={"status": None}))
    state = ctx.paths.state_dir("demo")
    assert "status:" not in host.files[state / "cluster.yaml"] and ctx.cluster is not c
    assert not any(k.argv[:2] == ["rm", "-f"] for k in host.calls)


def test_list_shows_every_cluster_and_its_state(ctx: Context, host: FakeHost) -> None:
    on_disk(ctx, host)
    broken = ctx.paths.clusters_dir / "broken"
    broken.mkdir()
    (broken / "cluster.yaml").write_text("metadata: {name: broken}\nspec: {nodes: []}\n")
    lifecycle.list_clusters(ctx)
    out = "\n".join(host.lines)
    header = ["NAME", "NODES", "K8S", "DATAPATH", "STATE", "HOSTS", "MESH", "SUBNET"]
    assert out.splitlines()[0].split() == header
    assert (
        "demo         1+2     1.37.2   auto      Creating  -      -            172.22.0.0/24" in out
    )
    assert "broken       " in out and "cluster.yaml" in out  # an unreadable file hides nothing


def test_list_with_no_clusters(ctx: Context, host: FakeHost) -> None:
    lifecycle.list_clusters(ctx)
    assert host.lines == ["no clusters"]


def test_list_names_a_directory_without_cluster_yaml(ctx: Context, host: FakeHost) -> None:
    """A create interrupted before plan leaves the directory and nothing else; hidden, it makes
    a second create of that name fail on a clash the student cannot see."""
    (ctx.paths.clusters_dir / "half").mkdir(parents=True)
    lifecycle.list_clusters(ctx)
    out = "\n".join(host.lines)
    assert "no clusters" not in out
    line = next(ln for ln in host.lines if ln.startswith("half"))
    assert "Incomplete" in line and "tkctl delete cluster half" in line


def test_describe_sections(ctx: Context, host: FakeHost) -> None:
    c = on_disk(ctx, host)
    lifecycle.describe(ctx, c, None)
    out = "\n".join(host.lines)
    for s in (
        "Name:        demo",
        "State:       Creating",
        "Kubernetes:  1.37.2   cni: cilium   datapath: auto   gvisor: false",
        "router 172.22.0.253",
        "Nodes:",
        "  demo-worker-1 ",
        "172.22.0.2",
        "2 cpu, 4g",
        " joined",
        "Status:",
        "  phase:      Creating",
        "  next:       preflight   (rerun tkctl create cluster demo)",
        "Addons:",
        "  unavailable (cluster is not running)",
        "Access:",
        "  apiserver:  https://172.22.0.253:6443 (VyOS haproxy in demo-router; every node "
        "and this host go through it)",
        "sudo ip route add 172.22.0.0/24 via <host-ip>",
        "Paths:",
        "/etc/containers/systemd/demo/",
    ):
        assert s in out, s
    assert "dns 172" not in out
    i = host.lines.index("Router:")
    assert host.lines[i + 1 : i + 5] == [
        "  mode:     proxy (VyOS load-balancing haproxy, TCP check on every control plane)",
        "  address:  172.22.0.253 (api.demo.k8s resolves to it on every node)",
        "  shell:    sudo podman exec -it demo-router vbash",
        "  backends: unavailable (cluster is not running)",
    ]


def test_describe_running_cluster_asks_kubectl_with_the_cluster_kubeconfig(
    ctx: Context, host: FakeHost
) -> None:
    c = ctx.require_cluster()
    assert c.status is not None
    ctx.cluster = c.model_copy(update={"status": c.status.model_copy(update={"phase": "Ready"})})
    c = on_disk(ctx, host)
    host.active.add("demo.target")
    r = host.responses
    r[("kubectl", "--request-timeout=5s", "-n", "kube-system", "get", "deploy")] = Result(
        0, "registry.k8s.io/metrics-server/metrics-server:v0.9.1", ""
    )
    r[("kubectl", "--request-timeout=5s", "-n", "local-path-storage")] = Result(1, "", "nope")
    r[("kubectl", "--request-timeout=5s", "get", "runtimeclass")] = Result(1, "", "nope")
    r[("ip", "-4", "route", "get")] = Result(
        0, "1.1.1.1 via 192.168.1.1 dev eth0 src 192.168.1.230 uid 1000\n", ""
    )
    lifecycle.describe(ctx, c, None)
    out = "\n".join(host.lines)
    assert "State:       Running" in out
    assert "  metrics-server  v0.9.1" in out and "  local-path      not installed" in out
    assert "  runtimeclass    none" in out and "Paths:" in out  # a failing kubectl never aborts
    assert "ip route add 172.22.0.0/24 via 192.168.1.230" in out
    kube = [k for k in host.calls if k.argv[0] == "kubectl"]
    assert kube and all(k.env == {"KUBECONFIG": str(ctx.state_dir / "kubeconfig")} for k in kube)


def test_describe_shows_what_create_recorded(ctx: Context, host: FakeHost) -> None:
    from datetime import UTC, datetime

    c = ctx.require_cluster()
    assert c.status is not None
    done = {
        "preflight": datetime(2026, 10, 14, 8, 2, 19, tzinfo=UTC),
        "plan": datetime(2026, 10, 14, 8, 2, 20, tzinfo=UTC),
    }
    ctx.cluster = c.model_copy(update={"status": c.status.model_copy(update={"completed": done})})
    c = on_disk(ctx, host)
    lifecycle.describe(ctx, c, None)
    out = "\n".join(host.lines)
    assert "  plan " in out and "2026-10-14 08:02:20Z" in out
    assert "  next:       images   (rerun tkctl create cluster demo)" in out


def test_describe_yaml_prints_the_file(ctx: Context, host: FakeHost) -> None:
    c = on_disk(ctx, host)
    lifecycle.describe(ctx, c, "yaml")
    assert host.lines[0].startswith("apiVersion: taroko.io/v1alpha1") and "status:" in host.lines[0]


def test_stop_names_every_unit_at_once(ctx: Context, host: FakeHost) -> None:
    lifecycle.stop(ctx, ctx.require_cluster())
    stops = [k for k in host.calls if k.argv[:2] == ["systemctl", "stop"]]
    assert len(stops) == 1 and stops[0].sudo
    assert stops[0].argv[2:] == [
        "demo.target",
        "demo-router.service",
        "demo-control-plane-1.service",
        "demo-worker-1.service",
        "demo-worker-2.service",
    ]
    assert host.lines[-1] == "cluster demo stopped"


def test_start_waits_for_fresh_ready_heartbeats(ctx: Context, host: FakeHost) -> None:
    stale = (
        "demo-control-plane-1 True 2000-01-01T00:00:00Z\ndemo-worker-1 True 2000-01-01T00:00:00Z\n"
        "demo-worker-2 True 2000-01-01T00:00:00Z\n"
    )
    fresh = stale.replace("2000-01-01", "2999-01-01")
    host.responses[("kubectl", "get", "nodes", "-o")] = [Result(0, stale, ""), Result(0, fresh, "")]
    host.responses[("kubectl", "get", "nodes", "--no-headers")] = Result(
        0,
        "demo-control-plane-1 Ready control-plane 1d v1.37.2\n"
        "demo-worker-1 Ready <none> 1d v1.37.2\n",
        "",
    )
    units.run(ctx)  # a created cluster has its node files on the host
    lifecycle.start(ctx, ctx.require_cluster())
    argvs = [k.argv for k in host.calls]
    assert ["systemctl", "start", "demo.target"] in argvs
    assert sum(1 for a in argvs if a[:4] == ["kubectl", "get", "nodes", "-o"]) == 2
    assert host.lines[-2:] == ["  demo-control-plane-1 Ready", "  demo-worker-1 Ready"]


def test_use_merges_the_cluster_into_kube_config_and_keeps_the_previous_file(
    ctx: Context, host: FakeHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    demo = kubeconfig.rename(KUBEADM_ADMIN, "demo", "https://172.22.0.253:6443")
    host.files[ctx.state_dir / "kubeconfig"] = demo
    lifecycle.use(ctx, ctx.require_cluster())
    config = home / ".kube" / "config"
    assert kubeconfig.load(host.files[config])["current-context"] == "demo"
    assert (home / ".kube" / "config.bak") not in host.files  # nothing to back up the first time
    writes = [k for k in host.calls if k.argv[0] == "write" and k.argv[1] == str(config)]
    assert writes and writes[0].argv[2] == "mode=600"
    assert host.lines[-1] == "~/.kube/config: current context demo (contexts: demo)"
    # a second cluster joins the file instead of replacing it
    other = ctx.require_cluster().model_copy(update={"metadata": model.Metadata(name="other")})
    host.files[ctx.paths.state_dir("other") / "kubeconfig"] = kubeconfig.rename(
        KUBEADM_ADMIN, "other", "https://172.22.1.253:6443"
    )
    ctx.cluster = other
    lifecycle.use(ctx, other)
    doc = kubeconfig.load(host.files[config])
    assert [c["name"] for c in doc["contexts"]] == ["demo", "other"]
    assert doc["current-context"] == "other"
    assert (
        host.files[home / ".kube" / "config.bak"]
        == host.files[config].replace("current-context: other", "current-context: demo")
        or kubeconfig.load(host.files[home / ".kube" / "config.bak"])["current-context"] == "demo"
    )
    assert host.lines[-1] == "~/.kube/config: current context other (contexts: demo, other)"


def test_use_refuses_to_clobber_a_kube_config_it_cannot_parse(
    ctx: Context, host: FakeHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    host.files[ctx.state_dir / "kubeconfig"] = kubeconfig.rename(
        KUBEADM_ADMIN, "demo", "https://172.22.0.253:6443"
    )
    host.files[home / ".kube" / "config"] = "just: text\n"
    with pytest.raises(lifecycle.ClusterError, match="not a kubeconfig"):
        lifecycle.use(ctx, ctx.require_cluster())
    assert host.files[home / ".kube" / "config"] == "just: text\n"


def test_use_without_a_kubeconfig_says_so(
    ctx: Context, host: FakeHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    with pytest.raises(lifecycle.ClusterError, match="has no kubeconfig yet"):
        lifecycle.use(ctx, ctx.require_cluster())


def test_describe_next_is_the_phase_after_the_last_recorded_one(
    ctx: Context, host: FakeHost
) -> None:
    """preflight runs before cluster.yaml exists and is never recorded; `next` must not point
    at it forever. Phases that were skipped as already done are not recorded either."""
    from datetime import UTC, datetime

    c = ctx.require_cluster()
    assert c.status is not None
    done = {"plan": datetime(2026, 10, 14, 8, 2, 20, tzinfo=UTC)}
    ctx.cluster = c.model_copy(update={"status": c.status.model_copy(update={"completed": done})})
    lifecycle.describe(ctx, on_disk(ctx, host), None)
    assert "  next:       images   (rerun tkctl create cluster demo)" in "\n".join(host.lines)
    host.lines.clear()
    done = {"plan": done["plan"], "kubeadm/init": datetime(2026, 10, 14, 8, 3, 0, tzinfo=UTC)}
    ctx.cluster = c.model_copy(update={"status": c.status.model_copy(update={"completed": done})})
    lifecycle.describe(ctx, ctx.cluster, None)
    assert "  next:       kubeadm/kubeconfig   (rerun" in "\n".join(host.lines)


def test_stop_survives_a_node_recorded_without_a_unit(ctx: Context, host: FakeHost) -> None:
    """An add that died after plan leaves a node in cluster.yaml with no unit; stop cluster must
    still stop the rest (systemctl stops what it can and exits non-zero for the unknown unit)."""
    host.responses[("systemctl", "stop")] = Result(
        5, "", "Failed to stop demo-worker-2.service: Unit demo-worker-2.service not loaded."
    )
    lifecycle.stop(ctx, ctx.require_cluster())
    assert host.lines[-1] == "cluster demo stopped"


def test_describe_running_cluster_lists_the_router_backends(ctx: Context, host: FakeHost) -> None:
    c = ctx.require_cluster()
    assert c.status is not None
    ctx.cluster = c.model_copy(update={"status": c.status.model_copy(update={"phase": "Ready"})})
    c = on_disk(ctx, host)
    host.active.add("demo.target")
    r = host.responses
    r[("kubectl", "--request-timeout=5s")] = Result(1, "", "nope")
    r[("ip", "-4", "route", "get")] = Result(
        0, "1.1.1.1 via 192.168.1.1 dev eth0 src 10.0.0.9\n", ""
    )
    r[("podman", "exec", "demo-router", OP, "show", "haproxy")] = Result(0, SHOW_HAPROXY, "")
    lifecycle.describe(ctx, c, None)
    assert "  backends: demo-control-plane-1 UP" in host.lines


ROUTE_GET = Result(0, "1.1.1.1 via 192.168.1.1 dev eth0 src 10.0.0.9\n", "")


def test_describe_bgp_cluster_router_section(bgp_ctx: Context, host: FakeHost) -> None:
    c = bgp_ctx.require_cluster()
    assert c.status is not None
    bgp_ctx.cluster = c.model_copy(
        update={"status": c.status.model_copy(update={"phase": "Ready"})}
    )
    c = on_disk(bgp_ctx, host)
    host.active.add("demo.target")
    host.responses[("kubectl", "--request-timeout=5s")] = Result(1, "", "nope")
    host.responses[("ip", "-4", "route", "get")] = ROUTE_GET
    bgp_router_answers(host, established=2, hops=2)
    lifecycle.describe(bgp_ctx, c, None)
    out = "\n".join(host.lines)
    assert "lb-ipam 172.22.0.201-172.22.0.219  router 172.22.0.253  vip 172.23.0.1" in out
    i = host.lines.index("Router:")
    section = [line for line in host.lines[i + 1 :] if not line.startswith("  $ ")]  # no echoes
    assert section[:5] == [
        "  mode:     bgp (VyOS BGP AS 65000, ECMP over every control plane announcing the VIP)",
        "  address:  172.22.0.253 (api.demo.k8s -> 172.23.0.1, the VIP)",
        "  shell:    sudo podman exec -it demo-router vbash",
        "  peers:    demo-control-plane-1 Established, demo-control-plane-2 Established, "
        "demo-control-plane-3 Active",
        "  nexthops: 2 to 172.23.0.1 (show ip route 172.23.0.1)",
    ]
    assert (
        "  apiserver:  https://172.23.0.1:6443 (anycast VIP; demo-router spreads it over the "
        "control planes announcing it)"
    ) in host.lines


def test_describe_stopped_bgp_cluster_says_peers_unavailable(
    bgp_ctx: Context, host: FakeHost
) -> None:
    c = on_disk(bgp_ctx, host)
    host.responses[("ip", "-4", "route", "get")] = ROUTE_GET
    lifecycle.describe(bgp_ctx, c, None)
    assert "  peers:    unavailable (cluster is not running)" in host.lines


def test_describe_single_control_plane_bgp_cluster_notes_no_ecmp(
    paths: Paths, host: FakeHost
) -> None:
    vers = versions.load(HOME / "versions.yaml")
    user = model.UserCluster(
        metadata=model.Metadata(name="demo"),
        spec=model.UserSpec(
            network=model.UserNetwork(ha="bgp"), nodes=[model.NodeGroup(role="control-plane")]
        ),
    )
    ctx = Context(host=host, paths=paths, versions=vers, options=Options(), say=host.lines.append)
    ctx.cluster = resolve(
        user,
        k8s_default=vers.kubernetes.default,
        existing=None,
        used_indices=set(),
        allow_overlap=False,
        tk8s_version="test",
    )
    c = on_disk(ctx, host)
    host.responses[("ip", "-4", "route", "get")] = ROUTE_GET
    lifecycle.describe(ctx, c, None)
    assert "  note:     one control plane: nothing to spread; --control-planes 3 shows ECMP" in (
        host.lines
    )


def test_describe_bgp_cluster_tells_other_machines_to_route_the_vip_too(
    bgp_ctx: Context, host: FakeHost
) -> None:
    c = on_disk(bgp_ctx, host)
    host.responses[("ip", "-4", "route", "get")] = ROUTE_GET
    lifecycle.describe(bgp_ctx, c, None)
    assert (
        "  from another machine: sudo ip route add 172.22.0.0/24 via 10.0.0.9 && "
        "sudo ip route add 172.23.0.1/32 via 10.0.0.9   (and 10.98.0.0/24 for ClusterIPs)"
    ) in host.lines


# ---- two routers


def ready_on_disk(ctx: Context, host: FakeHost) -> model.Cluster:
    """A Ready bgp cluster on disk, running, kubectl away, the host's default route known."""
    c = ctx.require_cluster()
    assert c.status is not None
    ctx.cluster = c.model_copy(update={"status": c.status.model_copy(update={"phase": "Ready"})})
    c = on_disk(ctx, host)
    host.active.add("demo.target")
    host.responses[("kubectl", "--request-timeout=5s")] = Result(1, "", "nope")
    host.responses[("ip", "-4", "route", "get")] = ROUTE_GET
    return c


def test_describe_two_router_cluster_lists_both_routers_and_their_vrrp_state(
    two_router_ctx: Context, host: FakeHost
) -> None:
    c = ready_on_disk(two_router_ctx, host)
    for r in ("demo-router-1", "demo-router-2"):
        bgp_router_answers(host, router=r)
    vrrp_answers(host, {"demo-router-1": "MASTER", "demo-router-2": "BACKUP"})
    lifecycle.describe(two_router_ctx, c, None)
    i = host.lines.index("Router:")
    section = [line for line in host.lines[i + 1 :] if not line.startswith("  $ ")]
    assert section[:7] == [
        "  mode:     bgp (VyOS BGP AS 65000, ECMP over every control plane announcing the VIP)",
        "  address:  172.22.0.253 (api.demo.k8s -> 172.23.0.1, the VIP; VRRP floats it between "
        "the two routers)",
        "  routers:  demo-router-1 172.22.0.251 MASTER, demo-router-2 172.22.0.252 BACKUP",
        "  shell:    sudo podman exec -it demo-router-1 vbash   (demo-router-2 likewise)",
        "  stop:     sudo systemctl stop demo-router-1.service   (podman stop would be "
        "restarted by systemd)",
        "  peers:    demo-control-plane-1 Established, demo-control-plane-2 Established, "
        "demo-control-plane-3 Established (demo-router-1) / demo-control-plane-1 Established, "
        "demo-control-plane-2 Established, demo-control-plane-3 Established (demo-router-2)",
        "  nexthops: 3 to 172.23.0.1 on demo-router-1, 3 on demo-router-2 "
        "(show ip route 172.23.0.1)",
    ]
    assert (
        "  apiserver:  https://172.23.0.1:6443 (anycast VIP; demo-router-1 and demo-router-2 "
        "spread it over the control planes announcing it)"
    ) in host.lines


def test_describe_one_router_cluster_has_no_routers_or_stop_line(
    bgp_ctx: Context, host: FakeHost
) -> None:
    c = ready_on_disk(bgp_ctx, host)
    bgp_router_answers(host)
    lifecycle.describe(bgp_ctx, c, None)
    assert not any(line.startswith(("  routers:", "  stop:")) for line in host.lines)


def test_stop_names_both_router_units(two_router_ctx: Context, host: FakeHost) -> None:
    """systemctl waits only for the units it is given: with two routers both must be named, and
    the single-router name (which does not exist) must not be."""
    lifecycle.stop(two_router_ctx, two_router_ctx.require_cluster())
    stops = [k for k in host.calls if k.argv[:2] == ["systemctl", "stop"]]
    assert stops[0].argv[2:5] == ["demo.target", "demo-router-1.service", "demo-router-2.service"]
    assert "demo-router.service" not in stops[0].argv


def test_describe_says_not_running_for_a_stopped_router(
    two_router_ctx: Context, host: FakeHost
) -> None:
    """A router stopped with systemctl has no VRRP state and no peers to list; the section says
    so in those words instead of 'unavailable' and 'none listed'."""
    c = ready_on_disk(two_router_ctx, host)
    bgp_router_answers(host, router="demo-router-1")
    vrrp_answers(host, {"demo-router-1": "MASTER"})
    host.podman.add("stopped:demo-router-2")
    lifecycle.describe(two_router_ctx, c, None)
    assert (
        "  routers:  demo-router-1 172.22.0.251 MASTER, demo-router-2 172.22.0.252 not running"
        in host.lines
    )
    peers = next(line for line in host.lines if line.startswith("  peers:"))
    assert peers.endswith(" (demo-router-1) / not running (demo-router-2)")
    hops = "  nexthops: 3 to 172.23.0.1 on demo-router-1, not running on demo-router-2 "
    assert hops + "(show ip route 172.23.0.1)" in host.lines
    assert not any(
        a[:3] == ["podman", "exec", "demo-router-2"] for a in [k.argv for k in host.calls]
    )


def test_start_refuses_when_a_node_file_podman_would_mount_is_missing(
    ctx: Context, host: FakeHost
) -> None:
    units.run(ctx)
    meminfo = ctx.state_dir / "nodes" / "demo-worker-1" / "meminfo"
    host.files.pop(meminfo)
    with pytest.raises(lifecycle.ClusterError) as err:
        lifecycle.start(ctx, ctx.require_cluster())
    assert str(meminfo) in str(err.value) and "tkctl create cluster demo" in str(err.value)
    assert ["systemctl", "start", "demo.target"] not in [k.argv for k in host.calls]


def test_use_normalises_a_state_kubeconfig_from_before_the_renaming(
    ctx: Context, host: FakeHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cluster created before entries were named after it still has kubeadm's names in
    <state>/kubeconfig; use must not point current-context at a context that is not there."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    legacy = KUBEADM_ADMIN.replace("https://api.demo.k8s:6443", "https://172.22.0.253:6443")
    host.files[ctx.state_dir / "kubeconfig"] = legacy
    lifecycle.use(ctx, ctx.require_cluster())
    doc = kubeconfig.load(host.files[home / ".kube" / "config"])
    assert [c["name"] for c in doc["contexts"]] == ["demo"] and doc["current-context"] == "demo"
    assert doc["clusters"][0]["cluster"]["server"] == "https://172.22.0.253:6443"


# ---- several hosts


def multi_on_disk(ctx: Context, hosts: dict[str, FakeHost]) -> model.Cluster:
    c = ctx.require_cluster()
    state = ctx.paths.state_dir(c.name)
    state.mkdir(parents=True, exist_ok=True)
    (state / "cluster.yaml").write_text(model.dumps(c))
    for h in hosts.values():
        h.files[state / "cluster.yaml"] = model.dumps(c)
        h.responses[("hostname",)] = Result(0, "op\n", "")
    return c


def test_load_cluster_builds_the_hosts_and_refuses_a_copy_managed_elsewhere(
    multi_ctx: tuple[Context, dict[str, FakeHost]], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, hosts = multi_ctx
    multi_on_disk(ctx, hosts)
    built: dict[str, FakeHost] = {"a": hosts["a"], "b": FakeHost(label="b"), "c": FakeHost()}
    monkeypatch.setattr("tk8s.hosts.build", lambda *a, **k: built)
    ctx.hosts = {}
    c = lifecycle.load_cluster(ctx, "demo")
    assert c.multi_host and ctx.hosts is built
    hosts["a"].responses[("hostname",)] = Result(0, "someone-else\n", "")
    with pytest.raises(
        lifecycle.ClusterError, match="cluster demo is managed from op; run tkctl there"
    ):
        lifecycle.load_cluster(ctx, "demo")


def test_state_of_multi_host_is_running_stopped_or_degraded(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    c = ctx.require_cluster().model_copy(
        update={"status": model.Status(phase="Ready", tk8s="t", operator="op")}
    )
    assert lifecycle.state_of(ctx, c) == "Stopped"
    for h in hosts.values():
        h.active.add("demo.target")
    assert lifecycle.state_of(ctx, c) == "Running"
    hosts["c"].active.discard("demo.target")
    assert lifecycle.state_of(ctx, c) == "Degraded"


def test_get_clusters_shows_the_host_count_and_managed_from(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    multi_on_disk(ctx, hosts)
    lifecycle.list_clusters(ctx)
    out = "\n".join(hosts["a"].lines)
    header = out.splitlines()[0].split()
    assert header == ["NAME", "NODES", "K8S", "DATAPATH", "STATE", "HOSTS", "MESH", "SUBNET"]
    assert (
        "demo         3+2     1.37.2   auto      Creating  3      -            192.168.1.0/24"
        in out
    )
    hosts["a"].responses[("hostname",)] = Result(0, "pve-b\n", "")
    hosts["a"].lines.clear()
    lifecycle.list_clusters(ctx)
    out = "\n".join(hosts["a"].lines)
    row = "demo         3+2     1.37.2   auto      managed   3      -            192.168.1.0/24"
    assert f"{row} from op" in out


def test_single_host_list_prints_a_dash_for_hosts(ctx: Context, host: FakeHost) -> None:
    on_disk(ctx, host)
    lifecycle.list_clusters(ctx)
    assert (
        "demo         1+2     1.37.2   auto      Creating  -      -            172.22.0.0/24"
        in "\n".join(host.lines)
    )


def test_describe_lists_hosts_and_the_host_of_every_node(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    ready = model.Status(phase="Ready", tk8s="test", operator="op")
    ctx.cluster = ctx.require_cluster().model_copy(update={"status": ready})
    c = multi_on_disk(ctx, hosts)
    hosts["b"].active.add("demo.target")
    hosts["a"].responses[("kubectl",)] = Result(1, "", "")
    lifecycle.describe(ctx, c, None)
    out = hosts["a"].lines
    i = out.index("Hosts:")
    assert out[i + 1 : i + 5] == [
        "  NAME     SSH                   IFACE    TARGET",
        "  a        (this host)           ens18    inactive",
        "  b        tux@192.168.1.231     ens18    active",
        "  c        192.168.1.232         eth0     inactive",
    ]
    assert (
        "  NAME                     ROLE           IP             RESOURCES      JOIN        HOST"
        in out
    )
    assert any(ln.startswith("  demo-control-plane-2 ") and ln.endswith(" b") for ln in out)
    text = "\n".join(out)
    assert "  routers:  demo-router-1 192.168.1.65 on a, demo-router-2 192.168.1.66 on b" in text
    assert "  nodes and LoadBalancer IPs (192.168.1.85-192.168.1.89) are on the LAN" in text
    assert (
        "             range 192.168.1.64-192.168.1.70,192.168.1.80-192.168.1.89  shim 192.168.1.67"
        in text
    )
    assert "from another machine" not in text and "State:       Degraded" in text


def test_stop_and_start_touch_every_host(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, hosts = multi_ctx
    c = ctx.require_cluster()
    units.run(ctx)
    lifecycle.stop(ctx, c)
    stops = {
        n: [k.argv for k in h.calls if k.argv[:2] == ["systemctl", "stop"]]
        for n, h in hosts.items()
    }
    assert stops["a"] == [
        [
            "systemctl",
            "stop",
            "demo.target",
            "demo-router-1.service",
            "demo-control-plane-1.service",
            "demo-worker-1.service",
        ]
    ]
    assert stops["c"] == [["systemctl", "stop", "demo.target", "demo-control-plane-3.service"]]
    fresh = "".join(f"{n.name} True 2999-01-01T00:00:00Z\n" for n in c.spec.nodes)
    hosts["a"].responses[("kubectl", "get", "nodes", "-o")] = Result(0, fresh, "")
    hosts["a"].responses[("kubectl", "get", "nodes", "--no-headers")] = Result(0, "x Ready\n", "")
    lifecycle.start(ctx, c)
    for n, h in hosts.items():
        assert ["systemctl", "start", "demo.target"] in [k.argv for k in h.calls], n
    del hosts["c"].files[ctx.state_dir / "nodes" / "demo-control-plane-3" / "meminfo"]
    with pytest.raises(lifecycle.ClusterError, match="files its nodes mount are missing"):
        lifecycle.start(ctx, c)


def test_load_cluster_keeps_the_callers_quiet_setting(
    multi_ctx: tuple[Context, dict[str, FakeHost]], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, hosts = multi_ctx
    multi_on_disk(ctx, hosts)
    seen: dict[str, object] = {}

    def build(*a: object, **k: object) -> dict[str, FakeHost]:
        seen.update(k)
        return hosts

    monkeypatch.setattr("tk8s.hosts.build", build)
    ctx.hosts = {}
    lifecycle.load_cluster(ctx, "demo")
    assert seen["quiet"] is False  # the `$ ssh …` lines show like the local ones
    assert hosts["a"].quiet is False


def test_get_clusters_judges_a_multi_host_cluster_by_every_host(
    multi_ctx: tuple[Context, dict[str, FakeHost]], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, hosts = multi_ctx
    ready = model.Status(phase="Ready", tk8s="test", operator="op")
    ctx.cluster = ctx.require_cluster().model_copy(update={"status": ready})
    multi_on_disk(ctx, hosts)
    monkeypatch.setattr("tk8s.hosts.build", lambda *a, **k: hosts)
    ctx.hosts = {}
    ctx.cluster = None  # get clusters starts from nothing: every row is its own cluster
    hosts["b"].active.add("demo.target")  # the operator's own target is down
    lifecycle.list_clusters(ctx)
    assert (
        "demo         3+2     1.37.2   auto      Degraded  3      -            192.168.1.0/24"
        in "\n".join(hosts["a"].lines)
    )


def test_stop_and_start_include_an_operator_that_runs_no_node(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    ctx, _ = multi_ctx
    op = FakeHost()
    ctx.host = op
    c = ctx.require_cluster()
    units.run(ctx)
    lifecycle.stop(ctx, c)
    assert ["systemctl", "stop", "demo.target"] in [k.argv for k in op.calls]
    fresh = "".join(f"{n.name} True 2999-01-01T00:00:00Z\n" for n in c.spec.nodes)
    op.responses[("kubectl", "get", "nodes", "-o")] = Result(0, fresh, "")
    op.responses[("kubectl", "get", "nodes", "--no-headers")] = Result(0, "x Ready\n", "")
    lifecycle.start(ctx, c)
    assert ["systemctl", "start", "demo.target"] in [k.argv for k in op.calls]


def test_an_unreachable_host_reads_as_down_not_as_a_crash(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    """ssh exit 255 raises in general (an unreachable host is never "nothing there"), but the
    read-only verbs have to describe an outage, not die in it."""
    from tk8s.host import HostError

    ctx, hosts = multi_ctx
    ready = model.Status(phase="Ready", tk8s="test", operator="op")
    c = ctx.require_cluster().model_copy(update={"status": ready})
    ctx.cluster = c
    multi_on_disk(ctx, hosts)
    for h in hosts.values():
        h.active.add("demo.target")
    hosts["c"].responses[("systemctl", "is-active")] = HostError(["ssh"], 255, "No route to host")
    hosts["c"].responses[("true",)] = HostError(["ssh"], 255, "No route to host")
    assert lifecycle.state_of(ctx, c) == "Degraded"
    hosts["a"].responses[("kubectl",)] = Result(1, "", "")
    lifecycle.describe(ctx, c, None)
    rows = [ln for ln in hosts["a"].lines if ln.startswith("  c        192.168.1.232")]
    assert rows and rows[0].endswith("unreachable")
    hosts["a"].lines.clear()
    lifecycle.list_clusters(ctx)
    assert "Degraded" in "\n".join(hosts["a"].lines)


def test_stop_and_start_skip_an_unreachable_host_and_name_it(
    multi_ctx: tuple[Context, dict[str, FakeHost]],
) -> None:
    from tk8s.host import HostError

    ctx, hosts = multi_ctx
    c = ctx.require_cluster()
    units.run(ctx)
    down = HostError(["ssh"], 255, "No route to host")
    hosts["b"].responses = {("systemctl",): down, **hosts["b"].responses}
    with pytest.raises(lifecycle.ClusterError, match="host b unreachable; stopped c, a"):
        lifecycle.stop(ctx, c)
    stops_c = [k.argv for k in hosts["c"].calls if k.argv[:2] == ["systemctl", "stop"]]
    assert stops_c == [["systemctl", "stop", "demo.target", "demo-control-plane-3.service"]]
    fresh = "".join(f"{n.name} True 2999-01-01T00:00:00Z\n" for n in c.spec.nodes)
    hosts["a"].responses[("kubectl", "get", "nodes", "-o")] = Result(0, fresh, "")
    hosts["a"].responses[("kubectl", "get", "nodes", "--no-headers")] = Result(0, "x Ready\n", "")
    with pytest.raises(lifecycle.ClusterError, match="host b unreachable; started c, a"):
        lifecycle.start(ctx, c)
    assert ["systemctl", "start", "demo.target"] in [k.argv for k in hosts["c"].calls]


def mesh_on_disk(ctx: Context, host: FakeHost, **status: object) -> model.Cluster:
    c = ctx.require_cluster()
    assert c.status is not None
    st = c.status.model_copy(update={"phase": "Ready", **status})
    ctx.cluster = c.model_copy(update={"status": st})
    return on_disk(ctx, host)


def test_describe_mesh_section_on_a_stopped_cluster(ctx: Context, host: FakeHost) -> None:
    c = mesh_on_disk(ctx, host, cluster_id=1, cilium="1.20.4")
    lifecycle.describe(ctx, c, None)
    i = host.lines.index("Mesh:")
    assert host.lines[i + 1 : i + 7] == [
        "  peers:      none",
        "  endpoint:   172.22.0.200 (clustermesh-apiserver)",
        "  global services:",
        "    unavailable (cluster is not running)",
        "  entrypoints:",
        "    unavailable (cluster is not running)",
    ]
    assert host.lines[i + 7] == "Access:"


def test_describe_mesh_section_lists_global_services_and_entrypoints_when_running(
    ctx: Context, host: FakeHost
) -> None:
    from tests.test_mesh_services import CILIUM_DBG, DBG_JSON, SVC_LIST

    c = mesh_on_disk(ctx, host, mesh=["other"], cluster_id=1, cilium="1.20.4")
    host.active.add("demo.target")
    host.responses[SVC_LIST] = Result(0, "default/echo 10.98.0.20 true\n", "")
    host.responses[CILIUM_DBG] = Result(0, DBG_JSON, "")
    host.responses[("kubectl", "get", "gateway", "-A", "-o")] = Result(
        0, "default/gw 172.22.0.205\n", ""
    )
    host.responses[("kubectl", "get", "svc", "-A", "--field-selector")] = Result(
        0, "default/db 172.22.0.206\n", ""
    )
    lifecycle.describe(ctx, c, None)
    assert "  peers:      other" in host.lines
    assert "    default/echo             local 1  remote 1" in host.lines
    assert "    gateway  default/gw      172.22.0.205" in host.lines
    assert "    service  default/db      172.22.0.206" in host.lines


def test_describe_mesh_running_but_not_connected_says_so(ctx: Context, host: FakeHost) -> None:
    c = mesh_on_disk(ctx, host, cluster_id=1, cilium="1.20.4")
    host.active.add("demo.target")
    host.responses[("kubectl", "get", "gateway", "-A", "-o")] = Result(0, "", "")
    host.responses[("kubectl", "get", "svc", "-A", "--field-selector")] = Result(0, "", "")
    lifecycle.describe(ctx, c, None)
    lines = [line for line in host.lines if not line.startswith("  $ ")]
    i = lines.index("  global services:")
    assert lines[i + 1] == "    none (not connected)"
    assert lines[i + 3] == "    none"


def test_describe_mesh_on_a_cluster_from_before_mesh_support(ctx: Context, host: FakeHost) -> None:
    c = ctx.require_cluster()
    net = c.spec.network.model_copy(update={"mesh": None})
    ctx.cluster = c.model_copy(update={"spec": c.spec.model_copy(update={"network": net})})
    c = mesh_on_disk(ctx, host)
    lifecycle.describe(ctx, c, None)
    i = host.lines.index("Mesh:")
    assert host.lines[i + 1] == "  unavailable (created before mesh support; recreate to connect)"
    assert host.lines[i + 2] == "Access:"


def test_get_clusters_lists_mesh_peers(ctx: Context, host: FakeHost) -> None:
    mesh_on_disk(ctx, host, mesh=["other", "third"])
    lifecycle.list_clusters(ctx)
    out = "\n".join(host.lines)
    assert (
        "demo         1+2     1.37.2   auto      Stopped   -      other, third 172.22.0.0/24" in out
    )
