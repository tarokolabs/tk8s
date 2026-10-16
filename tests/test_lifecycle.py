from pathlib import Path

import pytest

from tests.fixtures import HOME, OP, SHOW_HAPROXY, bgp_router_answers
from tk8s import lifecycle, model, versions
from tk8s.create.plan import resolve
from tk8s.host import FakeHost, Result
from tk8s.paths import Paths
from tk8s.phases import Context, Options


def on_disk(ctx: Context, host: FakeHost, *, marker: bool = False) -> model.Cluster:
    """demo's cluster.yaml on disk (list and describe read the data dir directly) and in the fake
    host (writes go through it). With marker: the file a v2026.10.x create left, no status."""
    c = ctx.require_cluster()
    state = ctx.paths.state_dir(c.name)
    state.mkdir(parents=True)
    text = model.dumps(c.model_copy(update={"status": None}) if marker else c)
    if marker:
        (state / ".create-complete").write_text("")
        host.files[state / ".create-complete"] = ""
    (state / "cluster.yaml").write_text(text)
    host.files[state / "cluster.yaml"] = text
    return model.load(state / "cluster.yaml", create_complete_marker=marker)


def test_load_cluster_names_the_missing_cluster(ctx: Context) -> None:
    with pytest.raises(
        lifecycle.ClusterError, match=r"cluster nope not found under .* \(tkctl get clusters\)"
    ):
        lifecycle.load_cluster(ctx, "nope")


def test_a_pre_status_cluster_with_the_marker_is_ready(ctx: Context, host: FakeHost) -> None:
    on_disk(ctx, host, marker=True)
    c = lifecycle.load_cluster(ctx, "demo")
    assert c.status is not None and c.status.phase == "Ready" and c.status.tk8s == "pre-status"
    assert ctx.cluster is c and ctx.name == "demo"
    assert lifecycle.state_of(ctx, c) == "Stopped"
    host.active.add("demo.target")
    assert lifecycle.state_of(ctx, c) == "Running"


def test_save_cluster_retires_the_marker(ctx: Context, host: FakeHost) -> None:
    on_disk(ctx, host, marker=True)
    c = lifecycle.load_cluster(ctx, "demo")
    lifecycle.save_cluster(ctx, c)
    state = ctx.paths.state_dir("demo")
    assert "phase: Ready" in host.files[state / "cluster.yaml"]
    assert (state / ".create-complete") not in host.files
    assert ["rm", "-f", str(state / ".create-complete")] in [k.argv for k in host.calls]


def test_list_shows_every_cluster_and_its_state(ctx: Context, host: FakeHost) -> None:
    on_disk(ctx, host)
    broken = ctx.paths.clusters_dir / "broken"
    broken.mkdir()
    (broken / "cluster.yaml").write_text("metadata: {name: broken}\nspec: {nodes: []}\n")
    lifecycle.list_clusters(ctx)
    out = "\n".join(host.lines)
    assert out.splitlines()[0].split() == ["NAME", "NODES", "K8S", "DATAPATH", "STATE", "SUBNET"]
    assert "demo         1+2     1.37.1   auto      Creating  172.22.0.0/24" in out
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
        "Kubernetes:  1.37.1   cni: cilium   datapath: auto   gvisor: false",
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
        0, "registry.k8s.io/metrics-server/metrics-server:v0.9.0", ""
    )
    r[("kubectl", "--request-timeout=5s", "-n", "local-path-storage")] = Result(1, "", "nope")
    r[("kubectl", "--request-timeout=5s", "get", "runtimeclass")] = Result(1, "", "nope")
    r[("ip", "-4", "route", "get")] = Result(
        0, "1.1.1.1 via 192.168.1.1 dev eth0 src 192.168.1.230 uid 1000\n", ""
    )
    lifecycle.describe(ctx, c, None)
    out = "\n".join(host.lines)
    assert "State:       Running" in out
    assert "  metrics-server  v0.9.0" in out and "  local-path      not installed" in out
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
        "demo-control-plane-1 Ready control-plane 1d v1.37.1\n"
        "demo-worker-1 Ready <none> 1d v1.37.1\n",
        "",
    )
    lifecycle.start(ctx, ctx.require_cluster())
    argvs = [k.argv for k in host.calls]
    assert ["systemctl", "start", "demo.target"] in argvs
    assert sum(1 for a in argvs if a[:4] == ["kubectl", "get", "nodes", "-o"]) == 2
    assert host.lines[-2:] == ["  demo-control-plane-1 Ready", "  demo-worker-1 Ready"]


def test_use_creates_kube_dir_and_keeps_the_previous_config(
    ctx: Context, host: FakeHost, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    host.files[ctx.state_dir / "kubeconfig"] = "kind: Config\nclusters: [demo]\n"
    lifecycle.use(ctx, ctx.require_cluster())
    config = home / ".kube" / "config"
    assert host.files[config] == "kind: Config\nclusters: [demo]\n"
    assert (home / ".kube" / "config.bak") not in host.files  # nothing to back up the first time
    writes = [k for k in host.calls if k.argv[0] == "write" and k.argv[1] == str(config)]
    assert writes and writes[0].argv[2] == "mode=600"
    assert host.lines[-1] == "~/.kube/config now points at demo"
    host.files[ctx.state_dir / "kubeconfig"] = "kind: Config\nclusters: [other]\n"
    lifecycle.use(ctx, ctx.require_cluster())
    assert host.files[home / ".kube" / "config.bak"] == "kind: Config\nclusters: [demo]\n"


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
    assert "lb-ipam 172.22.0.200-172.22.0.219  router 172.22.0.253  vip 172.23.0.1" in out
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
