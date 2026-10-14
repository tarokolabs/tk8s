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
    assert "advertiseAddress: 172.22.0.1" in text and "name: demo-cp-1" in text
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
    host.responses[("podman", "exec", "demo-cp-1", "test", "-f", "/etc/kubernetes/admin.conf")] = (
        Result(0, "", "")
    )
    host.responses[("podman", "exec", "demo-cp-1", "cat", "/etc/kubernetes/admin.conf")] = Result(
        0, ADMIN_CONF, ""
    )
    host.responses[("curl",)] = Result(0, "ok", "")


def test_init_runs_kubeadm_stores_the_key_and_waits_for_the_lb(
    ctx: Context, host: FakeHost
) -> None:
    host.responses[("podman", "exec", "demo-cp-1", "test", "-f")] = Result(1, "", "")
    host.responses[("podman", "exec", "demo-cp-1", "kubeadm", "init")] = Result(0, INIT_OUT, "")
    host.responses[("curl",)] = [Result(0, "", ""), Result(0, "ok", "")]
    kubeadm.init_run(ctx)
    init = next(c for c in host.calls if c.argv[3:5] == ["kubeadm", "init"])
    assert init.argv == [
        "podman",
        "exec",
        "demo-cp-1",
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
    assert "  demo-cp-1 initialized" in host.lines


def test_init_is_skipped_when_admin_conf_exists_but_still_waits_for_the_lb(
    ctx: Context, host: FakeHost
) -> None:
    initialised(host)
    kubeadm.init_run(ctx)
    assert not any(c.argv[3:5] == ["kubeadm", "init"] for c in host.calls)
    assert "  demo-cp-1 already initialized" in host.lines
    assert any(c.argv[0] == "curl" for c in host.calls)


def test_init_fails_when_kubeadm_fails_and_shows_the_tail(ctx: Context, host: FakeHost) -> None:
    host.responses[("podman", "exec", "demo-cp-1", "test", "-f")] = Result(1, "", "")
    host.responses[("podman", "exec", "demo-cp-1", "kubeadm", "init")] = Result(
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
    host.responses[("podman", "exec", "demo-cp-1", "cat", "/etc/kubernetes/admin.conf")] = Result(
        125, "", "container not running"
    )
    with pytest.raises(
        PhaseFailed, match=r"could not read /etc/kubernetes/admin\.conf from demo-cp-1"
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
