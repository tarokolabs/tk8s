from tk8s.create import kubeadm
from tk8s.host import FakeHost
from tk8s.phases import Context, Runner


def test_init_config_names_cluster_endpoint_and_sans(ctx: Context) -> None:
    text = kubeadm.render_init(ctx)
    assert 'kubernetesVersion: 1.37.1\ncontrolPlaneEndpoint: "api.demo.k8s:6443"' in text
    assert (
        "  certSANs:\n    - api.demo.k8s\n    - 172.22.0.100\n    - 172.22.0.1\n  extraArgs:"
        in text
    )
    assert "advertiseAddress: 172.22.0.1" in text and "name: demo-control-plane-1" in text
    assert "podSubnet: 10.244.0.0/21\n  serviceSubnet: 10.98.0.0/24" in text


def test_render_phase_writes_into_the_state_dir_mode_600(ctx: Context, host: FakeHost) -> None:
    Runner([kubeadm.PHASE], only="kubeadm/render").run(ctx)
    assert host.files[ctx.state_dir / "init-config.yaml"] == kubeadm.render_init(ctx)
    assert kubeadm.render_done(ctx)


import pytest  # noqa: E402

from tk8s.host import Result  # noqa: E402
from tk8s.phases import PhaseFailed  # noqa: E402

KEY = "0123456789abcdef" * 4
INIT_OUT = (
    "[init] Using Kubernetes version: v1.37.1\n...\n"
    "  kubeadm join api.demo.k8s:6443 --token abc.def --discovery-token-ca-cert-hash sha256:1234 "
    f"\\\n\t--control-plane --certificate-key {KEY}\n"
)
ADMIN_CONF = (
    "apiVersion: v1\nkind: Config\nclusters:\n- cluster:\n    server: https://api.demo.k8s:6443\n"
)


def initialised(host: FakeHost) -> None:
    host.responses[
        ("podman", "exec", "demo-control-plane-1", "test", "-f", "/etc/kubernetes/admin.conf")
    ] = Result(0, "", "")
    host.responses[
        ("podman", "exec", "demo-control-plane-1", "cat", "/etc/kubernetes/admin.conf")
    ] = Result(0, ADMIN_CONF, "")
    host.responses[("curl",)] = Result(0, "ok", "")


def test_init_runs_kubeadm_stores_the_key_and_waits_for_the_lb(
    ctx: Context, host: FakeHost
) -> None:
    host.responses[("podman", "exec", "demo-control-plane-1", "test", "-f")] = Result(1, "", "")
    host.responses[("podman", "exec", "demo-control-plane-1", "kubeadm", "init")] = Result(
        0, INIT_OUT, ""
    )
    host.responses[("curl",)] = [Result(0, "", ""), Result(0, "ok", "")]
    kubeadm.init_run(ctx)
    init = next(c for c in host.calls if c.argv[3:5] == ["kubeadm", "init"])
    assert init.argv == [
        "podman",
        "exec",
        "demo-control-plane-1",
        "kubeadm",
        "init",
        "--config=/opt/taroko/init-config.yaml",
        "--upload-certs",
        "--ignore-preflight-errors=SystemVerification",
    ]
    assert init.sudo
    assert host.files[ctx.state_dir / ".certificate-key"] == "0123456789abcdef" * 4 + "\n"
    curls = [c.argv for c in host.calls if c.argv[0] == "curl"]
    assert curls and curls[0] == [
        "curl",
        "-sk",
        "--max-time",
        "3",
        "https://172.22.0.100:6443/readyz",
    ]
    assert "  demo-control-plane-1 initialized" in host.lines


def test_init_is_skipped_when_admin_conf_exists_but_still_waits_for_the_lb(
    ctx: Context, host: FakeHost
) -> None:
    initialised(host)
    kubeadm.init_run(ctx)
    assert not any(c.argv[3:5] == ["kubeadm", "init"] for c in host.calls)
    assert "  demo-control-plane-1 already initialized" in host.lines
    assert any(c.argv[0] == "curl" for c in host.calls)


