from pathlib import Path

import pytest

from tk8s import lifecycle, model
from tk8s.host import FakeHost, Result
from tk8s.phases import Context


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


def test_describe_sections(ctx: Context, host: FakeHost) -> None:
    c = on_disk(ctx, host)
    lifecycle.describe(ctx, c, None)
    out = "\n".join(host.lines)
    for s in (
        "Name:        demo",
        "State:       Creating",
        "Kubernetes:  1.37.1   cni: cilium   datapath: auto   gvisor: false",
        "lb 172.22.0.100",
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
        "  apiserver:  https://172.22.0.100:6443",
        "sudo ip route add 172.22.0.0/24 via <host-ip>",
        "Paths:",
        "/etc/containers/systemd/demo/",
    ):
        assert s in out, s
    assert "dns 172" not in out


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
        "demo-lb.service",
        "demo-cp-1.service",
        "demo-worker-1.service",
        "demo-worker-2.service",
    ]
    assert host.lines[-1] == "cluster demo stopped"


def test_start_waits_for_fresh_ready_heartbeats(ctx: Context, host: FakeHost) -> None:
    stale = (
        "demo-cp-1 True 2000-01-01T00:00:00Z\ndemo-worker-1 True 2000-01-01T00:00:00Z\n"
        "demo-worker-2 True 2000-01-01T00:00:00Z\n"
    )
    fresh = stale.replace("2000-01-01", "2999-01-01")
    host.responses[("kubectl", "get", "nodes", "-o")] = [Result(0, stale, ""), Result(0, fresh, "")]
    host.responses[("kubectl", "get", "nodes", "--no-headers")] = Result(
        0, "demo-cp-1 Ready control-plane 1d v1.37.1\ndemo-worker-1 Ready <none> 1d v1.37.1\n", ""
    )
    lifecycle.start(ctx, ctx.require_cluster())
    argvs = [k.argv for k in host.calls]
    assert ["systemctl", "start", "demo.target"] in argvs
    assert sum(1 for a in argvs if a[:4] == ["kubectl", "get", "nodes", "-o"]) == 2
    assert host.lines[-2:] == ["  demo-cp-1 Ready", "  demo-worker-1 Ready"]


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