def test_init_fails_when_kubeadm_fails_and_shows_the_tail(ctx: Context, host: FakeHost) -> None:
    host.responses[("podman", "exec", "demo-control-plane-1", "test", "-f")] = Result(1, "", "")
    host.responses[("podman", "exec", "demo-control-plane-1", "kubeadm", "init")] = Result(
        1, "line1\n" * 40 + "error execution phase preflight: boom\n", ""
    )
    with pytest.raises(PhaseFailed, match="error execution phase preflight: boom"):
        Runner([kubeadm.PHASE], only="kubeadm/init").run(ctx)


def test_init_fails_when_the_lb_never_reports_healthy(ctx: Context, host: FakeHost) -> None:
    initialised(host)
    host.responses[("curl",)] = Result(0, "", "")
    with pytest.raises(
        PhaseFailed,
        match=r"the apiserver LB at 172\.22\.0\.100 has no healthy control plane after 60 s",
    ):
        kubeadm.init_run(ctx)  # init is "done" (admin.conf exists) but still waits for the LB


def test_kubeconfig_points_at_the_lb_and_is_0600(ctx: Context, host: FakeHost) -> None:
    initialised(host)
    kubeadm.kubeconfig_run(ctx)
    write = next(
        c for c in host.calls if c.argv[:2] == ["write", str(ctx.state_dir / "kubeconfig")]
    )
    assert write.argv[2] == "mode=600"
    text = host.files[ctx.state_dir / "kubeconfig"]
    assert "server: https://172.22.0.100:6443" in text and "api.demo.k8s" not in text
    host.responses[("kubectl", "get", "--raw", "/readyz")] = Result(0, "ok", "")
    assert kubeadm.kubeconfig_done(ctx)


def test_init_leaves_no_kubeconfig_when_admin_conf_cannot_be_read(
    ctx: Context, host: FakeHost
) -> None:
    initialised(host)
    host.responses[
        ("podman", "exec", "demo-control-plane-1", "cat", "/etc/kubernetes/admin.conf")
    ] = Result(125, "", "container not running")
    with pytest.raises(
        PhaseFailed, match=r"could not read /etc/kubernetes/admin\.conf from demo-control-plane-1"
    ):
        Runner([kubeadm.PHASE], only="kubeadm/kubeconfig").run(ctx)
    assert ctx.state_dir / "kubeconfig" not in host.files
    assert not kubeadm.kubeconfig_done(ctx)


def test_kubeconfig_done_needs_a_non_empty_file_and_a_ready_apiserver(
    ctx: Context, host: FakeHost
) -> None:
    host.files[ctx.state_dir / "kubeconfig"] = ""
    assert not kubeadm.kubeconfig_done(ctx)
    host.files[ctx.state_dir / "kubeconfig"] = "kind: Config\n"
    host.responses[("kubectl", "get", "--raw", "/readyz")] = Result(1, "", "")
    assert not kubeadm.kubeconfig_done(ctx)
    host.responses[("kubectl", "get", "--raw", "/readyz")] = Result(0, "ok", "")
    assert kubeadm.kubeconfig_done(ctx)
    readyz = next(c for c in host.calls if c.argv[:3] == ["kubectl", "get", "--raw"])
    assert readyz.env == {"KUBECONFIG": str(ctx.state_dir / "kubeconfig")}


def ha_ctx(ctx: Context, host: FakeHost) -> Context:
    """demo with 3 control planes (the third deferred) and one worker."""
    from tk8s import model
    from tk8s.create.plan import resolve

    user = model.UserCluster(
        metadata=model.Metadata(name="demo"),
        spec=model.UserSpec(
            nodes=[
                model.NodeGroup(role="control-plane"),
                model.NodeGroup(role="control-plane"),
                model.NodeGroup(role="control-plane", join=False),
                model.NodeGroup(role="worker"),
            ]
        ),
    )
    ctx.cluster = resolve(
        user,
        k8s_default="1.37.1",
        existing=None,
        used_indices=set(),
        allow_overlap=False,
        tk8s_version="test",
    )
    initialised(host)
    host.files[ctx.state_dir / "kubeconfig"] = "kind: Config\n"
    host.responses[("podman", "exec", "demo-control-plane-1", "kubeadm", "token", "create")] = (
        Result(
            0,
            "kubeadm join api.demo.k8s:6443 --token t.t "
            "--discovery-token-ca-cert-hash sha256:1\r\n",
            "",
        )
    )
    host.responses[("kubectl", "get", "node")] = Result(1, "", "not found")
    return ctx


def test_join_joins_control_planes_and_workers_and_skips_deferred(
    ctx: Context, host: FakeHost
) -> None:
    ctx = ha_ctx(ctx, host)
    host.files[ctx.state_dir / ".certificate-key"] = "abcd\n"
    kubeadm.join_run(ctx)
    joins = [c for c in host.calls if c.argv[:2] == ["podman", "exec"] and "bash" in c.argv]
    cp2 = next(c for c in joins if c.argv[2] == "demo-control-plane-2")
    assert cp2.argv[3:5] == ["bash", "-c"]
    assert cp2.argv[5] == (
        "kubeadm join api.demo.k8s:6443 --token t.t --discovery-token-ca-cert-hash sha256:1 "
        "--control-plane --certificate-key abcd --apiserver-advertise-address 172.22.0.2 "
        "--ignore-preflight-errors=SystemVerification"
    )
    wk = next(c for c in joins if c.argv[2] == "demo-worker-1")
    assert wk.argv[5].endswith("sha256:1 --ignore-preflight-errors=SystemVerification")
    assert not any(c.argv[2] == "demo-control-plane-3" for c in joins)
    assert (
        "  demo-control-plane-3 left unjoined (join: false); join later with: "
        "tkctl join node demo demo-control-plane-3"
    ) in host.lines
    assert "  demo-control-plane-2 joined (control-plane)" in host.lines
    taints = [c.argv for c in host.calls if c.argv[:3] == ["kubectl", "taint", "node"]]
    assert [t[3] for t in taints] == [
        "demo-control-plane-1",
        "demo-control-plane-2",
        "demo-control-plane-3",
    ]
    kube = [c for c in host.calls if c.argv[0] == "kubectl"]
    assert all(c.env == {"KUBECONFIG": str(ctx.state_dir / "kubeconfig")} for c in kube)


def test_join_uploads_a_fresh_certificate_key_when_none_is_stored(
    ctx: Context, host: FakeHost
) -> None:
    ctx = ha_ctx(ctx, host)
    host.responses[
        ("podman", "exec", "demo-control-plane-1", "kubeadm", "init", "phase", "upload-certs")
    ] = Result(0, "[upload-certs] Using certificate key:\nffff\n", "")
    kubeadm.join_run(ctx)
    assert host.files[ctx.state_dir / ".certificate-key"] == "ffff\n"
    cp2 = next(c for c in host.calls if c.argv[:3] == ["podman", "exec", "demo-control-plane-2"])
    assert "--certificate-key ffff" in cp2.argv[5]


def test_join_skips_nodes_already_in_the_cluster(ctx: Context, host: FakeHost) -> None:
    ctx = ha_ctx(ctx, host)
    host.files[ctx.state_dir / ".certificate-key"] = "abcd\n"
    general = host.responses.pop(("kubectl", "get", "node"))  # specific prefixes must come first
    host.responses[("kubectl", "get", "node", "demo-worker-1")] = Result(
        0, "demo-worker-1 Ready", ""
    )
    host.responses[("kubectl", "get", "node")] = general
    kubeadm.join_run(ctx)
    assert not any(c.argv[:3] == ["podman", "exec", "demo-worker-1"] for c in host.calls)
    assert "  demo-worker-1 already joined" in host.lines


def test_join_failure_shows_the_tail_and_names_the_node(ctx: Context, host: FakeHost) -> None:
    ctx = ha_ctx(ctx, host)
    host.files[ctx.state_dir / ".certificate-key"] = "abcd\n"
    host.responses[("podman", "exec", "demo-control-plane-2", "bash")] = Result(
        1, "x\n" * 30 + "error: boom\n", ""
    )
    with pytest.raises(PhaseFailed, match=r"demo-control-plane-2(.|\n)*error: boom"):
        Runner([kubeadm.PHASE], only="kubeadm/join").run(ctx)


def test_join_done_when_every_joining_node_is_in_the_cluster(ctx: Context, host: FakeHost) -> None:
    ctx = ha_ctx(ctx, host)
    assert not kubeadm.join_done(ctx)
    host.responses[("kubectl", "get", "node")] = Result(0, "x Ready", "")
    assert kubeadm.join_done(ctx)  # demo-control-plane-3 (join: false) is not required


def test_render_also_places_the_audit_policy_the_apiserver_mounts(
    ctx: Context, host: FakeHost
) -> None:
    """kube-apiserver's --audit-policy-file points at /opt/taroko/logs/apiserver/audit-policy.yaml,
    the state dir mounted into the node; without the file the apiserver crash-loops."""
    Runner([kubeadm.PHASE], only="kubeadm/render").run(ctx)
    policy = ctx.state_dir / "logs" / "apiserver" / "audit-policy.yaml"
    assert host.files[policy] == (ctx.paths.home / "manifests" / "audit-policy.yaml").read_text()
    assert kubeadm.render_done(ctx)
    del host.files[policy]
    assert not kubeadm.render_done(ctx)


def test_kubeconfig_waits_for_the_apiserver_through_the_lb(ctx: Context, host: FakeHost) -> None:
    """On a resume init is already done and skipped, so kubeconfig is the phase that must wait
    for the apiserver (a kubelet in CrashLoopBackOff brings it up seconds to minutes later)."""
    initialised(host)
    host.responses[("curl",)] = [Result(0, "", ""), Result(0, "", ""), Result(0, "ok", "")]
    kubeadm.kubeconfig_run(ctx)
    curls = [c for c in host.calls if c.argv[0] == "curl"]
    assert len(curls) == 3 and ctx.state_dir / "kubeconfig" in host.files
    host.calls.clear()
    host.responses[("curl",)] = Result(0, "", "")
    with pytest.raises(PhaseFailed, match="has no healthy control plane after 60 s"):
        kubeadm.kubeconfig_run(ctx)


def test_init_that_did_not_complete_is_refused_not_skipped(ctx: Context, host: FakeHost) -> None:
    """admin.conf exists after a kubeadm init that died in wait-control-plane; the cluster has no
    bootstrap token, no CoreDNS and an admin that cannot do anything. Say so instead of going on."""
    initialised(host)
    host.responses[("podman", "exec", "demo-control-plane-1", "kubectl")] = Result(
        1, "", "forbidden"
    )
    assert not kubeadm.init_done(ctx)
    with pytest.raises(PhaseFailed, match=r"kubeadm init on demo-control-plane-1 did not complete"):
        kubeadm.init_run(ctx)
    host.responses[("podman", "exec", "demo-control-plane-1", "kubectl")] = Result(
        0, "kubeadm-config", ""
    )
    assert kubeadm.init_done(ctx)
    host.calls.clear()
    kubeadm.init_run(ctx)
    assert "  demo-control-plane-1 already initialized" in host.lines


def test_init_already_done_waits_for_the_apiserver_before_judging_completion(
    ctx: Context, host: FakeHost
) -> None:
    """A resume right after a restart: admin.conf exists, the apiserver is still coming up. The
    completion check must come after the LB wait, or a healthy cluster is told to delete itself."""
    initialised(host)
    host.responses[("podman", "exec", "demo-control-plane-1", "kubectl")] = [
        Result(1, "", "connection refused"),
        Result(0, "kubeadm-config", ""),
    ]
    host.responses[("curl",)] = [Result(0, "", ""), Result(0, "ok", "")]
    assert not kubeadm.init_done(ctx)  # the blip
    kubeadm.init_run(ctx)  # waits for the LB, then finds the cluster bootstrapped
    assert "  demo-control-plane-1 already initialized" in host.lines
    curl_at = next(i for i, c in enumerate(host.calls) if c.argv[0] == "curl")
    cm_at = next(
        i
        for i, c in enumerate(host.calls)
        if c.argv[:4] == ["podman", "exec", "demo-control-plane-1", "kubectl"] and i > curl_at
    )
    assert curl_at < cm_at


def test_certificate_key_fresh_ignores_the_stored_key(ctx: Context, host: FakeHost) -> None:
    host.files[ctx.state_dir / ".certificate-key"] = "stale\n"
    host.responses[
        ("podman", "exec", "demo-control-plane-1", "kubeadm", "init", "phase", "upload-certs")
    ] = Result(0, "[upload-certs] Using certificate key:\nnewkey\n", "")
    assert kubeadm.certificate_key(ctx) == "stale"
    assert kubeadm.certificate_key(ctx, fresh=True) == "newkey"
    assert host.files[ctx.state_dir / ".certificate-key"] == "newkey\n"
